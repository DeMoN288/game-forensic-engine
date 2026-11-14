from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Review


class Parser:
    @staticmethod
    def text(html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for element in soup(["script", "style", "nav"]):
            element.decompose()
        return soup.get_text(" ", strip=True)

    def store(self, payload: dict, appid: int) -> tuple[str, str, dict]:
        item = payload.get(str(appid), {})
        if not item.get("success") or not isinstance(item.get("data"), dict):
            raise ValueError("Store returned no app data (region restriction, removed app or invalid AppID)")
        data = item["data"]
        if data.get("type") != "game":
            raise ValueError(f"Not a base game: {data.get('type')}")
        if data.get("steam_appid") != appid or not data.get("name"):
            raise ValueError("Store identity mismatch")
        description = self.text(data.get("detailed_description") or data.get("short_description", ""))
        meta = {key: data.get(key) for key in (
            "release_date", "price_overview", "developers", "publishers", "genres", "categories",
            "pc_requirements", "platforms", "is_free", "demos",
        )}
        return data["name"], description, meta

    @staticmethod
    def review(item: dict) -> Review:
        author = item.get("author") or {}
        if "recommendationid" not in item or not isinstance(item.get("voted_up"), bool):
            raise ValueError("Malformed review identity or vote")
        return Review(
            id=str(item["recommendationid"]), text=item.get("review", ""), positive=item["voted_up"],
            language=item.get("language", "english"), minutes_at_review=author.get("playtime_at_review"),
            minutes_total=author.get("playtime_forever"), created=item.get("timestamp_created"),
            updated=item.get("timestamp_updated"), last_played=author.get("last_played"),
            helpful=item.get("votes_up", 0), free=item.get("received_for_free", False),
            early_access=item.get("written_during_early_access", False), refunded=item.get("refunded", False),
        )

    def peek(self, html: str, reference: int) -> list[int]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: list[int] = []
        for anchor in soup.select("a[href]"):
            href = urlsplit(urljoin("https://steampeek.hu/", anchor["href"]))
            candidate: str | None = None
            if href.hostname == "store.steampowered.com":
                match = re.match(r"/app/(\d+)(?:/|$)", href.path)
                if match:
                    candidate = match.group(1)
            elif href.hostname in {"steampeek.hu", "www.steampeek.hu"}:
                candidate = (parse_qs(href.query).get("appid") or [None])[0]
            if candidate and candidate.isdigit() and int(candidate) != reference:
                title = anchor.get_text(" ", strip=True)
                if re.search(r"\b(demo|soundtrack|prologue|playtest|dedicated server)\b",
                             title, re.IGNORECASE):
                    continue
                candidates.append(int(candidate))
        result = list(dict.fromkeys(candidates))
        if not result:
            raise ValueError("SteamPeek returned no candidate links; empty page or changed markup")
        return result

    def steamdb(self, html: str, appid: int) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        canonical = soup.select_one('link[rel="canonical"]')
        if canonical and f"/app/{appid}/" not in canonical.get("href", ""):
            raise ValueError("SteamDB identity mismatch")
        if "SteamDB" not in title:
            raise ValueError("SteamDB document not recognized")
        rows = {}
        for tr in soup.select("table tr"):
            cells = tr.find_all(["td", "th"], recursive=False)
            if len(cells) == 2:
                rows[cells[0].get_text(" ", strip=True)] = cells[1].get_text(" ", strip=True)
        if not rows:
            raise ValueError("SteamDB metadata table missing")
        return {"title": title, "fields": rows}
