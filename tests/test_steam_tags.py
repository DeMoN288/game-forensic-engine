from __future__ import annotations

import json

import pytest

from game_forensic import mcp_server
from game_forensic.steam import parse_search_results, resolve_tag, search_by_tag


class _FakeTransport:
    def __init__(self, tag_json: str, search_payload: dict) -> None:
        self._tag_json = tag_json
        self._search_payload = search_payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str, params: dict | None = None) -> str:
        return self._tag_json

    async def json(self, url: str, params: dict | None = None) -> dict:
        return self._search_payload


@pytest.mark.asyncio
async def test_resolve_tag_name_and_numeric_id() -> None:
    transport = _FakeTransport(
        json.dumps([{"tagid": 560542, "name": "Incremental"}, {"tagid": 4234, "name": "Short"}]),
        {},
    )
    assert await resolve_tag(transport, "incremental") == (560542, "Incremental")
    assert await resolve_tag(transport, 4234) == (4234, "4234")
    with pytest.raises(ValueError, match="Unknown Steam tag"):
        await resolve_tag(transport, "NonExistentTagXYZ")


def test_parse_search_results_filters_bundles_dlcs_and_duplicates() -> None:
    html = """
    <a class="search_result_row"
       href="https://store.steampowered.com/app/3107330/Nodebuster/"
       data-ds-appid="3107330">
      <span class="title">Nodebuster</span>
    </a>
    <a class="search_result_row"
       href="https://store.steampowered.com/app/3107330/Nodebuster/"
       data-ds-appid="3107330">
      <span class="title">Nodebuster</span>
    </a>
    <a class="search_result_row"
       href="https://store.steampowered.com/app/999/Cool_Soundtrack/"
       data-ds-appid="999">
      <span class="title">Cool Soundtrack</span>
    </a>
    <a class="search_result_row"
       href="https://store.steampowered.com/app/111/Bundle/"
       data-ds-appid="111,222">
      <span class="title">Bundle Pack</span>
    </a>
    """
    items = parse_search_results(html)
    assert items == [
        {
            "appid": 3107330,
            "name": "Nodebuster",
            "url": "https://store.steampowered.com/app/3107330/",
        }
    ]


@pytest.mark.asyncio
async def test_search_by_tag_and_mcp_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    html = """
    <a class="search_result_row"
       href="https://store.steampowered.com/app/5229450/Hammer_these_nails/"
       data-ds-appid="5229450">
      <span class="title">Hammer these nails!</span>
    </a>
    """
    fake = _FakeTransport(
        json.dumps([{"tagid": 560542, "name": "Incremental"}]),
        {"success": 1, "total_count": 1, "results_html": html},
    )
    direct = await search_by_tag(fake, "Incremental", limit=5)
    assert direct["tag_id"] == 560542
    assert direct["count"] == 1
    assert direct["items"][0]["appid"] == 5229450

    monkeypatch.setattr(mcp_server, "Transport", lambda cfg: fake)
    res = json.loads(await mcp_server.steam_search_by_tag("Incremental", limit=5))
    assert res["tag"] == "Incremental"
    assert res["items"][0]["name"] == "Hammer these nails!"
