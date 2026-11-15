"""Offline tests for rejection feedback learning and mechanic presets.

Covers `record_rejection` / `load_rejection_penalties`, `score_candidate_with_preset`,
and the MCP wiring: auto-learning on `steam_blacklist_add`, `steam_dislike_feedback`,
and `recommend_by_preset` re-ranking over mocked Steam/itch discovery.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from game_forensic import mcp_server
from game_forensic.feedback import (
    load_rejection_penalties,
    normalize_reason,
    record_rejection,
)
from game_forensic.itch import ItchGame, analyze_itch_game, search_itch_games
from game_forensic.matcher import match_loops
from game_forensic.models import CoreLoop, Feature, Game
from game_forensic.presets import PRESETS, get_preset, score_candidate_with_preset

# --- reason normalization & rejection recording ---------------------------------

def test_normalize_reason_maps_codes_and_free_text() -> None:
    assert normalize_reason("grind") == "grind"
    assert normalize_reason("копать и фарм") == "grind"
    assert normalize_reason("повтор, монотон") == "repetitive"
    assert normalize_reason("слишком сложн") == "too_hard"
    assert normalize_reason("затянут на много часов") == "too_long"
    assert normalize_reason("я уже играл") == "already_played"
    assert normalize_reason("нет особой причины") == "other"


def test_record_rejection_normalizes_and_persists(tmp_path: Path) -> None:
    entry = record_rejection(123, "гринд и фарм", tags=["Grindy", " Farm "], directory=tmp_path)
    assert entry["reason_code"] == "grind"
    assert entry["tags"] == ["farm", "grindy"]
    assert entry["note"] == "гринд и фарм"

    raw = json.loads((tmp_path / "feedback.json").read_text(encoding="utf-8"))
    assert raw["rejections"][-1]["appid"] == 123


def test_record_rejection_rejects_bad_input(tmp_path: Path) -> None:
    for bad_appid in (0, -1, True, "123"):
        with pytest.raises(ValueError):
            record_rejection(bad_appid, "grind", directory=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        record_rejection(1, 5, directory=tmp_path)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        record_rejection(1, "grind", tags=["a", 2], directory=tmp_path)  # type: ignore[list-item]


# --- penalty derivation -----------------------------------------------------------

def test_load_rejection_penalties_derives_cumulative_profile(tmp_path: Path) -> None:
    record_rejection(1, "grind", tags=["cozy"], directory=tmp_path)
    record_rejection(2, "repetitive", tags=["cozy", "roguelike"], directory=tmp_path)
    record_rejection(3, "too_hard", directory=tmp_path)
    record_rejection(4, "too_long", directory=tmp_path)
    record_rejection(5, "too_long", directory=tmp_path)

    penalties = load_rejection_penalties(directory=tmp_path)
    assert penalties["counts"] == {
        "grind": 1,
        "repetitive": 1,
        "too_hard": 1,
        "too_long": 2,
        "already_played": 0,
        "other": 0,
    }
    # grind + repetitive = 2 -> multiplier 1.0 + 2 * 0.25
    assert penalties["gti_penalty_multiplier"] == 1.5
    assert penalties["axis_penalties"]["combat_pace"] == 0.1
    assert penalties["axis_penalties"]["gti_weight"] == 0.2
    assert penalties["axis_penalties"]["punishment_severity"] == 0.15
    assert penalties["preferred_max_hours"] == 1.0
    assert penalties["tag_penalties"] == {"cozy": 0.1, "roguelike": 0.05}
    assert penalties["warnings"] == []


def test_load_rejection_penalties_ignores_malformed_entries(tmp_path: Path) -> None:
    (tmp_path / "feedback.json").write_text(
        json.dumps(
            {
                "rejections": [
                    {"appid": 10, "reason_code": "grind", "tags": []},
                    {"appid": "bad", "reason_code": "grind", "tags": []},
                    {"appid": -1, "reason_code": "grind", "tags": []},
                    {"appid": 11, "reason_code": "wat", "tags": []},
                    {"appid": 12, "reason_code": "grind", "tags": [1, 2]},
                    "garbage",
                    {"appid": 13, "reason_code": "grind", "tags": ["a"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    penalties = load_rejection_penalties(directory=tmp_path)
    assert penalties["counts"]["grind"] == 2
    assert penalties["counts"]["repetitive"] == 0
    assert len(penalties["warnings"]) == 5


# --- preset definitions & scoring ------------------------------------------------

def test_presets_cover_three_named_profiles() -> None:
    assert set(PRESETS) == {"perspective", "meta_narrative", "tactile_incremental"}
    assert get_preset("perspective").itch_tag == "puzzle"
    assert get_preset("meta_narrative").itch_tag == "experimental"
    assert get_preset("tactile_incremental").steam_tags == ("Incremental", "Idler")
    with pytest.raises(ValueError, match="Unknown preset"):
        get_preset("missing")


def _candidate(**features: float) -> CoreLoop:
    return CoreLoop(
        origin="heuristic",
        features={axis: Feature(value=value, confidence=1.0) for axis, value in features.items()},
    )


def test_score_candidate_with_preset_prefers_matching_loop() -> None:
    candidate = _candidate(tactile_feedback=0.95, punishment_severity=0.1)
    score, reasons = score_candidate_with_preset(
        "tactile_incremental", gti_score=0.0, loop=candidate, hours=1.0, max_hours=3.0,
        penalties={},
    )
    assert score > 0
    assert any("CoreLoop match" in r for r in reasons)


def test_score_candidate_with_preset_penalizes_gti() -> None:
    candidate = _candidate(tactile_feedback=0.95, punishment_severity=0.1)
    clean, _ = score_candidate_with_preset(
        "tactile_incremental", 0.0, candidate, 1.0, 3.0, {}
    )
    penalized, reasons = score_candidate_with_preset(
        "tactile_incremental", 40.0, candidate, 1.0, 3.0,
        {"gti_penalty_multiplier": 1.5},
    )
    assert penalized < clean
    assert any("GTI penalty" in r for r in reasons)


def test_score_candidate_with_preset_penalizes_preferred_length() -> None:
    candidate = _candidate(tactile_feedback=0.9)
    under, _ = score_candidate_with_preset(
        "tactile_incremental", 0.0, candidate, 2.0, 3.0, {}
    )
    over, reasons = score_candidate_with_preset(
        "tactile_incremental", 0.0, candidate, 2.0, 3.0, {"preferred_max_hours": 1.0}
    )
    assert over < under
    assert any("Length penalty" in r for r in reasons)


def test_score_candidate_with_preset_applies_learned_axis_and_tag() -> None:
    candidate = _candidate(tactile_feedback=0.95, punishment_severity=0.1)
    clean, _ = score_candidate_with_preset(
        "tactile_incremental", 0.0, candidate, 1.0, 3.0, {}
    )
    penalized, reasons = score_candidate_with_preset(
        "tactile_incremental",
        0.0,
        candidate,
        1.0,
        3.0,
        {"axis_penalties": {"punishment_severity": 0.5},
         "tag_penalties": {"cozy": 0.5}},
        tags=["Cozy"],
    )
    assert penalized < clean
    assert any("Learned axis punishment_severity" in r for r in reasons)
    assert any("Learned tag cozy" in r for r in reasons)


def test_score_candidate_with_preset_rejects_bad_input() -> None:
    candidate = _candidate(tactile_feedback=0.9)
    with pytest.raises(ValueError):
        score_candidate_with_preset("bogus", 0.0, candidate, 1.0, 3.0, {})
    for gti in (-1.0, 101.0):
        with pytest.raises(ValueError):
            score_candidate_with_preset("tactile_incremental", gti, candidate, 1.0, 3.0, {})
    with pytest.raises(ValueError):
        score_candidate_with_preset("tactile_incremental", 0.0, candidate, 1.0, 0.0, {})


# --- MCP: auto-learning on blacklist + dislike feedback --------------------------

@pytest.mark.asyncio
async def test_blacklist_add_auto_learns_rejection(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 123, "name": "Grindy Game"}]}

    monkeypatch.setattr(mcp_server, "Transport", _Transport)

    res = json.loads(await mcp_server.steam_blacklist_add(123, "слишком много гринда"))
    assert res["status"] == "added"
    assert res["learned_reason_code"] == "grind"
    assert load_rejection_penalties(directory=tmp_path)["counts"]["grind"] == 1


@pytest.mark.asyncio
async def test_dislike_feedback_records_and_returns_penalties(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def json(self, url: str, params: dict):
            return {"items": [{"id": 55, "name": "Long Game"}]}

    monkeypatch.setattr(mcp_server, "Transport", _Transport)

    res = json.loads(await mcp_server.steam_dislike_feedback(
        "Long Game", "слишком затянут", tags=["RPG"]
    ))
    assert res["status"] == "recorded"
    assert res["appid"] == 55
    assert res["reason_code"] == "too_long"
    assert res["penalties_applied"]["preferred_max_hours"] == 2.0
    assert res["penalties_applied"]["counts"]["too_long"] == 1


@pytest.mark.asyncio
async def test_dislike_feedback_rejects_empty_reason() -> None:
    with pytest.raises(ValueError, match="Reason"):
        await mcp_server.steam_dislike_feedback(1, "   ")


# --- MCP: recommend_by_preset over mocks -----------------------------------------

@pytest.mark.asyncio
async def test_recommend_by_preset_steam_ranks_and_reports_penalties(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patch_mcp_network, sample_game,
) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    patch_mcp_network(
        monkeypatch,
        {
            100: sample_game(100, "Short Tactile", "responsive controls, no stress",
                             "Beat it in one sitting, responsive controls"),
            200: sample_game(200, "Other Tactile", "responsive controls, no stress",
                             "Beat it in one sitting, responsive controls"),
        },
    )

    async def fake_search_by_tag(transport, tag, limit=10):
        return {"tag": tag, "tag_id": 1, "count": 2,
                "items": [{"appid": 100, "name": "Short Tactile"},
                          {"appid": 200, "name": "Other Tactile"}]}

    monkeypatch.setattr(mcp_server, "search_by_tag", fake_search_by_tag)

    res = json.loads(await mcp_server.recommend_by_preset(
        preset="tactile_incremental", source="steam", max_hours=3.0, limit=5
    ))
    assert res["preset"] == "tactile_incremental"
    assert res["preset_name"] == "Короткая тактильная инкременталка"
    assert set(res["penalties_applied"]) == {
        "axis_penalties", "tag_penalties", "gti_penalty_multiplier",
        "preferred_max_hours", "counts",
    }
    assert res["count"] == 2
    assert {item["source"] for item in res["items"]} == {"steam"}
    scores = [item["score"] for item in res["items"]]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_recommend_by_preset_excludes_blacklist_and_overlength(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patch_mcp_network, make_reviews,
) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    (tmp_path / "blacklist.json").write_text(json.dumps({"999": "already rejected"}),
                                             encoding="utf-8")

    games = {
        100: Game(appid=100, name="Short Tactile", description="responsive controls",
                  reviews=make_reviews(25, "Beat it in one sitting, responsive controls",
                                       minutes=120, appid=100, positive=False)),
        200: Game(appid=200, name="Very Long", description="responsive controls",
                  reviews=make_reviews(25, "Beat the game after many hours of grind",
                                       minutes=600, appid=200, positive=False)),
        999: Game(appid=999, name="Banned", description="responsive controls",
                  reviews=make_reviews(25, "Beat it in one sitting, responsive controls",
                                       minutes=120, appid=999, positive=False)),
    }
    patch_mcp_network(monkeypatch, games)

    async def fake_search_by_tag(transport, tag, limit=10):
        return {"tag": tag, "tag_id": 1, "count": 3,
                "items": [{"appid": 100, "name": "Short Tactile"},
                          {"appid": 200, "name": "Very Long"},
                          {"appid": 999, "name": "Banned"}]}

    monkeypatch.setattr(mcp_server, "search_by_tag", fake_search_by_tag)

    res = json.loads(await mcp_server.recommend_by_preset(
        preset="tactile_incremental", source="steam", max_hours=3.0, limit=5
    ))
    item_appids = {item["appid"] for item in res["items"]}
    assert item_appids == {100}
    reasons = {ex["appid"]: ex["reason"] for ex in res["excluded"]}
    assert reasons[200] == "OVER_LENGTH"
    assert reasons[999].startswith("BLACKLIST")


@pytest.mark.asyncio
async def test_recommend_by_preset_itch_scores_candidates(monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path, patch_mcp_network) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    patch_mcp_network(monkeypatch, {})

    card = ItchGame(title="Nail Hammer", url="https://dev.itch.io/nail-hammer", author="A",
                    genre="Incremental", price="Free", platforms=["Windows"],
                    is_downloadable=True, is_browser=False)
    page = """
        <div class="formatted_description">Responsive controls. Playtime: 1.5 hours.</div>
        <div class="community_post"><div class="post_body">No stress.</div></div>
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=page))
    analyzed = await analyze_itch_game(card.url, title=card.title, transport=transport)

    async def fake_search_itch_games(**kwargs):
        return [card]

    async def fake_analyze_itch_game(url: str, title: str = ""):
        assert url == card.url
        return analyzed

    monkeypatch.setattr(mcp_server, "search_itch_games", fake_search_itch_games)
    monkeypatch.setattr(mcp_server, "analyze_itch_game", fake_analyze_itch_game)

    res = json.loads(await mcp_server.recommend_by_preset(
        preset="tactile_incremental", source="itch", max_hours=3.0, limit=5
    ))
    assert res["count"] == 1
    item = res["items"][0]
    assert item["name"] == "Nail Hammer"
    assert item["source"] == "itch"
    assert item["gti_score"] == analyzed.gti_score
    assert item["estimated_completion_hours"] == 1.5
    assert item["genre"] == "Incremental"
    match = match_loops(get_preset("tactile_incremental").loop, analyzed.loop)
    assert match.score > 0
    assert match.coverage > 0
    assert item["reasons"][0] == (
        f"CoreLoop match: {match.score * 100:.2f}; coverage: {match.coverage:.0%}"
    )
    assert item["score"] > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_source", ["description", "comments"])
