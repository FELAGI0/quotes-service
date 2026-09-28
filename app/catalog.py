from __future__ import annotations

import asyncio
import os
import random
import time
from email.utils import parsedate_to_datetime

import httpx
import orjson

BREAKER_THRESHOLD = 5
BREAKER_OPEN = float(os.getenv("CATALOG_BREAKER_OPEN_SECS", "30"))
BREAKER_MAX = 300.0
MAX_CONCURRENCY = 10
# Reader promise is 3s; leave headroom for framing/egress.
READER_BUDGET = 2.6


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except Exception:
        return 0.0


class CatalogClient:
    def __init__(self) -> None:
        self._base_url: str | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=1.0, read=2.0, write=1.0, pool=1.0),
            limits=httpx.Limits(
                max_connections=MAX_CONCURRENCY, max_keepalive_connections=MAX_CONCURRENCY
            ),
        )
        self.reads = 0
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)
        self._flight_lock = asyncio.Lock()
        self._breaker_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[dict | None]] = {}
        self._state = "closed"
        self._fails = 0
        self._open_until = 0.0
        self._backoff = BREAKER_OPEN
        self._probing = False

    def set_source(self, url: str) -> str:
        url = url.strip().rstrip("/")
        self._base_url = url
        return url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, qid: str) -> dict | None:
        """Return the catalog record, or None on 404/unavailable. Never raises."""
        if self._base_url is None:
            return None
        loop = asyncio.get_running_loop()
        async with self._flight_lock:
            fut = self._inflight.get(qid)
            if fut is None:
                fut = loop.create_future()
                self._inflight[qid] = fut
                leader = True
            else:
                leader = False
        if not leader:
            return await fut
        try:
            result = await self._request(qid)
        except Exception:
            result = None
        if not fut.done():
            fut.set_result(result)
        async with self._flight_lock:
            self._inflight.pop(qid, None)
        return result

    async def _request(self, qid: str) -> dict | None:
        now = time.monotonic()
        async with self._breaker_lock:
            if self._state == "open":
                if now < self._open_until:
                    return None  # fast fail: don't add load to a down catalog
                self._state = "half_open"
            if self._state == "half_open":
                if self._probing:
                    return None  # a single probe at a time
                self._probing = True
        deadline = time.monotonic() + READER_BUDGET
        try:
            async with self._sem:
                return await self._attempt(qid, deadline)
        finally:
            if self._probing:
                async with self._breaker_lock:
                    if self._state == "half_open":
                        self._probing = False

    async def _attempt(self, qid: str, deadline: float) -> dict | None:
        url = f"{self._base_url}/quote/{qid}"
        for attempt in (0, 1):  # initial + at most one 503 retry
            self.reads += 1
            try:
                resp = await self._client.get(url)
            except httpx.HTTPError:  # transport failure: timeout / refused
                async with self._breaker_lock:
                    self._failed()
                return None
            code = resp.status_code
            if code == 200:
                async with self._breaker_lock:
                    self._succeeded()
                try:
                    return orjson.loads(resp.content)
                except orjson.JSONDecodeError:
                    return None
            if code == 404:
                async with self._breaker_lock:
                    self._succeeded()
                return None
            if code == 503:
                # Catalog is busy, not down: do NOT trip the breaker. Honor
                # Retry-After for one retry, but never keep the reader past its budget.
                ra = _retry_after(resp.headers.get("retry-after"))
                if attempt == 0 and 0 < ra < deadline - time.monotonic():
                    await asyncio.sleep(ra)
                    continue
                return None
            async with self._breaker_lock:
                self._succeeded()
            return None
        return None

    def _open(self, escalate: bool) -> None:
        self._backoff = min(self._backoff * 2, BREAKER_MAX) if escalate else BREAKER_OPEN
        self._open_until = time.monotonic() + self._backoff + random.uniform(0, self._backoff * 0.1)
        self._state = "open"
        self._probing = False

    def _failed(self) -> None:
        self._probing = False
        if self._state == "half_open":
            self._open(escalate=True)
            return
        self._fails += 1
        if self._fails >= BREAKER_THRESHOLD:
            self._open(escalate=False)

    def _succeeded(self) -> None:
        self._fails = 0
        self._state = "closed"
        self._backoff = BREAKER_OPEN
        self._probing = False