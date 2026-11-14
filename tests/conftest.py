"""Shared offline fixtures.

Everything here is a factory or an offline double, so test modules never import
each other: pytest injects fixtures regardless of the test import mode, whereas
`from test_matcher import ...` only worked under the default prepend mode.
"""
import asyncio

import pytest

from game_forensic import mcp_server
from game_forensic.matcher import AXES
from game_forensic.models import CoreLoop, Feature, Game, GrindReport, Review
from game_forensic.network import SourceError
from game_forensic.pipeline import PipelineConfig


@pytest.fixture
def loop():
    def _loop(value=0.7, confidence=1, axes=AXES):
        return CoreLoop(features={axis: Feature(value=value, confidence=confidence) for axis in axes})

    return _loop


@pytest.fixture
def game(loop):
    def _game(appid, text="great game", candidates=(), features=True):
        return Game(appid=appid, name=f"Game {appid}", candidate_ids=list(candidates),
                    loop=loop() if features else CoreLoop(),
                    reviews=[Review(id=str(i), text=text, positive=False) for i in range(30)])

    return _game


@pytest.fixture
def make_reviews():
    """Negative reviews by default: the analyzer's GTI denominator is negative reviews only."""
    def _make_reviews(count, text="great game", minutes=120, positive=True, language="english", appid=0):
        return [Review(id=f"{appid}-r{i}", text=text, positive=positive, language=language,
                       minutes_at_review=minutes)
                for i in range(count)]

    return _make_reviews


@pytest.fixture
def make_report():
    """GrindReport factory; `make_report(...)` and `make_report(verdict, gti, category=rate)` both work."""
    def _make_report(verdict="NO_RED_FLAG_DETECTED", gti=0.0, sample_size=30, **categories) -> GrindReport:
        return GrindReport(gti=gti, verdict=verdict, confidence=1.0, sample_size=sample_size,
                           complaint_count=0, categories=categories, early_cluster={},
                           evidence=[], warnings=[])

    return _make_report


@pytest.fixture
def sample_game(make_reviews):
    def _sample_game(appid: int, name: str, description: str, review_text: str) -> Game:
        return Game(appid=appid, name=name, description=description,
                    reviews=make_reviews(25, review_text, minutes=120, appid=appid))

    return _sample_game


@pytest.fixture
def FakeTransport():
    class FakeTransport:
        def __init__(self, html="", fail=False):
            self.html, self.fail = html, fail

        async def get(self, url, params=None):
            if self.fail:
                raise SourceError("offline")
            return self.html

    return FakeTransport


@pytest.fixture
def FakePipeline(FakeTransport):
    class FakePipeline:
        def __init__(self, games, transport=None, errors=(), delay=0):
            self.games, self.transport = games, transport or FakeTransport()
            self.errors, self.delay = set(errors), delay
            self.config = PipelineConfig()
            self.calls, self.active, self.peak = [], 0, 0

        async def collect(self, appid, discover=False):
            self.calls.append((appid, discover))
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                if self.delay:
                    await asyncio.sleep(self.delay)
                if appid in self.errors:
                    raise SourceError("offline")
                return self.games[appid].model_copy(deep=True)
            finally:
                self.active -= 1

    return FakePipeline


@pytest.fixture
def patch_mcp_network(sample_game):
    """Replace the MCP server's transport/pipeline with offline doubles for `games`."""
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

    return _patch_mcp_network
