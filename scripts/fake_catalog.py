"""Fake catalog for local load testing. Modes are selected by id prefix."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import ORJSONResponse

app = FastAPI(default_response_class=ORJSONResponse, docs_url=None, redoc_url=None)

STATE = {"down": False, "hits": 0}


@app.get("/quote/{quote_id}")
async def quote(quote_id: str) -> ORJSONResponse:
    STATE["hits"] += 1
    if STATE["down"] or quote_id.startswith("busy"):
        return ORJSONResponse({"detail": "busy"}, status_code=503, headers={"Retry-After": "1"})
    if quote_id.startswith("nf"):
        return ORJSONResponse({"detail": "not found"}, status_code=404)
    if quote_id.startswith("slow"):
        await asyncio.sleep(30)
    if quote_id.startswith("lag"):
        await asyncio.sleep(0.8)  # slower than request jitter, under the 2s read timeout
    return ORJSONResponse(
        {
            "id": quote_id,
            "author": "Марк Аврелий",
            "text": f"цитата {quote_id}",
            "tags": ["стоицизм"],
            "year": 170,
            "lang": "ru",
        }
    )


@app.post("/_down")
async def down() -> dict[str, bool]:
    STATE["down"] = True
    return {"down": True}


@app.post("/_up")
async def up() -> dict[str, bool]:
    STATE["down"] = False
    return {"down": False}


@app.get("/_hits")
async def hits() -> dict[str, int]:
    return {"hits": STATE["hits"]}


@app.post("/_reset_hits")
async def reset_hits() -> dict[str, int]:
    STATE["hits"] = 0
    return {"hits": 0}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=9000)