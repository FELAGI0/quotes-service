from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import orjson
from fastapi import HTTPException
from starlette.requests import Request

_OPEN = b'{"quotes":['
_EMPTY = b'{"quotes":[]}'
_CLOSE = b"]}"
_FLUSH_EVERY = 1000
_REQUIRED = ("id", "author", "text")


class EnvelopeError(Exception):
    """Malformed snapshot envelope (broken ``{"quotes":[`` / ``]}``)."""


async def _iter_lines(request: Request) -> AsyncIterator[bytes]:
    """Split the request body into lines without holding it whole in memory."""
    buffer = bytearray()
    async for chunk in request.stream():
        buffer += chunk
        nl = buffer.rfind(b"\n")
        if nl == -1:
            continue
        block = bytes(buffer[: nl + 1])
        del buffer[: nl + 1]
        for line in block.split(b"\n")[:-1]:
            yield line
    if buffer:
        yield bytes(buffer)


async def _drain(request: Request) -> None:
    try:
        async for _ in request.stream():
            pass
    except Exception:  # best effort: the client may have gone away
        pass


async def iter_snapshot(request: Request) -> AsyncIterator[dict]:
    """Yield valid records from a snapshot stream.

    The envelope is validated as it is read; broken individual records are
    skipped. A malformed envelope drains the rest of the body and raises 400.
    """
    state = 0  # 0 = expect open line, 1 = records, 2 = closed
    seen = 0
    try:
        async for raw in _iter_lines(request):
            line = raw.strip()
            if not line:
                continue
            compact = line.replace(b" ", b"")
            if state == 0:
                if compact == _EMPTY:
                    state = 2
                    continue
                if compact != _OPEN:
                    raise EnvelopeError("expected first line '{\"quotes\":['")
                state = 1
                continue
            if state == 2:
                continue
            if compact == _CLOSE:
                state = 2
                continue
            try:
                record = orjson.loads(line.rstrip(b","))
            except Exception:
                continue
            if not isinstance(record, dict):
                continue
            if any(not isinstance(record.get(k), str) or not record[k] for k in _REQUIRED):
                continue
            seen += 1
            if seen % _FLUSH_EVERY == 0:
                await asyncio.sleep(0)
            yield record
    except EnvelopeError as exc:
        await _drain(request)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if state != 2:
        raise HTTPException(status_code=400, detail="unterminated snapshot: missing ']}'")