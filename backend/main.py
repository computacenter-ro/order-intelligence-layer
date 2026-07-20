"""[5] Core Backend — FastAPI application entrypoint (CLAUDE.md [5]).

Assembles the ASGI app: the read-only REST API (``backend/api.py``), the
WebSocket feed (``backend/ws.py``), and — started in the app lifespan — the
RabbitMQ consumers + stalled-journey sweep (``backend/consumers.py``), all in
one process sharing a single WebSocket hub.

Run with::

    uvicorn backend.main:app --port 8000

Importing this module performs no I/O: the broker/DB connections happen only
when the lifespan starts (i.e. when a server or a TestClient context runs it),
and the consumer task is created (not awaited) so startup never blocks.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.api import router as api_router
from backend.consumers import run_consumers
from backend.ws import manager as hub
from backend.ws import router as ws_router


async def _run_consumers_guarded() -> None:
    """Run the consumers, wired to the shared WS hub, containing any failure.

    Cancellation (on shutdown) propagates cleanly; any other error (e.g. the
    broker is unreachable) is logged so it can't take the whole app down.
    """
    try:
        await run_consumers(on_event=hub.broadcast)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a broker outage must not crash the app
        print(f"[backend] consumers stopped: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the consumers + sweep as a background task, broadcasting through the
    # same hub the /ws endpoint registers clients into (API + WS + consumers
    # share one hub in one process).
    task = asyncio.create_task(_run_consumers_guarded())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Order Intelligence Layer — Core Backend",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(api_router)
app.include_router(ws_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
