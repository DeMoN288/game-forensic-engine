"""Steam Store discovery by actual catalog tags, not title keywords."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .network import SourceError, Transport

_STORE = "https://store.steampowered.com"
_NON_GAME_SUFFIXES = (" soundtrack", " ost", " demo", " artbook", " dlc")


def _tag_key(value: str) -> str:
    return " ".join(value.casefold().replace("-", " ").replace("_", " ").split())


async def resolve_tag(transport: Transport, tag: str | int) -> tuple[int, str]:
    """Resolve English tag names/slugs; numeric IDs need no catalog lookup."""
    clean = str(tag).strip()
    if not clean or isinstance(tag, bool):
        raise ValueError("Steam tag must be a name or a positive tag ID")
    if clean.lstrip("+-").isdigit():
        tag_id = int(clean)
        if tag_id <= 0:
            raise ValueError("Steam tag ID must be positive")
        return tag_id, clean

    try:
        tags = json.loads(await transport.get(f"{_STORE}/tagdata/populartags/english"))
    except ValueError as exc:
        raise SourceError("Steam tag catalog returned invalid JSON") from exc
    if not isinstance(tags, list):
        raise SourceError("Steam tag catalog must be a list")
    for item in tags:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        if _tag_key(item["name"]) != _tag_key(clean):
            continue
        tag_id = item.get("tagid")
        if isinstance(tag_id, int) and not isinstance(tag_id, bool) and tag_id > 0:
            return tag_id, item["name"]
    raise ValueError(f"Unknown Steam tag: '{clean}'; use an English name or numeric tag ID")


def parse_search_results(html: str) -> list[dict]:
    """Extract named app links, ignoring bundles, malformed rows and duplicates."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    seen: set[int] = set()
    for row in soup.select("a.search_result_row[href]"):
        link = urlsplit(row["href"])
        match = re.fullmatch(r"/app/(\d+)(?:/.*)?", link.path)
        if link.hostname != "store.steampowered.com" or not match:
            continue
        appid = int(match.group(1))
        title = row.select_one(".title")
        name = title.get_text(" ", strip=True) if title else ""
        if not appid or appid in seen or not name or name.lower().endswith(_NON_GAME_SUFFIXES):
            continue
        # Multi-app rows are bundles, not individual games.
        if row.get("data-ds-appid", str(appid)) != str(appid):
            continue
        seen.add(appid)
        items.append({"appid": appid, "name": name, "url": f"{_STORE}/app/{appid}/"})
    return items


async def search_by_tag(transport: Transport, tag: str | int, limit: int = 10) -> dict:
    """Return a bounded discovery page with a real Steam tag filter."""
    tag_id, tag_name = await resolve_tag(transport, tag)
    safe_limit = min(max(limit, 1), 25)
    items: list[dict] = []
    seen: set[int] = set()
    # Bounded pagination allows filtered-out rows without unbounded requests.
    for start in range(0, 150, 50):
        data = await transport.json(
            f"{_STORE}/search/results/",
            {"tags": str(tag_id), "category1": "998", "start": start, "count": 50,
             "infinite": 1, "l": "english", "cc": "us", "ignore_preferences": 1},
        )
        if data.get("success") in (False, 0):
            raise SourceError("Steam tag search failed")
        html = data.get("results_html")
        if not isinstance(html, str):
            raise SourceError("Steam tag search returned no results_html")
        for item in parse_search_results(html):
            if item["appid"] not in seen:
                seen.add(item["appid"])
                items.append(item)
                if len(items) == safe_limit:
                    break
        if len(items) == safe_limit or not html.strip():
            break
        total = data.get("total_count")
        if isinstance(total, (int, str)) and str(total).isdigit() and start + 50 >= int(total):
            break
    return {"tag": tag_name, "tag_id": tag_id, "count": len(items), "items": items}
