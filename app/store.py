from __future__ import annotations

from array import array
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import AsyncIterator

import orjson

# Bodies are packed into one contiguous buffer, not stored as one Python object
# per record: 340k individual bytes objects cost ~1.4 GB in allocator overhead,
# the same payload packed costs ~110 MB. `_budget` caps resident bodies; LRU
# eviction is a safety valve, not the normal path.
DEFAULT_BUDGET = 160 * 1024 * 1024
_EVICTED = -1  # id is known, but its body is no longer resident


def _in(sorted_fps: array, fp: int) -> bool:
    i = bisect_left(sorted_fps, fp)
    return i < len(sorted_fps) and sorted_fps[i] == fp


class Store:
    __slots__ = ("_buf", "_index", "_order", "_bytes", "_dead", "_budget", "evictions")

    def __init__(self, budget_bytes: int = DEFAULT_BUDGET) -> None:
        self._buf = bytearray()  # [len:u32][payload] per resident body, packed
        self._index: dict[str, int] = {}  # id -> offset of its length prefix, or _EVICTED
        self._order: OrderedDict[str, None] = OrderedDict()  # resident ids, oldest first
        self._bytes = 0  # resident payload bytes
        self._dead = 0  # buffer bytes no longer referenced by a resident id
        self._budget = budget_bytes
        self.evictions = 0

    @property
    def local(self) -> int:
        return len(self._order)

    @property
    def catalog(self) -> int:
        return len(self._index)

    @property
    def bytes(self) -> int:
        return self._bytes

    def has(self, qid: str) -> bool:
        return qid in self._index

    def get(self, qid: str) -> bytes | None:
        off = self._index.get(qid, _EVICTED)
        if off < 0:
            return None
        length = int.from_bytes(self._buf[off : off + 4], "little")
        self._order.move_to_end(qid)
        return bytes(self._buf[off + 4 : off + 4 + length])

    def put_record(self, record: dict) -> None:
        self.put_bytes(record["id"], orjson.dumps(record))

    def put_bytes(self, qid: str, payload: bytes) -> None:
        off = len(self._buf)
        self._buf += len(payload).to_bytes(4, "little")
        self._buf += payload
        self._index[qid] = off
        self._order[qid] = None
        self._bytes += len(payload)
        self._reclaim()

    def remove(self, qid: str) -> None:
        self._order.pop(qid, None)
        off = self._index.pop(qid, None)
        if off is not None and off >= 0:
            self._release(off)

    async def replace(self, records: AsyncIterator[dict]) -> tuple[int, int]:
        """Load a full snapshot. Returns (imported, dropped).

        A snapshot is the whole catalog, so every body is rewritten anyway.
        The old ids are kept only as sorted 64-bit fingerprints (a few MB) so
        the old index and buffer can be freed before loading; holding the old
        id strings instead costs ~35 MB and pushes the second import past the
        container's memory limit.

        The reset is deferred until the first good record, so a broken envelope
        (bad first line) raises 400 without touching the current data.
        """
        old_fps = array("q", sorted(hash(qid) for qid in self._index))
        old_count = len(self._index)
        it = records.__aiter__()
        try:
            first = await it.__anext__()
        except StopAsyncIteration:
            self._reset()  # valid empty snapshot: the catalog is now empty
            return 0, old_count
        self._reset()
        imported = 0
        retained = 0
        record = first
        while True:
            qid = record["id"]
            if _in(old_fps, hash(qid)):
                retained += 1
            self.put_bytes(qid, orjson.dumps(record))
            imported += 1
            try:
                record = await it.__anext__()
            except StopAsyncIteration:
                break
        return imported, old_count - retained

    def _reset(self) -> None:
        self._buf.clear()
        self._index.clear()
        self._order.clear()
        self._bytes = 0
        self._dead = 0

    def _release(self, off: int) -> None:
        length = int.from_bytes(self._buf[off : off + 4], "little")
        self._dead += 4 + length
        self._bytes -= length

    def _drop_body(self, qid: str) -> None:
        self._order.pop(qid, None)
        off = self._index.get(qid, _EVICTED)
        if off >= 0:
            self._release(off)
            self._index[qid] = _EVICTED

    def _reclaim(self) -> None:
        while self._bytes > self._budget and self._order:
            self._drop_body(next(iter(self._order)))
            self.evictions += 1
        if self._dead > self._budget // 2:
            self._compact()

    def _compact(self) -> None:
        """Slide resident bodies to the front of the buffer, in place."""
        buf = self._buf
        write = 0
        for qid in self._order:  # oldest -> newest
            off = self._index[qid]
            length = int.from_bytes(buf[off : off + 4], "little")
            total = 4 + length
            if off != write:
                buf[write : write + total] = buf[off : off + total]
                self._index[qid] = write
            write += total
        del buf[write:]
        self._dead = 0


if __name__ == "__main__":
    import asyncio

    async def _gen(records: list[dict]):
        for r in records:
            yield r

    async def _main() -> None:
        s = Store(budget_bytes=60)
        imported, dropped = await s.replace(
            _gen([{"id": "a", "text": "x" * 30}, {"id": "b", "text": "y"}])
        )
        assert (imported, dropped) == (2, 0), (imported, dropped)
        assert (s.catalog, s.local) == (2, 1), (s.catalog, s.local)
        assert s.evictions == 1 and s.bytes <= 60, (s.evictions, s.bytes)
        assert s.has("a") and s.get("a") is None, "evicted body, id still known"
        assert s.get("b") == b'{"id":"b","text":"y"}', s.get("b")
        imported, dropped = await s.replace(_gen([{"id": "a", "text": "z"}, {"id": "c", "text": "w"}]))
        assert (imported, dropped) == (2, 1), (imported, dropped)
        assert s.catalog == 2 and s.has("a") and not s.has("b"), "a kept, b dropped"
        for i in range(50):  # churn to exercise eviction + compaction
            s.put_bytes(f"k{i}", b"p" * 16)
        assert s.bytes <= 60 and s._dead < 60, (s.bytes, s._dead)
        assert s.get("k49") == b"p" * 16
        print("store self-check ok", {"local": s.local, "bytes": s.bytes, "catalog": s.catalog})

    asyncio.run(_main())