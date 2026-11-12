import json
from pathlib import Path

import pytest

from game_forensic import mcp_server
from game_forensic.models import Game, Review


def _sample_game(appid: int, name: str, description: str, review_text: str) -> Game:
    reviews = [
        Review(
            id=f"{appid}-r{i}",
            text=review_text,
            language="english",
            positive=True,
            minutes_at_review=120,
        )
        for i in range(25)
    ]
    return Game(appid=appid, name=name, description=description, reviews=reviews)


def _patch_mcp_network(monkeypatch: pytest.MonkeyPatch, games: dict[int, Game]) -> None:
    class FakeTransport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 105600, "name": "Terraria"}]}

        async def get(self, url: str, params: dict | None = None) -> str:
            return ""

    class FakePipeline:
        def __init__(self, transport, config):
            self.transport = transport
            self.config = config

        async def collect(self, appid: int, discover: bool = False) -> Game:
            game = games[appid]
            if discover:
                candidates = [gid for gid in games if gid != appid]
                return game.model_copy(update={"candidate_ids": candidates})
            return game

    monkeypatch.setattr(mcp_server, "Transport", FakeTransport)
    monkeypatch.setattr(mcp_server, "Pipeline", FakePipeline)


@pytest.mark.asyncio
async def test_mcp_steam_analyze_game(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    _patch_mcp_network(
        monkeypatch,
        {
            105600: _sample_game(
                105600,
                "Terraria",
                "Cozy, relaxing sandbox with skill trees and upgrades. No timers.",
                "Beat the game in one sitting, responsive controls and fun upgrades.",
            )
        },
    )

    res = json.loads(await mcp_server.steam_analyze_game(105600, review_pages=1))
    assert res["appid"] == 105600
    assert res["title"] == "Terraria"
    assert res["verdict"] == "NO_RED_FLAG_DETECTED"
    assert res["estimated_completion_hours"] == 2.0


@pytest.mark.asyncio
async def test_mcp_steam_match_backlog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    _patch_mcp_network(
        monkeypatch,
        {
            105600: _sample_game(
                105600,
                "Terraria",
                "Relaxing sandbox with upgrades, responsive controls, no timers, story-rich.",
                "Relaxing game with oddly satisfying progression, great upgrades, no timers, story-rich.",
            )
        },
    )

    res = json.loads(await mcp_server.steam_match_backlog(105600, 105600))
    assert res["reference_game"]["appid"] == 105600
    assert res["candidate_game"]["appid"] == 105600
    assert res["recommendation_verdict"] == "HIGH_SYNERGY_RECOMMENDED"


@pytest.mark.asyncio
async def test_mcp_steam_batch_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    _patch_mcp_network(
        monkeypatch,
        {
            105600: _sample_game(
                105600,
                "Terraria",
                "Sandbox with upgrades and abilities.",
                "Great pacing and fun progression!",
            )
        },
    )

    res = json.loads(await mcp_server.steam_batch_filter([105600], max_grind=40))
    assert res["approved_count"] == 1
    assert res["approved_backlog"][0]["appid"] == 105600


@pytest.mark.asyncio
async def test_mcp_steam_recommend_and_profile_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    _patch_mcp_network(
        monkeypatch,
        {
            100: _sample_game(
                100,
                "Ref Game",
                "Relaxing game with upgrades, responsive controls, no timers, story-rich.",
                "Oddly satisfying loop and great upgrades.",
            ),
            200: _sample_game(
                200,
                "Candidate Game",
                "Relaxing game with upgrades, responsive controls, no timers, story-rich.",
                "Very satisfying loop and fun upgrades.",
            ),
        },
    )

    prof_res = json.loads(
        await mcp_server.steam_profile_set(100, {"combat_pace": 0.2, "narrative_weight": 0.8})
    )
    assert prof_res["status"] == "saved"
    assert prof_res["axes"]["combat_pace"] == 0.2

    rec_res = json.loads(await mcp_server.steam_recommend(100, max_candidates=5))
    assert rec_res["reference"] == 100
    assert len(rec_res["ranked"]) == 1
    assert rec_res["ranked"][0]["appid"] == 200
    assert rec_res["ranked"][0]["verdict"] == "NO_RED_FLAG_DETECTED"
