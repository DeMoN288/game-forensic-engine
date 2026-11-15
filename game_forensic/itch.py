from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .analyzer import Analyzer, AnalyzerConfig
from .matcher import extract_loop
from .models import CoreLoop, Game, Review

ITCH_BASE_URL = "https://itch.io"
MAX_COMMENTS = 80

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}

_DURATION_RE = re.compile(
    r"(?<!\w)(\d+(?:[.,]\d+)?)"
    r"(?:\s*(?:[-–—]|to)\s*(\d+(?:[.,]\d+)?))?"
    r"\s*(hours?|hrs?|hr|h|minutes?|mins?|min)\b",
    re.IGNORECASE,
)

_DEMO_RE = re.compile(r"\bdemo\b", re.IGNORECASE)


def is_demo_title(title: str) -> bool:
    """Return True when a game title looks like a demo release."""
    return bool(_DEMO_RE.search(title))


@dataclass
class ItchGame:
    title: str
    url: str
    author: str = ""
    genre: str = ""
    price: str = "Free"
    platforms: list[str] = field(default_factory=list)
    is_browser: bool = False
    is_downloadable: bool = False
    description: str = ""
    comments_count: int = 0
    estimated_hours: float | None = None
    duration_source: str = "unknown"
    gti_score: float = 0.0
    verdict: str = "INSUFFICIENT_DATA"
    warnings: list[str] = field(default_factory=list)
    loop: CoreLoop = field(default_factory=CoreLoop)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["loop"] = self.loop.model_dump()
        return data


def estimate_itch_hours(text: str) -> float | None:
    """Estimate average playtime in hours from free-form text."""
    if not text:
        return None

    estimates: list[float] = []
    for match in _DURATION_RE.finditer(text):
        low_raw, high_raw, unit_raw = match.groups()
        low = float(low_raw.replace(",", "."))
        high = float(high_raw.replace(",", ".")) if high_raw else low
        if low <= 0 or high <= 0:
            continue

        value = (low + high) / 2.0
        unit = unit_raw.lower()
        if unit.startswith("m"):
            value /= 60.0
        estimates.append(value)

    if not estimates:
        return None
    return round(sum(estimates) / len(estimates), 2)


def _build_catalog_url(tag: str, sort: str) -> str:
    clean_tag = tag.strip().lstrip("#")
    clean_sort = sort.strip()
    if not clean_sort or clean_sort == "popular":
        return f"{ITCH_BASE_URL}/games/tag-{clean_tag}"
    return f"{ITCH_BASE_URL}/games/{clean_sort}/tag-{clean_tag}"


def _parse_game_cell(cell: Any) -> ItchGame | None:
    title_link = cell.select_one("a.title.game_link")
    if title_link is None:
        return None

    title = title_link.get_text(strip=True)
    url = str(title_link.get("href") or "").strip()
    if not title or not url:
        return None

    author_el = cell.select_one(".game_author a")
    author = author_el.get_text(strip=True) if author_el else ""

    genre_el = cell.select_one(".game_genre")
    genre = genre_el.get_text(strip=True) if genre_el else ""

    price_el = cell.select_one(".price_value")
    price_text = price_el.get_text(strip=True) if price_el else ""
    price = price_text or "Free"

    has_web_flag = cell.select_one(".web_flag") is not None
    platforms: list[str] = []
    for span in cell.select(".game_platform span"):
        classes = span.get("class") or []
        if "web_flag" in classes:
            has_web_flag = True
            continue
        label = str(span.get("title") or span.get_text(strip=True) or "").strip()
        if label:
            platforms.append(label)

    is_downloadable = len(platforms) > 0
    is_browser = has_web_flag and not is_downloadable

    return ItchGame(
        title=title,
        url=url,
        author=author,
        genre=genre,
        price=price,
        platforms=platforms,
        is_browser=is_browser,
        is_downloadable=is_downloadable,
    )


def _matches_filters(
    game: ItchGame,
    *,
    browser_only: bool | None,
    free_only: bool,
    exclude_demos: bool = True,
) -> bool:
    if exclude_demos and is_demo_title(game.title):
        return False
    if browser_only is False and not game.is_downloadable:
        return False
    if browser_only is True and not game.is_browser:
        return False
    return not (free_only and game.price.strip().lower() != "free")


def _make_client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0),
        headers=_HEADERS,
        follow_redirects=True,
        transport=transport,
    )


async def search_itch_games(
    tag: str = "incremental",
    sort: str = "popular",
    browser_only: bool | None = None,
    free_only: bool = False,
    max_pages: int = 1,
    transport: httpx.AsyncBaseTransport | None = None,
    exclude_demos: bool = True,
) -> list[ItchGame]:
    """Fetch and filter itch.io catalog games by tag and sort order."""
    if max_pages < 1:
        return []

    base_url = _build_catalog_url(tag, sort)
    games: list[ItchGame] = []

    async with _make_client(transport) as client:
        for page in range(1, max_pages + 1):
            params = {"page": page} if page > 1 else None
            try:
                response = await client.get(base_url, params=params)
                response.raise_for_status()
            except httpx.HTTPError:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            cells = soup.select(".game_cell")
            if not cells:
                break

            for cell in cells:
                parsed = _parse_game_cell(cell)
                if parsed is not None and _matches_filters(
                    parsed,
                    browser_only=browser_only,
                    free_only=free_only,
                    exclude_demos=exclude_demos,
                ):
                    games.append(parsed)

    return games


async def analyze_itch_game(
    url: str,
    title: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> ItchGame:
    """Fetch an itch.io game page and run anti-grind analysis on its description and comments."""
    try:
        async with _make_client(transport) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return ItchGame(
            title=title,
            url=url,
            warnings=[f"Failed to fetch itch.io page: {exc}"],
        )

    soup = BeautifulSoup(response.text, "html.parser")

    resolved_title = title.strip()
    if not resolved_title:
        h1 = soup.select_one("h1.game_title")
        if h1:
            resolved_title = h1.get_text(strip=True)
        elif soup.title:
            resolved_title = soup.title.get_text(strip=True)

    desc_el = soup.select_one(".formatted_description")
    description = desc_el.get_text(separator=" ", strip=True) if desc_el else ""

    comments = [
        post.get_text(separator=" ", strip=True)
        for post in soup.select(".community_post .post_body")[:MAX_COMMENTS]
    ]
    comments = [c for c in comments if c]

    reviews = [
        Review(id=f"itch-comment-{idx}", text=comment, positive=True)
        for idx, comment in enumerate(comments, start=1)
    ]
    synthetic_game = Game(
        appid=1,
        name=resolved_title,
        description=description,
        reviews=reviews,
    )
    report = Analyzer(AnalyzerConfig()).analyze(synthetic_game)
    loop = extract_loop(synthetic_game)

    combined_text = "\n".join([description, *comments])
    estimated_hours = estimate_itch_hours(combined_text)
    if estimated_hours is None:
        duration_source = "unknown"
    elif estimate_itch_hours(description) is not None:
        duration_source = "description"
    else:
        duration_source = "comments"

    return ItchGame(
        title=resolved_title,
        url=url,
        description=description,
        comments_count=len(comments),
        estimated_hours=estimated_hours,
        duration_source=duration_source,
        gti_score=report.gti if report.gti is not None else 0.0,
        verdict=report.verdict,
        warnings=list(report.warnings),
        loop=loop,
    )
