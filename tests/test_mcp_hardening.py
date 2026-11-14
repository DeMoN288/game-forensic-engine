import json
from pathlib import Path

import pytest

from game_forensic import mcp_server


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.asyncio
async def test_batch_filter_rejects_nonfinite_thresholds(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        await mcp_server.steam_batch_filter([105600], max_grind=value)

    with pytest.raises(ValueError, match="finite"):
        await mcp_server.steam_batch_filter(
            [105600],
            max_completion_hours=value,
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.asyncio
async def test_profile_set_rejects_nonfinite_axis(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        await mcp_server.steam_profile_set(105600, {"combat_pace": value})


@pytest.mark.asyncio
async def test_batch_filter_isolates_unexpected_item_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    patch_mcp_network,
    sample_game,
) -> None:
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    patch_mcp_network(
        monkeypatch,
        {
            100: sample_game(100, "Broken Game", "Review text", "Review text"),
            200: sample_game(200, "Healthy Game", "Sandbox with upgrades.", "Fun progression."),
        },
    )

    original_collect = mcp_server.Pipeline.collect

    async def collect(self, appid: int, discover: bool = False):
        if appid == 100:
            raise RuntimeError("unexpected collection failure")
        return await original_collect(self, appid, discover=discover)

    monkeypatch.setattr(mcp_server.Pipeline, "collect", collect)

    result = json.loads(await mcp_server.steam_batch_filter([100, 200], max_grind=100))

    assert result["total_evaluated"] == 2
    errors = [item for item in result["rejected_grind_traps"] if "error" in item]
    assert errors == [
        {
            "appid": 100,
            "title": "AppID 100",
            "error": "unexpected collection failure",
            "passed": False,
        }
    ]
    assert any(item["appid"] == 200 for item in result["approved_backlog"])
