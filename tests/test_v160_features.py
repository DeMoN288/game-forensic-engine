import json

import httpx
import pytest

from game_forensic import mcp_server
from game_forensic.matcher import extract_loop
from game_forensic.models import Game
from game_forensic.pricing import fetch_steam_price
from game_forensic.storage import load_played_appids


@pytest.mark.parametrize("text", [
    "responsive controls and branching choices",
    "story narrator choices atmospheric",
    "clicker incremental idle prestige satisfying",
])
def test_v160_loop_cues(text):
    loop = extract_loop(Game(appid=1, name="test", description=text))
    assert loop.features
    assert any(feature.confidence > 0 for feature in loop.features.values())


def test_load_played_appids(tmp_path):
    path = tmp_path / "my_steam_games.json"
    path.write_text(json.dumps({"games": [{"appid": 123}, {"appid": "bad"}]}))
    assert load_played_appids(path) == {123}


@pytest.mark.asyncio
async def test_fetch_steam_price(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    payload = {"123": {"success": True, "data": {
        "price_overview": {"currency": "USD", "final": 499, "initial": 999},
    }}}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    assert await fetch_steam_price(123, transport=transport) == {
        "sale_price": 4.99, "normal_price": 9.99, "is_on_sale": True
    }


@pytest.mark.parametrize("text", ["puzzle perspective illusion", "short bite-sized game", "idle numbers"])
def test_genre_and_length_do_not_invent_loop_axes(text):
    assert extract_loop(Game(appid=1, name="test", description=text)).features == {}


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
async def test_preset_rejects_invalid_budget(value):
    with pytest.raises(ValueError, match="max_price_usd"):
        await mcp_server.recommend_by_preset(max_price_usd=value)


@pytest.mark.parametrize("data, expected", [
    ({"is_free": True}, {"sale_price": 0.0, "normal_price": 0.0, "is_on_sale": False}),
    ({"price_overview": {"currency": "EUR", "final": 499, "initial": 999}}, None),
    ({}, None),
    ({"price_overview": {"currency": "USD", "final": "NaN", "initial": 999}}, None),
])
async def test_price_free_or_unavailable(monkeypatch, tmp_path, data, expected):
    monkeypatch.chdir(tmp_path)
    def respond(request):
        assert request.url.host == "store.steampowered.com"
        assert request.url.params["cc"] == "us"
        return httpx.Response(200, json={"123": {"success": True, "data": data}})
    assert await fetch_steam_price(123, transport=httpx.MockTransport(respond)) == expected


async def test_preset_applies_price_and_shared_safety_filters(
    monkeypatch, tmp_path, patch_mcp_network, sample_game,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GAME_FORENSIC_DATA_DIR", str(tmp_path))
    games = {i: sample_game(i, f"Game {i}", "", "Beat it in one sitting") for i in range(100, 105)}
    games[100].reviews = []
    games[101].reviews = [review.model_copy(update={"text": "A nice game"}) for review in games[101].reviews]
    patch_mcp_network(monkeypatch, games)
    async def search(*args, **kwargs):
        return {"items": [{"appid": i, "name": game.name} for i, game in games.items()]}
    async def price(appid):
        return None if appid == 102 else {"sale_price": 5, "normal_price": 10, "is_on_sale": True}
    monkeypatch.setattr(mcp_server, "search_by_tag", search)
    monkeypatch.setattr(mcp_server, "fetch_steam_price", price)
    (tmp_path / "my_steam_games.json").write_text('[104]', encoding="utf-8")
    result = json.loads(await mcp_server.recommend_by_preset(source="steam", max_price_usd=6))
    assert [item["appid"] for item in result["items"]] == [103]
    reasons = {item["appid"]: item["reason"] for item in result["excluded"]}
    assert reasons == {100: "INSUFFICIENT_DATA", 101: "LENGTH_UNKNOWN",
                       102: "PRICE_UNKNOWN", 104: "ALREADY_PLAYED"}
