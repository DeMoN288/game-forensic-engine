from __future__ import annotations

import json

import httpx
import pytest

from game_forensic import mcp_server
from game_forensic.itch import (
    ItchGame,
    analyze_itch_game,
    estimate_itch_hours,
    search_itch_games,
)

CATALOG_HTML = """
<html>
  <body>
    <div class="game_grid_widget">
      <div class="game_cell">
        <a class="title game_link" href="https://dev1.itch.io/pocket-miner">Pocket Miner</a>
        <div class="game_author"><a href="https://dev1.itch.io">Dev One</a></div>
        <div class="game_genre">Simulation</div>
        <div class="game_platform">
          <span title="Download for Windows" class="icon-windows8">Windows</span>
          <span title="Download for Linux" class="icon-tux">Linux</span>
        </div>
      </div>
      <div class="game_cell">
        <a class="title game_link" href="https://dev2.itch.io/web-clicker">Web Clicker</a>
        <div class="game_author"><a href="https://dev2.itch.io">Dev Two</a></div>
        <div class="game_genre">Incremental</div>
        <div class="game_platform">
          <span class="web_flag">Play in browser</span>
        </div>
      </div>
      <div class="game_cell">
        <a class="title game_link" href="https://dev3.itch.io/paid-factory">Paid Factory</a>
        <div class="game_author"><a href="https://dev3.itch.io">Dev Three</a></div>
        <div class="game_genre">Strategy</div>
        <div class="price_value">$4.99</div>
        <div class="game_platform">
          <span title="Download for Windows" class="icon-windows8">Windows</span>
        </div>
      </div>
    </div>
  </body>
</html>
"""

GAME_PAGE_HTML = """
<html>
  <head><title>Pocket Miner by Dev One</title></head>
  <body>
    <h1 class="game_title">Pocket Miner</h1>
    <div class="formatted_description">
      Cozy incremental mining game with satisfying upgrades and zero microtransactions.
      Short campaign that takes about 2 hours to complete.
    </div>
    <div class="community_post">
      <div class="post_body">Great pacing, beat it in 90 minutes without any tedious grinding!</div>
    </div>
    <div class="community_post">
      <div class="post_body">Love the upgrades and responsive controls.</div>
    </div>
  </body>
</html>
"""


@pytest.mark.asyncio
async def test_search_itch_games_parsing_and_filters() -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, text=CATALOG_HTML)

    transport = httpx.MockTransport(handler)

    all_games = await search_itch_games(
        tag="incremental",
        sort="popular",
        browser_only=None,
        free_only=False,
        transport=transport,
    )
    assert len(all_games) == 3
    assert requested_urls[0] == "https://itch.io/games/tag-incremental"

    downloadable_only = await search_itch_games(
        tag="incremental",
        sort="top-rated",
        browser_only=False,
        free_only=False,
        transport=transport,
    )
    assert requested_urls[1] == "https://itch.io/games/top-rated/tag-incremental"
    assert [g.title for g in downloadable_only] == ["Pocket Miner", "Paid Factory"]
    assert all(g.is_downloadable and not g.is_browser for g in downloadable_only)

    free_downloadable = await search_itch_games(
        tag="incremental",
        browser_only=False,
        free_only=True,
        transport=transport,
    )
    assert [g.title for g in free_downloadable] == ["Pocket Miner"]
    assert free_downloadable[0].author == "Dev One"
    assert free_downloadable[0].genre == "Simulation"
    assert free_downloadable[0].price == "Free"
    assert free_downloadable[0].platforms == ["Download for Windows", "Download for Linux"]


def test_estimate_itch_hours_minutes_and_hours() -> None:
    assert estimate_itch_hours("Short experience, takes about 30 minutes.") == 0.5
    assert estimate_itch_hours("Playtime is 45-75 mins depending on pace.") == 1.0
    assert estimate_itch_hours("Expect 2-4 hours for a full run.") == 3.0
    assert estimate_itch_hours("Finished in 1.5 hrs.") == 1.5
    assert estimate_itch_hours("No duration mentioned here.") is None


@pytest.mark.asyncio
async def test_analyze_itch_game_description_and_comments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://dev1.itch.io/pocket-miner"
        return httpx.Response(200, text=GAME_PAGE_HTML)

    result = await analyze_itch_game(
        "https://dev1.itch.io/pocket-miner",
        transport=httpx.MockTransport(handler),
    )

    assert result.title == "Pocket Miner"
    assert result.comments_count == 2
    # Description mentions 2 hours, comment mentions 90 minutes (1.5 h) -> avg 1.75 h
    assert result.estimated_hours == 1.75
    assert result.duration_source == "description"
    assert result.gti_score >= 0.0
    assert result.verdict != ""


