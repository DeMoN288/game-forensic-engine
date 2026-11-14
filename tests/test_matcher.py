import pytest
from pydantic import ValidationError

from game_forensic.analyzer import Analyzer
from game_forensic.matcher import (
    AXES,
    Matcher,
    MatcherConfig,
    extract_loop,
    match_loops,
    rejection_reason,
    store_candidates,
)
from game_forensic.models import CoreLoop, Game
from game_forensic.parser import Parser
from game_forensic.pipeline import Pipeline, PipelineConfig


def test_identical_opposite_and_symmetric_vectors(loop):
    assert match_loops(loop(), loop()).score == 1
    assert match_loops(loop(0), loop(1)).score == 0
    assert match_loops(loop(0.1), loop(0.8)) == match_loops(loop(0.8), loop(0.1))


def test_unknown_dimensions_and_confidence_penalize_score(loop):
    partial = match_loops(loop(), loop(axes=AXES[:2]))
    assert partial.coverage == 0.4
    assert partial.score == 0.4
    assert partial.vector_similarity == 1
    assert match_loops(loop(), loop(confidence=0.5)).score == 0.5
    assert match_loops(loop(), loop(confidence=0)).coverage == 0
    assert match_loops(CoreLoop(), CoreLoop()).score == 0


def test_weighted_distance(loop):
    weights = dict.fromkeys(AXES, 1.0)
    weights[AXES[0]] = 6
    candidate = loop(0)
    candidate.features[AXES[0]].value = 1
    assert match_loops(loop(0), candidate, weights).score == 0.4


@pytest.mark.parametrize("options", [
    {"weights": {}}, {"weights": dict.fromkeys(AXES, 0)},
    {"weights": dict.fromkeys(AXES, float("nan"))}, {"max_candidates": 0},
    {"top_k": 6}, {"profiles": {1: {"genre": 0.5}}},
    {"profiles": {1: {AXES[0]: 1.1}}}, {"profiles": {1: {AXES[0]: float("nan")}}},
    {"blacklist": {0: "bad"}}, {"blacklist": {1: ""}},
])
def test_invalid_config(options):
    with pytest.raises(ValidationError):
        MatcherConfig(**options)


def test_curated_profiles_and_no_default_values():
    empty = Game(appid=1, description="An action RPG adventure")
    assert extract_loop(empty).features == {}
    curated = extract_loop(empty, dict.fromkeys(AXES, 0.3))
    assert curated.origin == "curated"
    assert len(curated.features) == 5
    assert all(f.value == 0.3 and f.confidence == 1 for f in curated.features.values())


def test_en_ru_extraction_evidence_and_negation():
    en = Game(appid=1, description="Fast-paced combat. Skill trees. Responsive controls. Permadeath. Story-rich.")
    result = extract_loop(en)
    assert set(result.features) == set(AXES)
    assert all(f.value == 0.85 and f.evidence for f in result.features.values())
    ru = Game(appid=2, description="Пошаговый бой. Дерево навыков. Отзывчивое управление. Глубокий сюжет.")
    assert extract_loop(ru).features["combat_pace"].value == 0.15
    assert extract_loop(ru).features["build_depth"].value == 0.85
    negated = Game(appid=3, description="Not responsive controls. No skill tree. No story.")
    result = extract_loop(negated)
    assert "tactile_feedback" not in result.features
    assert result.features["build_depth"].value == 0.15
    assert result.features["narrative_weight"].value == 0.15


def test_negative_tactility_does_not_match_positive_substring():
    result = extract_loop(Game(appid=1, description="Unresponsive controls"))
    assert result.features["tactile_feedback"].value == 0.15
    result = extract_loop(Game(appid=1, description="Неотзывчивое управление"))
    assert result.features["tactile_feedback"].value == 0.15


def test_conflicting_evidence_reduces_confidence():
    strong = extract_loop(Game(appid=1, description="Responsive controls"))
    mixed = extract_loop(Game(appid=1, description="Responsive controls but clunky controls"))
    assert mixed.features["tactile_feedback"].value == 0.5
    assert mixed.features["tactile_feedback"].confidence < strong.features["tactile_feedback"].confidence


