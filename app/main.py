from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import ORJSONResponse
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.catalog import CatalogClient
from app.snapshot import iter_snapshot
from app.store import Store


class Stats:
    __slots__ = ("served_local", "served_from_catalog")

    def __init__(self) -> None:
        self.served_local = 0
        self.served_from_catalog = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store()
    app.state.catalog = CatalogClient()
    app.state.stats = Stats()
    app.state.tombstones = set()
    yield
    await app.state.catalog.aclose()


app = FastAPI(
    default_response_class=ORJSONResponse, docs_url=None, redoc_url=None, lifespan=lifespan
)

_NOT_FOUND = {"detail": "not found"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


class SourceBody(BaseModel):
    url: str


@app.post("/catalog/source")
async def set_source(body: SourceBody, request: Request) -> dict[str, str]:
    return {"source": request.app.state.catalog.set_source(body.url)}


@app.post("/import")
async def import_snapshot(request: Request) -> dict[str, int]:
    state = request.app.state
    imported, dropped = await state.store.replace(iter_snapshot(request))
    state.tombstones.clear()  # a fresh snapshot supersedes local deletions
    return {"imported": imported, "dropped": dropped}


class PutBody(BaseModel):
    author: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=16384)


@app.put("/catalog/{quote_id}")
async def put_quote(quote_id: str, body: PutBody, request: Request) -> dict[str, str]:
    state = request.app.state
    state.tombstones.discard(quote_id)
    state.store.put_record({"id": quote_id, "author": body.author, "text": body.text})
    return {"id": quote_id, "author": body.author, "text": body.text}


@app.delete("/catalog/{quote_id}")
async def delete_quote(quote_id: str, request: Request) -> Response:
    state = request.app.state
    if not state.store.has(quote_id) and quote_id not in state.tombstones:
        return ORJSONResponse(_NOT_FOUND, status_code=404)
    state.store.remove(quote_id)
    state.tombstones.add(quote_id)
    return ORJSONResponse({"deleted": True})


@app.get("/quotes/{quote_id}")
async def get_quote(quote_id: str, request: Request) -> Response:
    state = request.app.state
    if quote_id in state.tombstones:
        return ORJSONResponse(_NOT_FOUND, status_code=404)
    payload = state.store.get(quote_id)
    if payload is not None:
        state.stats.served_local += 1
        return Response(content=payload, media_type="application/json", headers={"X-Source": "LOCAL"})
    record = await state.catalog.fetch(quote_id)
    if record is None:
        return ORJSONResponse(_NOT_FOUND, status_code=404)  # not cached: new quotes appear often
    state.store.put_record(record)
    state.stats.served_from_catalog += 1
    return ORJSONResponse(record, headers={"X-Source": "CATALOG"})


@app.get("/stats")
async def stats(request: Request) -> dict[str, int]:
    state = request.app.state
    return {
        "served_local": state.stats.served_local,
        "served_from_catalog": state.stats.served_from_catalog,
        "catalog_reads": state.catalog.reads,
        "evictions": state.store.evictions,
        "local": state.store.local,
        "bytes": state.store.bytes,
        "catalog": state.store.catalog,
    }