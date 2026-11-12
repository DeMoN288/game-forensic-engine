import json
from pathlib import Path

import pytest

from game_forensic import mcp_server
from game_forensic.analyzer import Analyzer
from game_forensic.matcher import estimate_completion_hours, extract_loop, match_loops
from game_forensic.models import Game, Review


def _make_reviews(count: int, text: str = "Fun game!", minutes: int = 120) -> list[Review]:
    return [
        Review(
            id=f"r{i}",
            text=text,
            language="english",
            positive=True,
            minutes_at_review=minutes,
        )
        for i in range(count)
    ]


def test_negation_window_handles_multiword_adjectives():
    analyzer = Analyzer()
    review = Review(
        id="r1",
        text="Great pacing with no tedious late-game grind at all.",
        language="english",
        positive=False,
    )
    assert analyzer.find_patterns(review) == []


def test_shallow_progression_warning_detected():
    reviews = _make_reviews(20, "Fun concept but no upgrades and nothing to unlock.")
    game = Game(appid=1, name="Shallow", reviews=reviews)
    report = Analyzer().analyze(game)
    assert any("Shallow progression" in w for w in report.warnings)


def test_extract_loop_supports_cozy_incremental_cues():
    hammer = Game(
        appid=5229450,
        name="Hammer these nails!",
        description="Cozy, relaxing physics-based game with upgrades and abilities. No stress, no timers.",
        reviews=_make_reviews(5, "Oddly satisfying hammering and fun upgrades!"),
    )
    dismantle = Game(
        appid=4043010,
        name="Just Dismantle",
        description="Relaxing dismantling with upgrades and very satisfying physics.",
        reviews=_make_reviews(5, "Cozy, no timers, satisfying upgrades."),
    )
    loop_a = extract_loop(hammer)
    loop_b = extract_loop(dismantle)
    match = match_loops(loop_a, loop_b)
    assert len(match.common_dimensions) >= 4
    assert match.coverage >= 0.8
    assert match.vector_similarity >= 0.9
    assert match.score >= 0.4


def test_estimate_completion_hours_median():
    reviews = [
        Review(id="1", text="Beat the game in one sitting!", language="english", positive=True, minutes_at_review=90),
        Review(id="2", text="Completed 100% achievements.", language="english", positive=True, minutes_at_review=120),
        Review(id="3", text="Just started playing.", language="english", positive=True, minutes_at_review=15),
    ]
    game = Game(appid=1, name="Short Game", reviews=reviews)
    assert estimate_completion_hours(game) == 1.8


@pytest.mark.asyncio
async def test_mcp_batch_filter_rejects_insufficient_data_and_blacklisted(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    (tmp_path / "blacklist.json").write_text(json.dumps({"999": "No upgrades"}), encoding="utf-8")

    class FakePipeline:
        def __init__(self, transport, config):
            pass

        async def collect(self, appid: int, discover: bool = False) -> Game:
            if appid == 100:
                return Game(appid=100, name="Unverified Game", reviews=[])
            return Game(
                appid=200,
                name="Good Short Game",
                reviews=_make_reviews(25, "Completed in one sitting, great game!", minutes=120),
            )

    monkeypatch.setattr(mcp_server, "Pipeline", FakePipeline)

    raw = await mcp_server.steam_batch_filter([999, 100, 200], max_grind=35.0, max_completion_hours=3.0)
    data = json.loads(raw)

    assert data["approved_count"] == 1
    assert data["approved_backlog"][0]["appid"] == 200
    assert data["approved_backlog"][0]["estimated_completion_hours"] == 2.0

    rejected_ids = {item["appid"]: item for item in data["rejected_grind_traps"]}
    assert rejected_ids[999]["verdict"] == "BLACKLISTED"
    assert rejected_ids[100]["verdict"] == "INSUFFICIENT_DATA"
    assert rejected_ids[100]["passed"] is False


@pytest.mark.asyncio
async def test_mcp_blacklist_add_and_search_games(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))

    class FakeTransport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {
                "items": [
                    {"id": 111, "name": "Cool Game Soundtrack", "price": {"final": 499, "currency": "USD"}},
                    {"id": 222, "name": "Cool Game", "price": {"final": 999, "currency": "USD"}},
                ]
            }

    monkeypatch.setattr(mcp_server, "Transport", FakeTransport)

    search_res = json.loads(await mcp_server.steam_search_games("Cool Game", limit=5))
    assert search_res["count"] == 1
    assert search_res["items"][0]["appid"] == 222

    bl_res = json.loads(await mcp_server.steam_blacklist_add("Cool Game", "No upgrades"))
    assert bl_res["appid"] == 222
    saved = json.loads((tmp_path / "blacklist.json").read_text(encoding="utf-8"))
    assert saved["222"] == "No upgrades"


def test_extract_loop_uses_steam_category_metadata_fallback():
    game = Game(
        appid=777,
        name="Chill Toy",
        description="Physics toy with upgrades.",
        metadata={
            "categories": [
                {"description": "#category_playable_at_your_own_pace"},
                {"description": "Playable without Timed Input"},
                {"description": "Save Anytime"},
            ]
        },
        reviews=_make_reviews(5, "Fun upgrades!"),
    )
    loop = extract_loop(game)
    assert loop.features["combat_pace"].value == 0.15
    assert loop.features["combat_pace"].confidence == 0.45
    assert loop.features["punishment_severity"].value == 0.15
    assert loop.features["punishment_severity"].confidence == 0.45