@pytest.mark.parametrize("options,rejected", [
    ({"verdict": "RED_FLAG_REJECT", "gti": 50, "repetition": 0.6}, True),
    ({"verdict": "CAUTION", "gti": 16, "time_gating": 0.1}, True),
    ({"verdict": "CAUTION", "gti": 16, "pay_to_skip": 0.1}, True),
    ({"verdict": "CAUTION", "gti": 16, "repetition": 0.2}, False),
    ({"verdict": "INSUFFICIENT_DATA", "gti": 0}, True),
    ({"gti": None}, True), ({}, False),
])
def test_safety_policy(make_report, options, rejected):
    assert bool(rejection_reason(make_report(**options))) is rejected


def test_store_parser_scope_and_deduplication():
    html = '''<a href="https://store.steampowered.com/app/999/">Navigation</a>
    <div id="recommended_block"><a data-ds-appid="2" href="/app/2/">Two</a>
    <a href="https://store.steampowered.com/app/3/">Three</a>
    <a data-ds-appid="1">Self</a><a data-ds-appid="0">Zero</a>
    <a data-ds-appid="2">Duplicate</a><a data-ds-appid="bad">Malformed</a></div>'''
    assert store_candidates(html, 1) == [2, 3]
    assert store_candidates("<html>age gate</html>", 1) == []


def test_existing_peek_parser():
    html = '''<a href="https://steampeek.hu/?appid=2">Two</a>
    <a href="https://store.steampowered.com/app/3/">Three</a>
    <a href="https://steampeek.hu/?appid=1">Self</a>
    <a href="https://steampeek.hu/?appid=4">Demo</a>'''
    assert Parser().peek(html, 1) == [2, 3]


async def test_batch_blacklist_safety_failure_top_five(game, FakePipeline):
    ids = list(range(2, 13)) + [2, 1]
    games = {i: game(i) for i in range(1, 13)}
    games[1].candidate_ids = ids
    games[3] = game(3, "forced grind pay to win daily chores")
    games[4].reviews = []
    pipeline = FakePipeline(games, errors=[5], delay=0.002)
    report = await Matcher(pipeline, config=MatcherConfig(blacklist={2: "Обман ожиданий"},
                                                        concurrency=2)).recommend(1)
    assert len(report.ranked) == 5
    assert [item.appid for item in report.ranked] == [6, 7, 8, 9, 10]
    assert "BLACKLIST" in report.excluded["2"]
    assert "RED_FLAG_REJECT" in report.excluded["3"]
    assert "INSUFFICIENT_DATA" in report.excluded["4"]
    assert "SourceError" in report.excluded["5"]
    assert (2, False) not in pipeline.calls
    assert pipeline.peak == 2
    assert report.raw_candidates == list(range(2, 13))


async def test_caution_timegate_rejected_even_when_evidence_disabled(game, FakePipeline):
    games = {1: game(1, candidates=[2]), 2: game(2)}
    # Seven out of thirty complaints: CAUTION with P2W, below RED_FLAG threshold.
    for review in games[2].reviews[:7]:
        review.text = "pay to win"
    analyzer = Analyzer()
    analyzer.config.max_evidence = 0
    report = await Matcher(FakePipeline(games), analyzer).recommend(1)
    assert "CAUTION" in report.excluded["2"]
    assert "pay_to_skip" in report.excluded["2"]


async def test_non_p2w_caution_retained_with_warning(game, FakePipeline):
    games = {1: game(1, candidates=[2]), 2: game(2)}
    for review in games[2].reviews[:7]:
        review.text = "forced grind"
    report = await Matcher(FakePipeline(games)).recommend(1)
    assert report.ranked[0].grind.verdict == "CAUTION"
    assert report.ranked[0].warnings[0].startswith("CAUTION")


