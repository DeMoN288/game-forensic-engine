from __future__ import annotations

import asyncio
import json

from pydantic import Field

from .models import Game, Model, Review, SourceStatus
from .network import SourceError, Transport
from .parser import Parser

NEW_REVIEWS = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"


class PipelineConfig(Model):
    country: str = Field(default="us", pattern=r"^[a-z]{2}$")
    languages: list[str] = Field(default_factory=lambda: ["english", "russian"])
    review_pages: int = Field(default=4, ge=1, le=30)
    page_size: int = Field(default=100, ge=1, le=100)
    enable_steamdb: bool = False
    enable_steampeek: bool = True
    allow_legacy_reviews: bool = True
    game_concurrency: int = Field(default=3, ge=1, le=12)
    source_timeout: float = Field(default=180, gt=0)


class Pipeline:
    def __init__(self, transport: Transport, config: PipelineConfig | None = None):
        self.transport = transport
        self.config = config or PipelineConfig()
        self.parser = Parser()
        self.games_sem = asyncio.Semaphore(self.config.game_concurrency)

    async def _store(self, appid: int) -> tuple[dict, SourceStatus]:
        url = "https://store.steampowered.com/api/appdetails"
        try:
            async with asyncio.timeout(self.config.source_timeout):
                payload = await self.transport.json(url, {"appids": appid, "cc": self.config.country,
                                                          "l": "english"})
            name, description, metadata = self.parser.store(payload, appid)
            return {"name": name, "description": description, "metadata": metadata}, SourceStatus(
                source="steam_store", url=url, status="ok", count=1)
        except (SourceError, TimeoutError) as exc:
            return {}, SourceStatus(source="steam_store", url=url, status="unavailable", detail=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            return {}, SourceStatus(source="steam_store", url=url, status="parse_error", detail=str(exc))

    async def _reviews(self, appid: int) -> tuple[list[Review], SourceStatus, dict]:
        reviews: dict[str, Review] = {}
        cursor = "*"
        seen_cursors: set[str] = set()
        legacy = False
        summary = {}
        warnings: list[str] = []
        malformed = 0
        finished = False
        source_url = NEW_REVIEWS
        try:
            async with asyncio.timeout(self.config.source_timeout):
                for page in range(self.config.review_pages):
                    try:
                        if legacy:
                            payload = await self._legacy(appid, cursor)
                        else:
                            payload = await self.transport.json(NEW_REVIEWS, {"input_json": json.dumps({
                                "appid": appid, "filter": 1, "languages": self.config.languages,
                                "cursor": cursor, "review_type": 0, "purchase_type": 1,
                                "num_per_page": self.config.page_size, "filter_offtopic_activity": True,
                            })})
                            payload = payload.get("response", payload)
                    except SourceError:
                        if legacy or not self.config.allow_legacy_reviews:
                            raise
                        legacy = True
                        source_url = f"https://store.steampowered.com/appreviews/{appid}"
                        warnings.append("New reviews service unavailable; deprecated endpoint fallback")
                        payload = await self._legacy(appid, cursor)
                    if not isinstance(payload.get("reviews"), list):
                        raise ValueError("reviews array missing")
                    if page == 0:
                        summary = payload.get("query_summary", {})
                    items = payload["reviews"]
                    for item in items:
                        try:
                            review = self.parser.review(item)
                            # Legacy endpoint fetches all languages; filter locally.
                            if "all" in self.config.languages or review.language in self.config.languages:
                                reviews[review.id] = review
                        except (ValueError, TypeError, KeyError):
                            malformed += 1
                    new_cursor = payload.get("cursor")
                    if not items:
                        finished = True
                        break
                    if not new_cursor or new_cursor == cursor or new_cursor in seen_cursors:
                        warnings.append("Missing/repeating pagination cursor; stopped")
                        break
                    seen_cursors.add(cursor)
                    cursor = new_cursor
            if malformed:
                warnings.append(f"Skipped malformed reviews: {malformed}")
            if not finished:
                warnings.append("Bounded recent sample, not a population census")
            return list(reviews.values()), SourceStatus(
                source="steam_reviews", url=source_url, status="partial" if warnings else "ok",
                count=len(reviews), detail="; ".join(warnings)), summary
        except (SourceError, TimeoutError, ValueError, TypeError, KeyError) as exc:
            return list(reviews.values()), SourceStatus(
                source="steam_reviews", url=source_url, status="partial" if reviews else "unavailable",
                count=len(reviews), detail="; ".join([*warnings, type(exc).__name__, str(exc)])), summary

    async def _legacy(self, appid: int, cursor: str) -> dict:
        payload = await self.transport.json(f"https://store.steampowered.com/appreviews/{appid}", {
            "json": 1, "filter": "recent", "language": "all", "review_type": "all",
            "purchase_type": "all", "num_per_page": self.config.page_size,
            "filter_offtopic_activity": 1, "cursor": cursor,
        })
        if payload.get("success") != 1:
            raise SourceError("Legacy reviews service returned failure")
        return payload

    async def _html_source(self, appid: int, kind: str) -> tuple[dict, SourceStatus]:
        url = f"https://steamdb.info/app/{appid}/" if kind == "steamdb" else f"https://steampeek.hu/?appid={appid}"
        enabled = self.config.enable_steamdb if kind == "steamdb" else self.config.enable_steampeek
        if not enabled:
            return {}, SourceStatus(source=kind, url=url, status="disabled", detail="Explicitly disabled")
        try:
            async with asyncio.timeout(self.config.source_timeout):
                html = await self.transport.get(url)
            if kind == "steamdb":
                data = {"steamdb": self.parser.steamdb(html, appid)}
                count = len(data["steamdb"]["fields"])
            else:
                data = {"candidate_ids": self.parser.peek(html, appid)}
                count = len(data["candidate_ids"])
            return data, SourceStatus(source=kind, url=url, status="ok", count=count)
        except (SourceError, TimeoutError) as exc:
            return {}, SourceStatus(source=kind, url=url, status="unavailable", detail=str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            return {}, SourceStatus(source=kind, url=url, status="parse_error", detail=str(exc))

    async def collect(self, appid: int, discover: bool = False) -> Game:
        if appid <= 0:
            raise ValueError("AppID must be positive")
        async with self.games_sem:
            tasks = [self._store(appid), self._reviews(appid), self._html_source(appid, "steamdb")]
            if discover:
                tasks.append(self._html_source(appid, "steampeek"))
            results = await asyncio.gather(*tasks)
        store, store_status = results[0]
        reviews, reviews_status, summary = results[1]
        db, db_status = results[2]
        game = Game(appid=appid, **store, reviews=reviews, sources=[store_status, reviews_status, db_status])
        game.metadata.update(db)
        game.metadata["review_query_summary"] = summary
        game.metadata["sampling"] = {"filter": "recent", "review_type": "all",
                                     "languages": self.config.languages, "max_pages": self.config.review_pages}
        if discover:
            peek, status = results[3]
            game.candidate_ids = peek.get("candidate_ids", [])
            game.sources.append(status)
        return game

    async def collect_many(self, appids: list[int]) -> list[Game]:
        # Process bounded batches, rather than allocating a task for every input game.
        result = []
        ids = list(dict.fromkeys(appids))
        for start in range(0, len(ids), self.config.game_concurrency):
            result.extend(await asyncio.gather(*(self.collect(appid) for appid in
                                                 ids[start:start + self.config.game_concurrency])))
        return result
