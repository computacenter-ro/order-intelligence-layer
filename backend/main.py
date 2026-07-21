"""[5] Core Backend — FastAPI application entrypoint (CLAUDE.md [5]).

Assembles the ASGI app: the read-only REST API (``backend/api.py``), the
WebSocket feed (``backend/ws.py``), and — started in the app lifespan — the
RabbitMQ consumers + stalled-journey sweep (``backend/consumers.py``), all in
one process sharing a single WebSocket hub.

The consumers are wired to a **fan-out** ``on_event`` that delivers each event
to every sink (the WebSocket hub and Microsoft Teams). Sinks are isolated: one
sink failing (e.g. Teams is down) is caught and logged and never blocks the
other sink or the consumers.

Run with::

    uvicorn backend.main:app --port 8000

Importing this module performs no I/O: the broker/DB connections happen only
when the lifespan starts (i.e. when a server or a TestClient context runs it),
and the consumer task is created (not awaited) so startup never blocks.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv(override=False)  # .env → os.getenv (TEAMS_WEBHOOK_* / DASHBOARD_URL)

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import teams
from backend.api import router as api_router
from backend.consumers import run_consumers
from backend.ws import manager as hub
from backend.ws import router as ws_router

def _sinks():
    """The event sinks, in delivery order, resolved at call time.

    Each entry is (name, async callable taking the {"type","data"} event).
    Resolving here (not at import) keeps the reference live if a sink is
    reconfigured or swapped.
    """
    return (
        ("ws", hub.broadcast),
        ("teams", teams.notify),
    )


async def _fan_out(event: dict) -> None:
    """Deliver one event to every sink, isolating failures per sink.

    A sink raising (e.g. Teams is unreachable) is caught and logged so it never
    stops the other sinks — nor, since ``on_event`` is awaited inside the
    consumers, the consumers themselves.
    """
    for name, sink in _sinks():
        try:
            await sink(event)
        except Exception as exc:  # noqa: BLE001 — one sink must not break the others
            print(
                f"[backend] {name} sink failed for {event.get('type')!r}: {exc}",
                flush=True,
            )


async def _run_consumers_guarded() -> None:
    """Run the consumers, wired to the fan-out sink, containing any failure.

    Cancellation (on shutdown) propagates cleanly; any other error (e.g. the
    broker is unreachable) is logged so it can't take the whole app down.
    """
    try:
        await run_consumers(on_event=_fan_out)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a broker outage must not crash the app
        print(f"[backend] consumers stopped: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the consumers + sweep as a background task. Events fan out to the WS
    # hub (the same one the /ws endpoint registers clients into) and to Teams —
    # API + WS + consumers share one hub in one process.
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(api_router)
app.include_router(ws_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
