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
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend import teams
from backend.api import router as api_router
from backend.auth import router as auth_router
from backend.auth_entra import router as entra_router
from backend.consumers import run_consumers
from backend.report import report_loop
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


CONSUMERS_RETRY_DELAY = int(os.getenv("CONSUMERS_RETRY_DELAY", "5"))


async def _run_consumers_guarded() -> None:
    """Run the consumers, wired to the fan-out sink, retrying on any failure.

    Cancellation (on shutdown) propagates cleanly. Any other error (e.g. the
    broker is unreachable) is logged and retried after a short delay — it must
    never crash the app, and it must never permanently give up.

    The retry is what makes a slow/unavailable broker survivable. ``aio_pika``'s
    ``connect_robust`` only re-establishes a connection that was already open and
    then dropped; it does NOT retry the *initial* connect, so a broker that isn't
    accepting AMQP connections yet at startup (the docker-compose boot race:
    ``service_healthy`` fires on the diagnostics ping, a beat before the listener
    accepts) raises here. Without this loop that first failure would kill the
    consumer task for the life of the process while uvicorn stayed up — the API
    and WebSocket keep working but nothing drains ``raw.events`` /
    ``processed.alerts``, so the dashboard goes silent. The loop also covers a
    broker that disappears mid-run.
    """
    while True:
        try:
            await run_consumers(on_event=_fan_out)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a broker outage must not crash the app
            print(
                f"[backend] consumers error, retrying in {CONSUMERS_RETRY_DELAY}s: {exc}",
                flush=True,
            )
            await asyncio.sleep(CONSUMERS_RETRY_DELAY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the consumers + sweep as a background task. Events fan out to the WS
    # hub (the same one the /ws endpoint registers clients into) and to Teams —
    # API + WS + consumers share one hub in one process.
    #
    # The twice-daily Teams report is a SEPARATE task, deliberately not part of
    # run_consumers' gather and NOT wired to _fan_out:
    #   * not in the gather, because that coroutine owns the RabbitMQ connection and
    #     is torn down and retried whenever the broker blips — which would take the
    #     reporter with it, exactly when a report still matters. It reads Postgres
    #     and Redis and never the broker.
    #   * not through _fan_out, because that also feeds the WebSocket hub and the
    #     dashboard has no use for a report event (hence no `report.daily` in
    #     backend/ws.py). The loop calls teams.notify directly.
    tasks = [
        asyncio.create_task(_run_consumers_guarded()),
        asyncio.create_task(report_loop()),
    ]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — shutdown must not raise
                print(f"[backend] background task failed on shutdown: {exc}", flush=True)


def cors_allow_origins() -> list[str]:
    """The browser origins allowed to call this API, from ``CORS_ALLOW_ORIGINS``.

    Comma-separated, e.g.
    ``https://dash.example.com,https://dash-staging.example.com``. The default is
    the local dashboard, so docker-compose dev is unchanged.

    Env-driven because the deployed dashboard lives on a different HTTPS
    subdomain than the backend (Azure Container Apps gives each app its own
    hostname), and that hostname is a deployment detail — baking it in would mean
    a code change per environment.

    Entries are trimmed and blanks dropped, so a trailing comma or a wrapped
    value in a compose/portal field can't inject an empty origin. An empty result
    means "no cross-origin browser calls", NOT "allow everything": with
    ``allow_credentials=True`` a wildcard is rejected by browsers anyway, so
    silently widening would be both wrong and unsafe.
    """
    raw = os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


app = FastAPI(
    title="Order Intelligence Layer — Core Backend",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
    # POST for /auth/login + /auth/logout; PATCH for marking an alert resolved;
    # OPTIONS for the preflight.
    allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    # Required so the browser sends/receives the httpOnly session cookie
    # cross-origin (dashboard :3000 → backend :8000, or the two deployed
    # subdomains). Note this forbids a "*" origin — hence the explicit list.
    allow_credentials=True,
)
app.include_router(auth_router)
app.include_router(entra_router)
app.include_router(api_router)
app.include_router(ws_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    """Run the ASGI app, so ``python -m backend.main`` works as CLAUDE.md documents.

    Without this the module merely defined ``app`` and exited **silently** — zero
    output, exit code 0 — so the documented dev command started no server, no
    consumers and no report loop, while looking like it had done something. The
    symptom is indistinguishable from "everything is fine but nothing happened",
    which is the worst kind: the twice-daily Teams report simply never fired, and
    the only clue was an unset ``teams:digest:last_sent`` watermark.

    ``ai_service/main.py`` has always had this block, so the two services in the
    same CLAUDE.md code block behaved differently under the same invocation shape.

    Both compose files run ``uvicorn backend.main:app`` directly and are unaffected;
    this is purely the local-dev entrypoint. Host and port stay env-driven so it
    matches how the containers are configured rather than hardcoding :8000 twice.
    """
    import os

    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("BACKEND_HOST", "127.0.0.1"),
        port=int(os.getenv("BACKEND_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
