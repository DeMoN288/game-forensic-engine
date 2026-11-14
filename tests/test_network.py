import json

import httpx
import pytest

from game_forensic import network
from game_forensic.network import NetworkConfig, SourceError, Transport, retry_after
from game_forensic.pipeline import Pipeline, PipelineConfig


def _mock_async_client(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=kwargs["timeout"],
            headers=kwargs["headers"],
            follow_redirects=kwargs["follow_redirects"],
            trust_env=False,
        )

    monkeypatch.setattr(network.httpx, "AsyncClient", factory)


def test_retry_after_parsing():
    assert retry_after(None) == 0
    assert retry_after("2.5") == 2.5
    assert retry_after("-4") == 0
    assert retry_after("not-a-date") == 0


async def test_transport_retries_429_and_honours_retry_after(monkeypatch, tmp_path):
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, text='{"ok": true}')

    async def no_sleep(delay):
        sleeps.append(delay)

    _mock_async_client(monkeypatch, handler)
    monkeypatch.setattr(network.asyncio, "sleep", no_sleep)
    config = NetworkConfig(attempts=2, host_interval=0, cache_dir=tmp_path)

    async with Transport(config) as transport:
        assert await transport.json("https://example.test/data") == {"ok": True}

    assert calls == 2
    assert any(1.9 <= delay <= 3.0 for delay in sleeps)


async def test_transport_recovers_from_corrupt_cache(monkeypatch, tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="fresh")

    _mock_async_client(monkeypatch, handler)
    config = NetworkConfig(attempts=1, host_interval=0, cache_dir=tmp_path)
    transport = Transport(config)
    cache_path = transport._cache_path("https://example.test/data", None)
    cache_path.write_text("{broken", encoding="utf-8")

    async with transport:
        assert await transport.get("https://example.test/data") == "fresh"

    assert calls == 1
    assert json.loads(cache_path.read_text(encoding="utf-8"))["body"] == "fresh"


@pytest.mark.parametrize("payload", ["[]", '"just text"', "null", "123", "true"])
async def test_transport_recovers_from_non_object_cache(monkeypatch, tmp_path, payload):
    """Valid JSON that is not an object must not crash the request with TypeError."""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="fresh")

    _mock_async_client(monkeypatch, handler)
    config = NetworkConfig(attempts=1, host_interval=0, cache_dir=tmp_path)
    transport = Transport(config)
    cache_path = transport._cache_path("https://example.test/data", None)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(payload, encoding="utf-8")

    async with transport:
        assert await transport.get("https://example.test/data") == "fresh"

    assert calls == 1
    assert json.loads(cache_path.read_text(encoding="utf-8"))["body"] == "fresh"


async def test_transport_ignores_cache_with_non_string_body(monkeypatch, tmp_path):
    """A cache entry whose body is not a string must fall through to the network."""
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="fresh")

    _mock_async_client(monkeypatch, handler)
    config = NetworkConfig(attempts=1, host_interval=0, cache_dir=tmp_path)
    transport = Transport(config)
    cache_path = transport._cache_path("https://example.test/data", None)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"time": __import__("time").time(), "body": ["not", "text"]}),
                          encoding="utf-8")

    async with transport:
        assert await transport.get("https://example.test/data") == "fresh"

    assert calls == 1


async def test_transport_rejects_oversized_body(monkeypatch, tmp_path):
    def handler(request):
        return httpx.Response(200, content=b"too large")

    _mock_async_client(monkeypatch, handler)
    config = NetworkConfig(attempts=1, host_interval=0, cache_dir=tmp_path, max_body=4)

    async with Transport(config) as transport:
        with pytest.raises(SourceError, match="response too large"):
            await transport.get("https://example.test/data")


async def test_reviews_missing_key_ends_the_sample_without_a_failure():
    """The live service drops the `reviews` key once the cursor is exhausted."""

    class _Transport:
        async def json(self, url, params):
            if json.loads(params["input_json"])["cursor"] == "*":
                return {"response": {
                    "query_summary": {"total_reviews": 1},
                    "reviews": [{"recommendationid": "1", "voted_up": True,
                                 "review": "good", "language": "english"}],
                    "cursor": "next",
                }}
            return {"response": {"query_summary": {"num_reviews": 0}, "cursor": "next",
                                 "total_matching": 1}}

    pipeline = Pipeline(_Transport(), PipelineConfig(review_pages=3))
    reviews, status, summary = await pipeline._reviews(100)

    assert len(reviews) == 1
    assert status.status == "ok"
    assert status.detail == ""
    assert summary == {"total_reviews": 1}


async def test_reviews_wrong_type_is_reported_as_a_failure():
    class _Transport:
        async def json(self, url, params):
            return {"response": {"query_summary": {}, "reviews": "not-a-list"}}

    pipeline = Pipeline(_Transport(), PipelineConfig(review_pages=1))
    reviews, status, _summary = await pipeline._reviews(100)

    assert reviews == []
    assert status.status == "unavailable"
    assert "TypeError" in status.detail


async def test_reviews_pagination_stops_on_empty_page():
    cursors = []

    class _Transport:
        async def json(self, url, params):
            request = json.loads(params["input_json"])
            cursor = request["cursor"]
            cursors.append(cursor)
            if cursor == "end":
                return {"response": {"reviews": []}}
            number = 0 if cursor == "*" else 2
            return {"response": {
                "query_summary": {"total_reviews": 3},
                "reviews": [
                    {
                        "recommendationid": str(number + offset),
                        "voted_up": True,
                        "review": "good",
                        "language": "english",
                    }
                    for offset in range(2 if cursor == "*" else 1)
                ],
                "cursor": "next" if cursor == "*" else "end",
            }}

    pipeline = Pipeline(_Transport(), PipelineConfig(review_pages=3))
    reviews, status, summary = await pipeline._reviews(100)

    assert cursors == ["*", "next", "end"]
    assert len(reviews) == 3
    assert status.status == "ok"
    assert summary == {"total_reviews": 3}