async def test_store_fallback_candidate_limit_and_blacklist_before_limit(game, FakePipeline, FakeTransport):
    transport = FakeTransport('<div id="recommended_block"><a data-ds-appid="3"></a>'
                              '<a data-ds-appid="4"></a><a data-ds-appid="5"></a></div>')
    pipeline = FakePipeline({i: game(i) for i in range(1, 6)}, transport)
    pipeline.games[1].candidate_ids = [2]
    config = MatcherConfig(max_candidates=2, blacklist={2: "risk"})
    report = await Matcher(pipeline, config=config).recommend(1)
    assert [i.appid for i in report.ranked] == [3, 4]
    assert "лимит" in report.excluded["5"]
    assert (5, False) not in pipeline.calls
    assert report.sources[-1].source == "steam_store_similar"


async def test_discovery_failure_and_no_matches_are_explained(game, FakePipeline, FakeTransport):
    report = await Matcher(FakePipeline({1: game(1)}, FakeTransport(fail=True))).recommend(1)
    assert report.ranked == []
    assert report.sources[-1].status == "unavailable"
    assert any("Нет кандидатов" in warning for warning in report.warnings)


async def test_reference_and_candidate_missing_features(game, FakePipeline, loop):
    pipeline = FakePipeline({1: game(1, candidates=[2], features=False), 2: game(2)})
    report = await Matcher(pipeline).recommend(1)
    assert "эталона" in report.excluded["2"]
    assert (2, False) not in pipeline.calls
    pipeline.games[1].loop = loop()
    pipeline.games[2].loop = CoreLoop()
    report = await Matcher(pipeline).recommend(1)
    assert "покрытие" in report.excluded["2"]


async def test_candidate_timeout_is_isolated(game, FakePipeline):
    pipeline = FakePipeline({1: game(1, candidates=[2, 3]), 2: game(2), 3: game(3)}, delay=0.02)
    report = await Matcher(pipeline, config=MatcherConfig(candidate_timeout=0.001)).recommend(1)
    assert "TimeoutError" in report.excluded["2"]
    assert "TimeoutError" in report.excluded["3"]
    assert pipeline.active == 0


async def test_full_pipeline_with_fixture_transport():
    description = "Fast-paced combat. Skill trees. Responsive controls. Permadeath. Story-rich."

    class FixtureTransport:
        async def json(self, url, params):
            if "appdetails" in url:
                appid = params["appids"]
                return {str(appid): {"success": True, "data": {
                    "type": "game", "steam_appid": appid, "name": f"Fixture {appid}",
                    "detailed_description": description,
                }}}
            return {"response": {"reviews": [{"recommendationid": str(i), "voted_up": True,
                                              "review": "great", "language": "english"} for i in range(25)],
                                 "cursor": "end"}}

        async def get(self, url, params=None):
            if "steampeek" in url:
                return '<a href="https://steampeek.hu/?appid=2">Candidate</a>'
            return '<div id="recommended_block"></div>'

    pipeline = Pipeline(FixtureTransport(), PipelineConfig(review_pages=1))
    report = await Matcher(pipeline).recommend(1)
    assert report.ranked[0].appid == 2
    assert report.ranked[0].grind.gti == 0
    assert report.ranked[0].match.coverage == 1
    assert 0 < report.ranked[0].match.score < 1


async def test_store_search_fallback_when_discovery_empty(game, FakePipeline, FakeTransport):
    class SearchTransport(FakeTransport):
        async def json(self, url, params):
            if "storesearch" in url:
                return {"items": [{"id": 2, "name": "Cozy Match"}]}
            return {}

    ref = game(1)
    ref.description = "Cozy incremental game with upgrades."
    cand = game(2)
    pipeline = FakePipeline({1: ref, 2: cand}, SearchTransport('<div id="recommended_block"></div>'))
    report = await Matcher(pipeline).recommend(1)
    assert [r.appid for r in report.ranked] == [2]
    assert any(s.source == "steam_store_search" and s.status == "ok" for s in report.sources)
