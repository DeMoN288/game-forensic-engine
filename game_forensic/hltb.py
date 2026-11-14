from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class HltbTimes:
    game_id: int
    game_name: str
    main_hours: float | None
    main_extra_hours: float | None
    completionist_hours: float | None
    combined_hours: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _hours(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return round(seconds / 3600.0, 1)


def _cache_path(game_name: str, cache_dir: Path | None) -> Path:
    directory = cache_dir or Path(".cache") / "game-forensic"
    digest = hashlib.sha256(game_name.encode("utf-8")).hexdigest()
    return directory / f"hltb_{digest}.json"


def _cached(path: Path, ttl_seconds: int) -> HltbTimes | None:
    if ttl_seconds < 0 or not path.is_file():
        return None
    try:
        if time.time() - path.stat().st_mtime > ttl_seconds:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return HltbTimes(**raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("data", "games", "results"):
        value = payload.get(key)
        found = _items(value)
        if found:
            return found

    for value in payload.values():
        found = _items(value)
        if found:
            return found
    return []


async def fetch_hltb_times(
    game_name: str,
    appid: int | None = None,
    *,
    cache_dir: Path | None = None,
    ttl_seconds: int = 86400,
    transport: httpx.AsyncBaseTransport | None = None,
) -> HltbTimes | None:
    name = game_name.strip()
    if not name:
        return None

    cache_path = _cache_path(name, cache_dir)
    cached = _cached(cache_path, ttl_seconds)
    if cached is not None:
        return cached

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://howlongtobeat.com/",
        "Origin": "https://howlongtobeat.com",
    }
    base_url = "https://howlongtobeat.com"

    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            transport=transport,
            timeout=30,
        ) as client:
            init_response = await client.get("/api/search/site/init?t=1")
            init_response.raise_for_status()
            init_data = init_response.json()
            token = init_data["token"]

            response = await client.post(
                "/api/search/site",
                headers={"x-auth-token": str(token)},
                json={"searchTerms": name.split()},
            )
            response.raise_for_status()
            candidates = _items(response.json())
            if not candidates:
                return None

            selected = candidates[0]
            if appid is not None:
                selected = next(
                    (
                        item
                        for item in candidates
                        if str(item.get("profile_steam", "")) == str(appid)
                    ),
                    selected,
                )

            result = HltbTimes(
                game_id=int(selected["game_id"]),
                game_name=str(selected.get("game_name") or name),
                main_hours=_hours(selected.get("comp_main")),
                main_extra_hours=_hours(selected.get("comp_plus")),
                completionist_hours=_hours(selected.get("comp_100")),
                combined_hours=_hours(selected.get("comp_all")),
            )
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(asdict(result), ensure_ascii=True),
                encoding="utf-8",
            )
            return result
    except Exception:
        return None


__all__ = ["HltbTimes", "fetch_hltb_times"]
