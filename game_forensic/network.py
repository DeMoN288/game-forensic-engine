"""Bounded HTTP transport; fallback proxies do not solve CAPTCHA challenges."""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from .models import Model


class NetworkConfig(Model):
    proxies: list[str] = Field(default_factory=list)
    timeout: float = Field(default=20, gt=0)
    attempts: int = Field(default=3, ge=1, le=6)
    concurrency: int = Field(default=6, ge=1, le=32)
    host_interval: float = Field(default=1.0, ge=0)
    cache_ttl: int = Field(default=3600, ge=0)
    cache_dir: Path = Path(".cache/game-forensic")
    max_body: int = Field(default=8_000_000, gt=0)
    max_retry_after: float = Field(default=120, gt=0)


class SourceError(RuntimeError):
    pass


# Cloudflare interstitials and region blocks are returned with HTTP 200.
BLOCK_MARKERS = (
    "cf-chl-", "just a moment...", "verify you are human",
    "this item is currently unavailable in your region",
)


def retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return max(0, (dt - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class Transport:
    def __init__(self, config: NetworkConfig):
        self.config = config
        self.clients: list[httpx.AsyncClient] = []
        self.sem = asyncio.Semaphore(config.concurrency)
        self.locks: dict[str, asyncio.Lock] = {}
        self.next_request: dict[str, float] = {}

    async def __aenter__(self) -> Self:
        try:
            for proxy in [None, *self.config.proxies]:
                if proxy and urlsplit(proxy).scheme not in {"http", "https", "socks5", "socks5h"}:
                    raise ValueError("Proxy must use HTTP, HTTPS or SOCKS5")
                self.clients.append(httpx.AsyncClient(
                    proxy=proxy,
                    timeout=httpx.Timeout(self.config.timeout, connect=min(8, self.config.timeout)),
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                    follow_redirects=True, trust_env=False,
                    limits=httpx.Limits(max_connections=self.config.concurrency),
                ))
        except Exception:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *args) -> None:
        await asyncio.gather(*(client.aclose() for client in self.clients))

    async def _pace(self, host: str) -> None:
        async with self.locks.setdefault(host, asyncio.Lock()):
            await asyncio.sleep(max(0, self.next_request.get(host, 0) - time.monotonic()))
            self.next_request[host] = time.monotonic() + self.config.host_interval

    def _cache_path(self, url: str, params: dict | None) -> Path:
        key = json.dumps([url, params], sort_keys=True, ensure_ascii=False)
        return self.config.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".json")

    @staticmethod
    def _read_cache(path: Path, ttl: int) -> str | None:
        """Cached body when the entry is a fresh object with a string body, else None.

        Any malformed cache (truncated JSON, wrong shape, non-string body) simply means
        "no cache": the caller falls through to the network instead of failing.
        """
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(cached, dict):
                return None
            body = cached["body"]
            if isinstance(body, str) and time.time() - cached["time"] < ttl:
                return body
        except (OSError, ValueError, TypeError, KeyError):
            return None
        return None

    async def _read_body(self, host: str, response: httpx.Response) -> str:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > self.config.max_body:
                raise SourceError(f"{host}: response too large")
        return body.decode(response.encoding or "utf-8", errors="replace")

    @staticmethod
    def _is_block_page(text: str) -> bool:
        lowered = text.lower()
        return any(marker in lowered for marker in BLOCK_MARKERS)

    async def _request(self, host: str, route: int, attempt: int, client: httpx.AsyncClient,
                       url: str, params: dict | None, path: Path) -> tuple[str, str, float]:
        """One attempt on one route.

        Returns ("done", body, _) on success, ("stop", error, _) when this route is
        exhausted, or ("retry", error, delay) when another attempt is worthwhile.
        """
        async with self.sem, client.stream("GET", url, params=params) as response:
            status = response.status_code
            if status == 429:
                delay = retry_after(response.headers.get("Retry-After"))
                if delay > self.config.max_retry_after:
                    raise SourceError(f"{host}: 429; cooldown exceeds configured budget")
                # Same host cooldown applies to every route, including proxies.
                delay = max(delay, 2 ** attempt + random.uniform(0, 0.5))
                self.next_request[host] = max(
                    self.next_request.get(host, 0), time.monotonic() + delay)
                return "retry", f"{host}: HTTP 429", delay
            if status in {401, 403, 451}:
                return "stop", f"{host}: HTTP {status}; route {route} blocked", 0.0
            if status >= 500 or status == 408:
                return "retry", f"{host}: HTTP {status}", 0.0
            if status >= 400:
                raise SourceError(f"{host}: HTTP {status}")
            text = await self._read_body(host, response)
            if self._is_block_page(text):
                return "stop", f"{host}: challenge or region-block page", 0.0
            if self.config.cache_ttl:
                await asyncio.to_thread(self._write_cache, path, text)
            return "done", text, 0.0

    async def get(self, url: str, params: dict | None = None) -> str:
        path = self._cache_path(url, params)
        if self.config.cache_ttl and path.exists():
            cached = await asyncio.to_thread(self._read_cache, path, self.config.cache_ttl)
            if cached is not None:
                return cached
        host = urlsplit(url).netloc
        last_error = "no routes"
        for route, client in enumerate(self.clients):
            for attempt in range(self.config.attempts):
                await self._pace(host)
                try:
                    action, payload, delay = await self._request(
                        host, route, attempt, client, url, params, path)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    # Library exception strings can contain proxy credentials.
                    action, payload, delay = "retry", f"{host}: {type(exc).__name__} on route {route}", 0.0
                if action == "done":
                    return payload
                last_error = payload
                if action == "stop":
                    break
                if attempt + 1 < self.config.attempts:
                    await asyncio.sleep(max(delay, 2 ** attempt + random.uniform(0, 0.5)))
        raise SourceError(last_error)

    @staticmethod
    def _write_cache(path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temp.write_text(json.dumps({"time": time.time(), "body": text}), encoding="utf-8")
            temp.replace(path)
        except OSError:
            pass

    async def json(self, url: str, params: dict | None = None) -> dict:
        try:
            value = json.loads(await self.get(url, params))
        except ValueError as exc:
            raise SourceError(f"{urlsplit(url).netloc}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise SourceError("Expected a JSON object")
        return value
