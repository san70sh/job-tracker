"""Polite async HTTP: shared client, per-host spacing, retry with backoff."""
from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .config import get_settings


class PostingGone(Exception):
    """404/410: the posting no longer exists (-> jobs.closed_at)."""


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, client: httpx.AsyncClient | None = None, delay: float | None = None):
        s = get_settings()
        self._own = client is None
        self.client = client or httpx.AsyncClient(
            headers={"User-Agent": s.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"},
            timeout=s.http_timeout,
            follow_redirects=True,
        )
        self.delay = s.per_host_delay if delay is None else delay
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._own:
            await self.client.aclose()

    async def __aenter__(self) -> "Fetcher":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _throttle(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            wait = self._last.get(host, 0) + self.delay - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    async def request(self, method: str, url: str, *, gone_ok: bool = False, **kw: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(3):
            await self._throttle(url)
            try:
                r = await self.client.request(method, url, **kw)
            except httpx.TransportError as e:
                last_exc = e
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (404, 410) and not gone_ok:
                raise PostingGone(f"{r.status_code} {url}")
            if r.status_code in (429, 502, 503, 504):
                await asyncio.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
                last_exc = FetchError(f"{r.status_code} {url}")
                continue
            if r.status_code >= 400:
                raise FetchError(f"{r.status_code} {method} {url}: {r.text[:200]}")
            return r
        raise FetchError(f"giving up on {url}: {last_exc}")

    async def get_json(self, url: str, **kw: Any) -> Any:
        return (await self.request("GET", url, **kw)).json()

    async def post_json(self, url: str, body: Any, **kw: Any) -> Any:
        return (await self.request("POST", url, json=body, **kw)).json()

    async def get_text(self, url: str, **kw: Any) -> str:
        return (await self.request("GET", url, **kw)).text
