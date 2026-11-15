"""Steam US store price lookup with a small disk cache."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import time
from pathlib import Path

import httpx

from .storage import write_json_object


def _valid_price(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and all(isinstance(raw.get(key), (int, float)) and not isinstance(raw[key], bool)
                and math.isfinite(raw[key]) and raw[key] >= 0
                for key in ("sale_price", "normal_price"))
        and isinstance(raw.get("is_on_sale"), bool)
    )


async def fetch_steam_price(
    appid: int | None = None,
    title: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict | None:
    if not isinstance(appid, int) or isinstance(appid, bool) or appid <= 0:
        return None
    key = f"steam-us-v2:{appid}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    cache = Path(".cache/game-forensic") / f"price_{digest}.json"
    try:
        with contextlib.suppress(OSError, UnicodeError, ValueError):
            if cache.exists() and time.time() - cache.stat().st_mtime < 3600:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if _valid_price(raw):
                    return raw
        params = {"appids": appid, "cc": "us", "filters": "price_overview,is_free"}
        async with httpx.AsyncClient(timeout=8, transport=transport) as client:
            response = await client.get("https://store.steampowered.com/api/appdetails", params=params)
            response.raise_for_status()
            rows = response.json()
        row = rows[str(appid)]
        if row.get("success") is not True:
            return None
        data = row["data"]
        if data.get("is_free") is True:
            sale = normal = 0.0
        else:
            price = data["price_overview"]
            if price["currency"] != "USD":
                return None
            sale = float(price["final"]) / 100
            normal = float(price["initial"]) / 100
        result = {"sale_price": sale, "normal_price": normal, "is_on_sale": sale < normal}
        if not _valid_price(result):
            return None
        with contextlib.suppress(OSError):
            write_json_object(cache, result)
        return result
    except (httpx.HTTPError, OSError, UnicodeError, ValueError, TypeError, KeyError, AttributeError):
        return None
