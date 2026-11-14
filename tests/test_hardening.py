import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from game_forensic import cli, mcp_server, storage
from game_forensic.matcher import Matcher
from game_forensic.models import RecommendationReport
from game_forensic.policy import SafetyPolicy, safety_gate
from game_forensic.storage import remove_blacklist, set_blacklist, set_profile, write_json_object


async def test_cli_blacklist_warnings_go_to_stderr(monkeypatch, tmp_path, capsys):
    path = tmp_path / "blacklist.json"
    path.write_text(json.dumps({"bad": "invalid", "200": 5, "300": "valid"}), encoding="utf-8")

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Matcher:
        def __init__(self, *args):
            pass

        async def recommend(self, appid):
            return RecommendationReport(reference=appid, raw_candidates=[], ranked=[], excluded={})

    monkeypatch.setattr(cli, "Transport", _Transport)
    monkeypatch.setattr(cli, "Matcher", _Matcher)
    args = cli.build_parser().parse_args(["recommend", "1", "--blacklist", str(path)])

    await cli.run_recommendation(args)

    errors = capsys.readouterr().err
    assert "bad" in errors
    assert "200" in errors


async def test_matcher_rejects_unverified_reference_before_candidate_collection(game, FakePipeline):
    reference = game(1, candidates=[2])
    reference.name = ""
    pipeline = FakePipeline({1: reference, 2: game(2)})

    report = await Matcher(pipeline).recommend(1)

    assert report.ranked == []
    assert "эталона" in report.excluded["2"]
    assert (2, False) not in pipeline.calls


async def test_batch_filter_rejects_unverified_game_with_enough_reviews(monkeypatch, tmp_path, sample_game):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    unverified = sample_game(100, "", "Cozy upgrades", "Great game")

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Pipeline:
        def __init__(self, transport, config):
            pass

        async def collect(self, appid, discover=False):
            return unverified

    monkeypatch.setattr(mcp_server, "Transport", _Transport)
    monkeypatch.setattr(mcp_server, "Pipeline", _Pipeline)

    result = json.loads(await mcp_server.steam_batch_filter([100]))

    rejected = result["rejected_grind_traps"][0]
    assert result["approved_count"] == 0
    assert rejected["verdict"] == "UNVERIFIED_GAME"
    assert rejected["store_verified"] is False
    assert "Steam Store" in rejected["reason"]


@pytest.mark.parametrize("unverified_appid", [100, 200])
async def test_match_backlog_never_recommends_unverified_game(monkeypatch, tmp_path, unverified_appid,
                                                              patch_mcp_network, sample_game):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    description = "Relaxing game with upgrades, responsive controls, no timers, story-rich."
    reviews = "Oddly satisfying progression, great upgrades, no timers, story-rich."
    patch_mcp_network(monkeypatch, {
        100: sample_game(100, "" if unverified_appid == 100 else "Reference", description, reviews),
        200: sample_game(200, "" if unverified_appid == 200 else "Candidate", description, reviews),
    })

    result = json.loads(await mcp_server.steam_match_backlog(100, 200))

    assert result["reference_game"]["store_verified"] is (unverified_appid != 100)
    assert result["candidate_game"]["store_verified"] is (unverified_appid != 200)
    assert result["recommendation_verdict"] == "UNVERIFIED_GAME"
    assert any("Steam Store" in warning for warning in result["warnings"])


@pytest.mark.parametrize("target", [0, -1, "0", "-42"])
async def test_numeric_appid_must_be_positive(target):
    with pytest.raises(ValueError, match="positive"):
        await mcp_server._resolve_appid(object(), target)


async def test_invalid_appid_does_not_create_blacklist(monkeypatch, tmp_path):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="positive"):
        await mcp_server.steam_blacklist_add(0, "invalid")

    assert not (tmp_path / "blacklist.json").exists()


def test_length_limit_rejects_unknown_estimate(make_report):
    decision = safety_gate(make_report(), SafetyPolicy(max_completion_hours=10), completion_hours=None)
    assert decision.allowed is False
    assert decision.code == "LENGTH_UNKNOWN"
    assert safety_gate(make_report(), SafetyPolicy(), completion_hours=None).allowed is True


async def test_batch_filter_enforces_unknown_length_limit(monkeypatch, tmp_path, sample_game):
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    good = sample_game(100, "Good", "Cozy upgrades", "Great game")

    class _Transport:
        def __init__(self, config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Pipeline:
        def __init__(self, transport, config):
            pass

        async def collect(self, appid, discover=False):
            return good

    monkeypatch.setattr(mcp_server, "Transport", _Transport)
    monkeypatch.setattr(mcp_server, "Pipeline", _Pipeline)

    result = json.loads(await mcp_server.steam_batch_filter([100], max_completion_hours=10))

    rejected = result["rejected_grind_traps"][0]
    assert rejected["passed"] is False
    assert rejected["policy_code"] == "LENGTH_UNKNOWN"


@pytest.mark.parametrize("contents", ["{broken", "[]"])
def test_corrupt_user_json_is_never_overwritten(tmp_path, contents):
    operations = (
        ("blacklist.json", lambda: set_blacklist(tmp_path, 100, "reason")),
        ("blacklist.json", lambda: remove_blacklist(tmp_path, 100)),
        ("profiles.json", lambda: set_profile(tmp_path, 100, {"combat_pace": 0.2})),
    )
    for filename, operation in operations:
        path = tmp_path / filename
        path.write_text(contents, encoding="utf-8")
        with pytest.raises(ValueError, match=filename):
            operation()
        assert path.read_text(encoding="utf-8") == contents


def test_invalid_utf8_user_json_is_never_overwritten(tmp_path):
    path = tmp_path / "blacklist.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError, match="blacklist.json"):
        set_blacklist(tmp_path, 100, "reason")
    assert path.read_bytes() == b"\xff"


def test_atomic_write_preserves_old_file_when_replace_fails(monkeypatch, tmp_path):
    path = tmp_path / "blacklist.json"
    path.write_text('{"old": "value"}', encoding="utf-8")

    def fail_replace(self, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        write_json_object(path, {"new": "value"})

    assert path.read_text(encoding="utf-8") == '{"old": "value"}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_blacklist_mutations_are_serialized(monkeypatch, tmp_path):
    workers = 16
    barrier = threading.Barrier(workers)
    guard = threading.Lock()
    active = 0
    peak = 0
    original_write = storage.write_json_object

    def observed_write(path, payload):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.002)
            original_write(path, payload)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(storage, "write_json_object", observed_write)

    def add(appid):
        barrier.wait()
        set_blacklist(tmp_path, appid, "reason")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(add, range(1, workers + 1)))

    saved = json.loads((tmp_path / "blacklist.json").read_text(encoding="utf-8"))
    assert peak == 1
    assert set(saved) == {str(appid) for appid in range(1, workers + 1)}