@pytest.mark.asyncio
async def test_mcp_itch_search_by_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    cards = [
        ItchGame(
            title="Too Long",
            url="https://dev.itch.io/too-long",
            author="A",
            genre="RPG",
            price="Free",
            platforms=["Windows"],
            is_downloadable=True,
            is_browser=False,
        ),
        ItchGame(
            title="Unknown Duration",
            url="https://dev.itch.io/unknown",
            author="B",
            genre="Puzzle",
            price="$2.00",
            platforms=["Linux"],
            is_downloadable=True,
            is_browser=False,
        ),
        ItchGame(
            title="Short High Grind",
            url="https://dev.itch.io/short-high",
            author="C",
            genre="Idle",
            price="Free",
            platforms=["Windows"],
            is_downloadable=True,
            is_browser=False,
        ),
        ItchGame(
            title="Short Low Grind",
            url="https://dev.itch.io/short-low",
            author="D",
            genre="Incremental",
            price="Free",
            platforms=["Windows"],
            is_downloadable=True,
            is_browser=False,
        ),
    ]

    analyzed_map = {
        "https://dev.itch.io/too-long": ItchGame(
            title="Too Long",
            url="https://dev.itch.io/too-long",
            estimated_hours=5.0,
            gti_score=5.0,
            verdict="NO_RED_FLAG_DETECTED",
            comments_count=4,
        ),
        "https://dev.itch.io/unknown": ItchGame(
            title="Unknown Duration",
            url="https://dev.itch.io/unknown",
            estimated_hours=None,
            gti_score=1.0,
            verdict="INSUFFICIENT_DATA",
            comments_count=0,
        ),
        "https://dev.itch.io/short-high": ItchGame(
            title="Short High Grind",
            url="https://dev.itch.io/short-high",
            estimated_hours=2.0,
            gti_score=42.0,
            verdict="MODERATE_GRIND",
            comments_count=3,
        ),
        "https://dev.itch.io/short-low": ItchGame(
            title="Short Low Grind",
            url="https://dev.itch.io/short-low",
            estimated_hours=1.5,
            gti_score=10.0,
            verdict="NO_RED_FLAG_DETECTED",
            comments_count=6,
        ),
    }

    async def fake_search_itch_games(**kwargs) -> list[ItchGame]:
        return cards

    async def fake_analyze_itch_game(url: str, title: str = "") -> ItchGame:
        return analyzed_map[url]

    monkeypatch.setattr(mcp_server, "search_itch_games", fake_search_itch_games)
    monkeypatch.setattr(mcp_server, "analyze_itch_game", fake_analyze_itch_game)

    raw = await mcp_server.itch_search_by_tag(
        tag="incremental",
        sort="popular",
        max_hours=3.0,
        browser_only=False,
        free_only=False,
        limit=10,
    )
    payload = json.loads(raw)

    assert payload["tag"] == "incremental"
    assert payload["count"] == 3
    assert [item["title"] for item in payload["items"]] == [
        "Short Low Grind",
        "Short High Grind",
        "Unknown Duration",
    ]
    first = payload["items"][0]
    assert first["author"] == "D"
    assert first["genre"] == "Incremental"
    assert first["price"] == "Free"
    assert first["platforms"] == ["Windows"]
    assert first["is_downloadable"] is True
    assert first["is_browser"] is False
    assert first["gti_score"] == 10.0
    assert first["comments_count"] == 6


@pytest.mark.asyncio
async def test_mcp_itch_analyze_game(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = ItchGame(
        title="Pocket Miner",
        url="https://dev1.itch.io/pocket-miner",
        description="Cozy mining game.",
        comments_count=2,
        estimated_hours=1.5,
        duration_source="description",
        gti_score=8.5,
        verdict="NO_RED_FLAG_DETECTED",
    )

    async def fake_analyze_itch_game(url: str, title: str = "") -> ItchGame:
        assert url == "https://dev1.itch.io/pocket-miner"
        return expected

    monkeypatch.setattr(mcp_server, "analyze_itch_game", fake_analyze_itch_game)

    raw = await mcp_server.itch_analyze_game("  https://dev1.itch.io/pocket-miner  ")
    payload = json.loads(raw)
    assert payload == expected.as_dict()
