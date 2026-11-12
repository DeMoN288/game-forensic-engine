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

    async def get(self, url: str, params: dict | None = None) -> str:
        path = self._cache_path(url, params)
        if self.config.cache_ttl and path.exists():
            try:
                cached = await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))
                if time.time() - cached["time"] < self.config.cache_ttl:
                    return cached["body"]
            except (OSError, ValueError, KeyError):
                pass
        host = urlsplit(url).netloc
        last_error = "no routes"
        for route, client in enumerate(self.clients):
            for attempt in range(self.config.attempts):
                await self._pace(host)
                delay = 0.0
                try:
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
                            last_error = f"{host}: HTTP 429"
                        elif status in {401, 403, 451}:
                            last_error = f"{host}: HTTP {status}; route {route} blocked"
                            break
                        elif status >= 500 or status == 408:
                            last_error = f"{host}: HTTP {status}"
                        elif status >= 400:
                            raise SourceError(f"{host}: HTTP {status}")
                        else:
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > self.config.max_body:
                                    raise SourceError(f"{host}: response too large")
                            text = body.decode(response.encoding or "utf-8", errors="replace")
                            if any(marker in text.lower() for marker in (
                                "cf-chl-", "just a moment...", "verify you are human",
                                "this item is currently unavailable in your region",
                            )):
                                last_error = f"{host}: challenge or region-block page"
                                break
                            if self.config.cache_ttl:
                                await asyncio.to_thread(self._write_cache, path, text)
                            return text
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    # Library exception strings can contain proxy credentials.
                    last_error = f"{host}: {type(exc).__name__} on route {route}"
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
