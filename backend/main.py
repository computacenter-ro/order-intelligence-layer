"""[5] Core Backend — FastAPI application entrypoint (CLAUDE.md [5]).

Assembles the ASGI app and mounts the read-only REST API from ``backend/api.py``.
Run with::

    uvicorn backend.main:app --port 8000

The RabbitMQ consumers + stalled-journey sweep (``backend/consumers.py``) and the
WebSocket feed are separate concerns wired elsewhere; importing this module
performs no I/O (no broker/DB connection), so it is safe to import in tests.
"""

from __future__ import annotations

from fastapi import FastAPI

from backend.api import router

app = FastAPI(title="Order Intelligence Layer — Core Backend", version="0.1.0")
app.include_router(router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
