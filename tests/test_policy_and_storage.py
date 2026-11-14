"""Regression tests for the shared safety gate, persistence validation, and reporting.

Each test pins a defect that was reproduced before the fix.
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from game_forensic import mcp_server
from game_forensic.matcher import (
    AXES,
    COMPILED_CUES,
    CUES,
    DEFAULT_BLACKLIST,
    Matcher,
    MatcherConfig,
    rejection_reason,
)
from game_forensic.models import Game, Match, Review
from game_forensic.policy import SafetyPolicy, safety_gate
from game_forensic.storage import (
    load_user_data,
    parse_blacklist,
    parse_profiles,
    remove_blacklist,
    set_blacklist,
    set_profile,
)

# --- policy: one rule set for every consumer -------------------------------------

def test_gated_category_blocks_matches_matcher_and_batch_filter(make_report):
    """CAUTION + pay_to_skip must be rejected identically everywhere."""
    report = make_report("CAUTION", 18.08, pay_to_skip=0.2333, repetition=0.1)
    decision = safety_gate(report, SafetyPolicy(max_gti=35.0))
    assert decision.allowed is False
    assert decision.code == "GATED_CATEGORY"
    assert rejection_reason(report) == decision.reason


def test_caution_without_gated_categories_is_allowed(make_report):
    report = make_report("CAUTION", 16.0, repetition=0.2)
    assert safety_gate(report, SafetyPolicy(max_gti=35.0)).allowed is True


def test_gate_applies_caller_limits_on_top_of_verdict(make_report):
    clean = make_report("NO_RED_FLAG_DETECTED", 50.0)
    assert safety_gate(clean, SafetyPolicy(max_gti=35.0)).code == "OVER_GTI"
    long_game = safety_gate(clean, SafetyPolicy(), completion_hours=120.0)
    assert long_game.allowed is True
    limited = safety_gate(clean, SafetyPolicy(max_completion_hours=10.0), completion_hours=120.0)
    assert limited.code == "OVER_LENGTH"


def test_gate_rejects_red_flag_and_insufficient_data(make_report):
    assert safety_gate(make_report("RED_FLAG_REJECT", 50.0, repetition=0.6)).code == "RED_FLAG_REJECT"
    assert safety_gate(make_report("INSUFFICIENT_DATA", 0.0, sample_size=3)).code == "INSUFFICIENT_DATA"
    assert safety_gate(make_report(gti=None)).code == "INSUFFICIENT_DATA"


# --- storage: hand-edited files degrade instead of crashing ----------------------

def test_profiles_typo_and_range_are_reported_not_fatal():
    warnings: list[str] = []
    parsed = parse_profiles(
        {"100": {"combat_pace": "fast", "genre": 0.5, "build_depth": 1.7, "narrative_weight": 0.4}},
        AXES, warnings)
    assert parsed == {100: {"narrative_weight": 0.4}}
    assert len(warnings) == 3
    assert any("не число" in w for w in warnings)
    assert any("неизвестная ось" in w for w in warnings)
    assert any("вне [0, 1]" in w for w in warnings)


def test_profiles_entry_with_no_valid_axes_is_dropped_with_warning():
    warnings: list[str] = []
    assert parse_profiles({"1": {"genre": 0.5}}, AXES, warnings) == {}
    assert any("не осталось валидных осей" in w for w in warnings)


def test_blacklist_null_cancels_builtin_seed_and_bad_type_is_reported():
    warnings: list[str] = []
    merged, unlisted = parse_blacklist({"1599340": None, "100": 5, "200": "  no upgrades  "},
                                       DEFAULT_BLACKLIST, warnings)
    assert 1599340 not in merged
    assert 200 in merged and merged[200] == "no upgrades"
    assert unlisted == {1599340}
    assert any("должна быть строкой" in w for w in warnings)


def test_load_user_data_merges_seed_and_reports_missing_files(tmp_path: Path):
    state = load_user_data(AXES, DEFAULT_BLACKLIST, directory=tmp_path)
    assert state.blacklist == DEFAULT_BLACKLIST
    assert state.warnings == []


def test_corrupt_blacklist_json_is_a_warning(tmp_path: Path):
    (tmp_path / "blacklist.json").write_text("{not json", encoding="utf-8")
    state = load_user_data(AXES, DEFAULT_BLACKLIST, directory=tmp_path)
    assert state.blacklist == DEFAULT_BLACKLIST
    assert any("не читается" in w for w in state.warnings)


def test_blacklist_remove_cancels_seed_even_after_re_adding(tmp_path: Path):
    assert remove_blacklist(tmp_path, 1599340, DEFAULT_BLACKLIST) is False
    assert 1599340 not in load_user_data(AXES, DEFAULT_BLACKLIST, directory=tmp_path).blacklist

    # Re-adding a seed makes it blacklisted again...
    set_blacklist(tmp_path, 1599340, "still bad")
    assert load_user_data(AXES, DEFAULT_BLACKLIST, directory=tmp_path).blacklist[1599340] == "still bad"

    # ...and removal must win over the built-in default once more.
    assert remove_blacklist(tmp_path, 1599340, DEFAULT_BLACKLIST) is False
    state = load_user_data(AXES, DEFAULT_BLACKLIST, directory=tmp_path)
    assert 1599340 not in state.blacklist
    assert state.unblacklisted == {1599340}


def test_blacklist_remove_of_non_seed_deletes_the_key(tmp_path: Path):
    set_blacklist(tmp_path, 777, "no upgrades")
    assert remove_blacklist(tmp_path, 777, DEFAULT_BLACKLIST) is False
    assert json.loads((tmp_path / "blacklist.json").read_text(encoding="utf-8")) == {}


def test_set_profile_merges_partial_update(tmp_path: Path):
    set_profile(tmp_path, 100, {"combat_pace": 0.2})
    merged = set_profile(tmp_path, 100, {"narrative_weight": 0.8})
    assert merged == {"combat_pace": 0.2, "narrative_weight": 0.8}


# --- matcher: validated candidates are always accounted for ----------------------

async def test_candidates_beyond_top_k_are_excluded_with_a_reason(loop, make_reviews, FakePipeline):
    games = {1: Game(appid=1, name="Ref", loop=loop(), candidate_ids=list(range(2, 12)),
                     reviews=make_reviews(30, positive=False))}
    for i in range(2, 12):
        games[i] = Game(appid=i, name=f"G{i}", loop=loop(), reviews=make_reviews(30, positive=False))
    report = await Matcher(FakePipeline(games), config=MatcherConfig(top_k=5)).recommend(1)

    assert len(report.ranked) == 5
    accounted = {r.appid for r in report.ranked} | set(map(int, report.excluded))
    assert accounted == set(report.raw_candidates)
    for appid in range(7, 12):
        assert "топ-5" in report.excluded[str(appid)]


async def test_batch_filter_reports_skipped_and_unresolved_counts(monkeypatch, tmp_path: Path, make_reviews):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    good = Game(appid=100, name="Good", reviews=make_reviews(25, "great pacing", appid=100, positive=False))

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 100, "name": "Good"}]}

    class _Pipeline:
        def __init__(self, transport, config):
            pass

        async def collect(self, appid: int, discover: bool = False) -> Game:
            return good

    monkeypatch.setattr(mcp_server, "Transport", _Transport)
    monkeypatch.setattr(mcp_server, "Pipeline", _Pipeline)

    res = json.loads(await mcp_server.steam_batch_filter([100] * 11, max_grind=40.0))
    assert res["total_requested"] == 11
    assert res["skipped_over_limit"] == 3
    assert any("лимиту" in w for w in res["warnings"])


async def test_unsupported_language_request_is_reported(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    german = [Review(id=f"g{i}", text="Sehr grindy und repetitiv", positive=False,
                     language="german", minutes_at_review=100) for i in range(60)]
    game = Game(appid=500, name="German Game", reviews=german)

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 500, "name": "German Game"}]}

    class _Pipeline:
        def __init__(self, transport, config):
            pass

        async def collect(self, appid: int, discover: bool = False) -> Game:
            return game

    monkeypatch.setattr(mcp_server, "Transport", _Transport)
    monkeypatch.setattr(mcp_server, "Pipeline", _Pipeline)

    res = json.loads(await mcp_server.steam_analyze_game(500, review_pages=1, languages=["german"]))
    assert res["sample_size"] == 0
    assert any("german" in w for w in res["warnings"])
    assert any("Ни один запрошенный язык" in w for w in res["warnings"])


# --- mcp: synergy verdict can never exceed the shared gate -----------------------

@pytest.mark.parametrize("verdict,gti,expected", [
    ("NO_RED_FLAG_DETECTED", 10.0, "HIGH_SYNERGY_RECOMMENDED"),
    ("CAUTION", 18.0, "HIGH_SYNERGY_RECOMMENDED"),
    ("INSUFFICIENT_DATA", 0.0, "INSUFFICIENT_DATA"),
    ("NO_RED_FLAG_DETECTED", 45.0, "DIVERGENT_OR_HIGH_GRIND"),
])
def test_synergy_verdict_respects_gate(make_report, verdict, gti, expected):
    match = Match(score=0.9, coverage=1.0, vector_similarity=1.0, common_dimensions=list(AXES))
    report = make_report(verdict, gti, repetition=0.1)
    assert mcp_server.synergy_verdict(match, report) == expected


def test_synergy_verdict_blocked_by_gated_category_or_blacklist(make_report):
    match = Match(score=0.9, coverage=1.0, vector_similarity=1.0, common_dimensions=list(AXES))
    blocked = make_report("CAUTION", 18.0, pay_to_skip=0.3)
    assert mcp_server.synergy_verdict(match, blocked) == "DIVERGENT_OR_HIGH_GRIND"
    assert mcp_server.synergy_verdict(match, make_report(), blocked=True) == "BLACKLISTED"


def test_synergy_verdict_uses_configured_coverage_floor(make_report):
    match = Match(score=0.9, coverage=0.5, vector_similarity=1.0, common_dimensions=list(AXES))
    # 0.5 is above the engine default floor, so the candidate may be recommended.
    assert MatcherConfig().min_coverage == 0.4
    assert mcp_server.synergy_verdict(match, make_report()) == "HIGH_SYNERGY_RECOMMENDED"
    # A stricter caller-supplied floor must be honoured instead of the default.
    assert mcp_server.synergy_verdict(match, make_report(), min_coverage=0.6) == "INSUFFICIENT_DATA"
    below_default = Match(score=0.9, coverage=0.3, vector_similarity=1.0,
                          common_dimensions=list(AXES))
    assert mcp_server.synergy_verdict(below_default, make_report()) == "INSUFFICIENT_DATA"


async def test_analyze_game_surfaces_profile_load_warnings(monkeypatch, tmp_path: Path, make_reviews):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    (tmp_path / "profiles.json").write_text(json.dumps({"100": {"combat_pace": "fast"}}),
                                            encoding="utf-8")
    game = Game(appid=100, name="X", reviews=make_reviews(25, "great pacing", appid=100, positive=False))

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 100, "name": "X"}]}

    class _Pipeline:
        def __init__(self, transport, config):
            pass

        async def collect(self, appid: int, discover: bool = False) -> Game:
            return game

    monkeypatch.setattr(mcp_server, "Transport", _Transport)
    monkeypatch.setattr(mcp_server, "Pipeline", _Pipeline)

    res = json.loads(await mcp_server.steam_analyze_game(100, review_pages=1))
    assert any("profiles.json" in w for w in res["warnings"])


def test_blacklist_add_and_remove_round_trip(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 1599340, "name": "Lost Ark"}]}

    monkeypatch.setattr(mcp_server, "Transport", _Transport)

    async def scenario():
        added = json.loads(await mcp_server.steam_blacklist_add("Lost Ark", "P2W"))
        assert added["status"] == "added"
        removed = json.loads(await mcp_server.steam_blacklist_remove("Lost Ark"))
        assert removed["status"] == "removed"
        blacklist, profiles, _ = mcp_server.user_state()
        assert 1599340 not in blacklist
        assert profiles == {}

    asyncio.run(scenario())


def test_precompiled_cues_match_previous_inline_patterns():
    """Compiled cue tables must preserve the original word-boundary semantics."""
    assert set(COMPILED_CUES) == set(CUES)
    for axis, poles in CUES.items():
        assert len(COMPILED_CUES[axis]) == len(poles)
        for pole, compiled in zip(poles, COMPILED_CUES[axis], strict=True):
            assert compiled.pattern == r"\b(?:" + pole + r")\b"
            assert compiled.flags & re.IGNORECASE
