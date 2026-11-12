import pytest
from game_forensic.mcp_server import steam_analyze_game, steam_match_backlog, steam_batch_filter

@pytest.mark.asyncio
async def test_mcp_steam_analyze_game():
    res = await steam_analyze_game(105600, review_pages=1)
    assert "appid" in res
    assert "105600" in res

@pytest.mark.asyncio
async def test_mcp_steam_match_backlog():
    res = await steam_match_backlog(105600, 105600)
    assert "reference_game" in res
    assert "candidate_game" in res

@pytest.mark.asyncio
async def test_mcp_steam_batch_filter():
    res = await steam_batch_filter([105600], max_grind=40)
    assert "approved_backlog" in res
