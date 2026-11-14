from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from game_forensic.hltb import fetch_hltb_times


def _transport(calls: list[str], status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/init"):
            return httpx.Response(status, json={"token": "test-token"})
        return httpx.Response(
            status,
            json={
                "data": [
                    {
                        "game_id": 42,
                        "game_name": "Test Game",
                        "profile_steam": 123,
                        "comp_main": 3600,
                        "comp_plus": 7200,
                        "comp_100": 10800,
                        "comp_all": 14400,
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_init_and_search_parsing(tmp_path: Path) -> None:
    calls: list[str] = []
    result = await fetch_hltb_times(
        "Test Game",
        transport=_transport(calls),
        cache_dir=tmp_path,
    )

    assert result is not None
    assert result.game_id == 42
    assert result.main_hours == 1.0
    assert result.main_extra_hours == 2.0
    assert result.completionist_hours == 3.0
    assert result.combined_hours == 4.0
    assert calls == ["/api/search/site/init", "/api/search/site"]


@pytest.mark.anyio
async def test_prefers_matching_steam_appid(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"token": "token"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "game_id": 1,
                        "game_name": "Wrong",
                        "profile_steam": 111,
                        "comp_main": 3600,
                    },
                    {
                        "game_id": 2,
                        "game_name": "Right",
                        "profile_steam": 222,
                        "comp_main": 7200,
                    },
                ]
            },
        )

    result = await fetch_hltb_times(
        "Test Game",
        appid=222,
        cache_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    assert result is not None
    assert result.game_id == 2
    assert result.game_name == "Right"
    assert result.main_hours == 2.0


@pytest.mark.anyio
async def test_disk_cache_hit(tmp_path: Path) -> None:
    calls: list[str] = []
    transport = _transport(calls)

    first = await fetch_hltb_times(
        "Cached Game",
        cache_dir=tmp_path,
        transport=transport,
    )
    second = await fetch_hltb_times(
        "Cached Game",
        cache_dir=tmp_path,
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("network call"))
        ),
    )

    assert first == second
    assert calls == ["/api/search/site/init", "/api/search/site"]


@pytest.mark.anyio
async def test_http_error_returns_none(tmp_path: Path) -> None:
    result = await fetch_hltb_times(
        "Unavailable Game",
        cache_dir=tmp_path,
        transport=_transport([], status=500),
    )

    assert result is None