async def test_analyze_itch_game_extracts_and_serializes_loop(evidence_source: str) -> None:
    cue = "Responsive controls, no stress."
    description = cue if evidence_source == "description" else "A short game."
    comment = cue if evidence_source == "comments" else "A fun game."
    page = f"""
        <h1 class="game_title">Nail Hammer</h1>
        <div class="formatted_description">{description}</div>
        <div class="community_post"><div class="post_body">{comment}</div></div>
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=page))

    result = await analyze_itch_game("https://dev.itch.io/nail-hammer", transport=transport)

    assert result.title == "Nail Hammer"
    assert set(result.loop.features) == {"tactile_feedback", "punishment_severity"}
    assert all(feature.confidence > 0 and feature.evidence
               for feature in result.loop.features.values())
    assert json.loads(json.dumps(result.as_dict()))["loop"] == result.loop.model_dump()
    assert ItchGame(title="Unknown", url="https://dev.itch.io/unknown").loop.features == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("exclude_demos", [None, True, False])
async def test_search_itch_games_filters_demo_titles(exclude_demos: bool | None) -> None:
    titles = ["Nail Hammer [DEMO]", "Pocket Demo", "Tiny dEmO", "Demolition", "Full Game"]
    catalog = "".join(
        f'<div class="game_cell"><a class="title game_link" '
        f'href="https://dev.itch.io/game-{index}">{title}</a></div>'
        for index, title in enumerate(titles)
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=catalog))
    options = {} if exclude_demos is None else {"exclude_demos": exclude_demos}

    # Keep the pre-existing positional transport argument usable.
    games = await search_itch_games("incremental", "popular", None, False, 1, transport, **options)

    expected = titles if exclude_demos is False else ["Demolition", "Full Game"]
    assert [game.title for game in games] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2, 4])
async def test_recommend_by_preset_both_bounds_sources_and_sorts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patch_mcp_network, sample_game, limit: int,
) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    cap = max(limit, 3)
    games = {
        appid: sample_game(appid, f"Steam {appid}", "", "Beat it in one sitting")
        for appid in range(100, 108)
    }
    for appid, game in games.items():
        game.loop = _candidate(tactile_feedback=0.95 if appid == 100 else 0.1,
                               punishment_severity=0.1)
    patch_mcp_network(monkeypatch, games)
    steam_calls: list[int] = []
    original_collect = mcp_server.Pipeline.collect

    async def collect(self, appid: int, discover: bool = False):
        steam_calls.append(appid)
        return await original_collect(self, appid, discover=discover)

    monkeypatch.setattr(mcp_server.Pipeline, "collect", collect)
    search_limits: list[int] = []

    async def fake_search_by_tag(transport, tag, limit=10):
        search_limits.append(limit)
        # The second preset tag repeats the same games.
        return {"items": [{"appid": appid, "name": game.name} for appid, game in games.items()]}

    cards = [ItchGame(title=f"Itch {index}", url=f"https://dev.itch.io/game-{index}")
             for index in range(8)]
    itch_calls: list[str] = []

    async def fake_search_itch_games(**kwargs):
        return cards

    async def fake_analyze_itch_game(url: str, title: str = ""):
        itch_calls.append(url)
        return ItchGame(title=title, url=url, estimated_hours=1.0,
                        loop=_candidate(tactile_feedback=0.9, punishment_severity=0.1))

    monkeypatch.setattr(mcp_server, "search_by_tag", fake_search_by_tag)
    monkeypatch.setattr(mcp_server, "search_itch_games", fake_search_itch_games)
    monkeypatch.setattr(mcp_server, "analyze_itch_game", fake_analyze_itch_game)

    result = json.loads(await mcp_server.recommend_by_preset(source="both", limit=limit))

    assert search_limits == [cap] * len(get_preset("tactile_incremental").steam_tags)
    assert sorted(steam_calls) == list(games)[:cap]
    assert sorted(itch_calls) == sorted(card.url for card in cards[:cap])
    assert result["count"] == limit
    assert result["items"][0]["name"] == "Steam 100"
    scores = [item["score"] for item in result["items"]]
    assert scores == sorted(scores, reverse=True)
    expected_sources = {"steam", "itch"} if limit > 1 else {"steam"}
    assert {item["source"] for item in result["items"]} == expected_sources


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["steam", "itch"])
@pytest.mark.parametrize("demo_in_catalog", [True, False])
async def test_recommend_by_preset_excludes_demos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, patch_mcp_network, sample_game,
    source: str, demo_in_catalog: bool,
) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    titles = ["Nail Hammer [DEMO]", "Pocket Demo", "Demolition"]
    catalog_titles = titles if demo_in_catalog else ["Nail Hammer", "Pocket", "Demolition"]
    games = {
        appid: sample_game(appid, title, "responsive controls", "Beat it in one sitting")
        for appid, title in enumerate(titles, start=100)
    }
    patch_mcp_network(monkeypatch, games)

    async def fake_search_by_tag(transport, tag, limit=10):
        return {"items": [{"appid": appid, "name": name}
                          for appid, name in enumerate(catalog_titles, start=100)]}

    cards = [ItchGame(title=name, url=f"https://dev.itch.io/game-{index}")
             for index, name in enumerate(catalog_titles)]
    analyzed_urls: list[str] = []

    async def fake_search_itch_games(**kwargs):
        return cards

    async def fake_analyze_itch_game(url: str, title: str = ""):
        analyzed_urls.append(url)
        index = int(url.rsplit("-", 1)[1])
        return ItchGame(title=titles[index], url=url, estimated_hours=1.0)

    monkeypatch.setattr(mcp_server, "search_by_tag", fake_search_by_tag)
    monkeypatch.setattr(mcp_server, "search_itch_games", fake_search_itch_games)
    monkeypatch.setattr(mcp_server, "analyze_itch_game", fake_analyze_itch_game)

    result = json.loads(await mcp_server.recommend_by_preset(source=source, limit=2))

    assert [item["name"] for item in result["items"]] == ["Demolition"]
    if source == "steam" or not demo_in_catalog:
        assert {item["name"] for item in result["excluded"] if item["reason"] == "DEMO"} == set(titles[:2])
    if source == "itch" and demo_in_catalog:
        assert analyzed_urls == [cards[2].url]


@pytest.mark.asyncio
async def test_recommend_by_preset_rejects_bad_args() -> None:
    with pytest.raises(ValueError, match="Unknown preset"):
        await mcp_server.recommend_by_preset(preset="bogus")
    with pytest.raises(ValueError, match="source"):
        await mcp_server.recommend_by_preset(preset="tactile_incremental", source="nope")
    with pytest.raises(ValueError, match="max_hours"):
        await mcp_server.recommend_by_preset(preset="tactile_incremental", max_hours=0.0)
