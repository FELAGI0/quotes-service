"""Fast contract check for stages 5-8. Run in a container on the service network."""

from __future__ import annotations

import asyncio
import os

import httpx

BASE = os.getenv("BASE", "http://qs:8000")
FAKE = os.getenv("FAKE", "http://fake:9000")

SNAP = b'{"quotes":[\n' + b"".join(
    b'{"id":"q-%03d","author":"A%d","text":"t%d","tags":["x"],"year":1,"lang":"ru"},\n' % (i, i, i)
    for i in range(100)
) + b"]}"


def line(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}{'  ' + detail if detail else ''}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as c:
        s0 = (await c.get(f"{BASE}/stats")).json()

        # 2-3. catalog source
        r = await c.post(f"{BASE}/catalog/source", json={"url": FAKE})
        line("POST /catalog/source -> 200 {source}", r.status_code == 200 and r.json()["source"] == FAKE, str(r.json()))

        # 4. small snapshot import
        r = await c.post(f"{BASE}/import", content=SNAP, headers={"Content-Type": "application/json"})
        j = r.json()
        line("POST /import 100 -> imported=100 dropped=0", r.status_code == 200 and j == {"imported": 100, "dropped": 0}, str(j))

        # 5. GET from snapshot -> LOCAL
        r = await c.get(f"{BASE}/quotes/q-007")
        line("GET snapshot id -> 200 LOCAL, full body", r.status_code == 200 and r.headers.get("x-source") == "LOCAL" and r.json()["id"] == "q-007", r.text[:80])

        # 6. GET unknown -> CATALOG, second time -> LOCAL
        r1 = await c.get(f"{BASE}/quotes/ok-1")
        r2 = await c.get(f"{BASE}/quotes/ok-1")
        line("GET unknown -> 200 CATALOG", r1.status_code == 200 and r1.headers.get("x-source") == "CATALOG", r1.text[:80])
        line("GET same again -> 200 LOCAL (cached)", r2.status_code == 200 and r2.headers.get("x-source") == "LOCAL", r2.headers.get("x-source", ""))

        # 6b. 404 not cached: two reads both hit catalog
        h0 = (await c.get(f"{FAKE}/_hits")).json()["hits"]
        a = await c.get(f"{BASE}/quotes/nf-1")
        b = await c.get(f"{BASE}/quotes/nf-1")
        h1 = (await c.get(f"{FAKE}/_hits")).json()["hits"]
        line("404 twice -> 404/404, both hit catalog (not cached)", a.status_code == 404 and b.status_code == 404 and h1 - h0 == 2, f"hits+{h1-h0}")

        # 7. PUT -> 200, then GET LOCAL
        r = await c.put(f"{BASE}/catalog/q-new", json={"author": "Марк Аврелий", "text": "новая цитата"})
        line("PUT new -> 200 {id,author,text}", r.status_code == 200 and r.json() == {"id": "q-new", "author": "Марк Аврелий", "text": "новая цитата"}, str(r.json()))
        r = await c.get(f"{BASE}/quotes/q-new")
        line("GET after PUT -> 200 LOCAL", r.status_code == 200 and r.headers.get("x-source") == "LOCAL" and r.json()["text"] == "новая цитата", r.text[:80])

        # 8. boundary validation
        r = await c.put(f"{BASE}/catalog/q-bad", json={"author": "a" * 201, "text": "x"})
        line("PUT author=201 -> 422", r.status_code == 422, str(r.status_code))
        r = await c.put(f"{BASE}/catalog/q-bad", json={"author": "", "text": "x"})
        line("PUT author='' -> 422", r.status_code == 422, str(r.status_code))
        r = await c.put(f"{BASE}/catalog/q-bad", json={"author": "a", "text": "x" * 16385})
        line("PUT text=16385 -> 422", r.status_code == 422, str(r.status_code))
        r = await c.put(f"{BASE}/catalog/q-ok", json={"author": "а", "text": "я" * 16384})
        line("PUT exact limits (author=1,text=16384) -> 200", r.status_code == 200, str(r.status_code))

        # 9. DELETE -> 200, GET -> 404
        r = await c.delete(f"{BASE}/catalog/q-new")
        line("DELETE existing -> 200 {deleted:true}", r.status_code == 200 and r.json() == {"deleted": True}, str(r.json()))
        r = await c.get(f"{BASE}/quotes/q-new")
        line("GET deleted -> 404 (tombstone, no catalog call)", r.status_code == 404, str(r.status_code))
        r = await c.delete(f"{BASE}/catalog/q-absent")
        line("DELETE absent -> 404", r.status_code == 404, str(r.status_code))

        # snapshot resets tombstones
        await c.post(f"{BASE}/import", content=b'{"quotes":[\n{"id":"q-new","author":"A","text":"restored"},\n]}', headers={"Content-Type": "application/json"})
        r = await c.get(f"{BASE}/quotes/q-new")
        line("after re-import, tombstoned id visible again -> 200", r.status_code == 200, f"{r.status_code} {r.headers.get('x-source')}")

        # 10. stats shape + deltas
        s1 = (await c.get(f"{BASE}/stats")).json()
        keys = {"served_local", "served_from_catalog", "catalog_reads", "evictions", "local", "bytes", "catalog"}
        line("GET /stats has all 7 fields", set(s1) == keys, str(sorted(s1)))
        line("counters moved (served_local/catalog/reads up)",
             s1["served_local"] > s0["served_local"] and s1["served_from_catalog"] > s0["served_from_catalog"] and s1["catalog_reads"] > s0["catalog_reads"])
        print("  stats0:", s0)
        print("  stats1:", s1)


if __name__ == "__main__":
    asyncio.run(main())