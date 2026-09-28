"""Client-side checks for stages 3+4 against a running container."""

from __future__ import annotations

import asyncio
import os
import time

import httpx

BASE = os.getenv("BASE", "http://localhost:8000")
FAKE = os.getenv("FAKE", "http://localhost:9000")
# URL the *service* uses to reach the catalog (must resolve inside its network).
CATALOG_URL = os.getenv("CATALOG_URL", "http://fake:9000")


async def main() -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        print("stats0    ", (await c.get(f"{BASE}/stats")).json())

        r = await c.post(f"{BASE}/catalog/source", json={"url": CATALOG_URL})
        print("source    ", r.status_code, r.json())

        r = await c.get(f"{BASE}/quotes/ok-1")
        print("forward   ", r.status_code, "X-Source=" + str(r.headers.get("x-source")), r.json().get("id"))

        await c.post(f"{FAKE}/_reset_hits")
        res = await asyncio.gather(*[c.get(f"{BASE}/quotes/sf-1") for _ in range(5)])
        hits = (await c.get(f"{FAKE}/_hits")).json()["hits"]
        print("singleflight responses", [x.status_code for x in res], "catalog_hits", hits)

        await c.post(f"{FAKE}/_reset_hits")
        t = time.monotonic()
        r = await c.get(f"{BASE}/quotes/busy-1")
        dt = time.monotonic() - t
        hits = (await c.get(f"{FAKE}/_hits")).json()["hits"]
        print(f"503+retry time {dt:.2f}s status {r.status_code} catalog_hits {hits}")

        r1 = await c.get(f"{BASE}/quotes/nf-1")
        r2 = await c.get(f"{BASE}/quotes/nf-1")
        print("404 twice statuses", r1.status_code, r2.status_code)

        print("--- breaker: point at dead port (refused) ---")
        await c.post(f"{BASE}/catalog/source", json={"url": CATALOG_URL.replace("9000","9999")})
        for i in range(6):
            t = time.monotonic()
            r = await c.get(f"{BASE}/quotes/dead-{i}")
            print(f"fail {i}: status {r.status_code} {1000 * (time.monotonic() - t):.0f}ms")
        t = time.monotonic()
        r = await c.get(f"{BASE}/quotes/dead-open")
        print(f"open fast-fail: status {r.status_code} {1000 * (time.monotonic() - t):.0f}ms")

        print("--- recovery: back to live, wait for half-open ---")
        await c.post(f"{BASE}/catalog/source", json={"url": CATALOG_URL})
        await asyncio.sleep(float(__import__("os").getenv("BREAKER_WAIT", "4")))
        t = time.monotonic()
        r = await c.get(f"{BASE}/quotes/ok-2")
        print(f"recovered: status {r.status_code} X-Source={r.headers.get('x-source')} {1000 * (time.monotonic() - t):.0f}ms")

        print("stats1    ", (await c.get(f"{BASE}/stats")).json())


if __name__ == "__main__":
    asyncio.run(main())