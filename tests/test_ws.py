"""Tests for backend/ws.py — the WebSocket hub + /ws endpoint.

The ConnectionManager is exercised directly with fake WebSockets (no server),
including the drop-a-dead-client path. The /ws endpoint's connect/disconnect
lifecycle is driven through fastapi's TestClient (in-process, no real network).
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.ws import (
    ConnectionManager,
    make_event,
    manager as app_manager,
    EVENT_ALERT_NEW,
    EVENT_JOURNEY_UPDATED,
    EVENT_JOURNEY_COMPLETED,
)


class _FakeWS:
    def __init__(self, *, fail: bool = False) -> None:
        self.accepted = False
        self.sent: list[dict] = []
        self._fail = fail

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        if self._fail:
            raise RuntimeError("client gone")
        self.sent.append(data)


# --- event format ------------------------------------------------------------


def test_make_event_shape():
    ev = make_event(EVENT_ALERT_NEW, {"alert_id": "a1"})
    assert ev == {"type": "alert.new", "data": {"alert_id": "a1"}}


def test_event_type_constants():
    assert EVENT_ALERT_NEW == "alert.new"
    assert EVENT_JOURNEY_UPDATED == "journey.updated"
    assert EVENT_JOURNEY_COMPLETED == "journey.completed"


# --- ConnectionManager -------------------------------------------------------


async def test_connect_accepts_and_registers():
    m = ConnectionManager()
    ws = _FakeWS()
    await m.connect(ws)
    assert ws.accepted is True
    assert len(m) == 1


def test_disconnect_removes_and_is_idempotent():
    m = ConnectionManager()
    ws = _FakeWS()
    m.disconnect(ws)  # unknown → no error
    assert len(m) == 0


async def test_broadcast_sends_to_all_clients():
    m = ConnectionManager()
    a, b = _FakeWS(), _FakeWS()
    await m.connect(a)
    await m.connect(b)
    event = make_event(EVENT_JOURNEY_UPDATED, {"journey_id": "J1"})
    await m.broadcast(event)
    assert a.sent == [event]
    assert b.sent == [event]


async def test_broadcast_drops_dead_clients():
    m = ConnectionManager()
    good, dead = _FakeWS(), _FakeWS(fail=True)
    await m.connect(good)
    await m.connect(dead)
    assert len(m) == 2
    event = make_event(EVENT_JOURNEY_COMPLETED, {"journey_id": "J9"})
    await m.broadcast(event)
    # the healthy client received it; the failing one was evicted
    assert good.sent == [event]
    assert len(m) == 1


async def test_broadcast_with_no_clients_is_noop():
    m = ConnectionManager()
    await m.broadcast(make_event(EVENT_ALERT_NEW, {}))  # must not raise
    assert len(m) == 0


# --- /ws endpoint lifecycle --------------------------------------------------


def test_ws_endpoint_registers_and_deregisters():
    assert len(app_manager) == 0
    with TestClient(app).websocket_connect("/ws"):
        assert len(app_manager) == 1
    # after the client disconnects the hub drops it
    assert len(app_manager) == 0


# =============================================================================
# (b) End-to-end: an event broadcast through the hub reaches a real WS client
# (FastAPI TestClient, in-process — no broker). A test-only POST route triggers
# the broadcast from inside the app's event loop, where the socket lives.
# =============================================================================

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI

from backend.ws import router as ws_router, manager as ws_manager
from shared.models import Department, LogLine, ProcessedAlert

BASE = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)


def _emit_app() -> FastAPI:
    """A minimal app: the /ws endpoint + a test-only route that broadcasts."""
    a = FastAPI()
    a.include_router(ws_router)

    @a.post("/_emit")
    async def _emit(event: dict) -> dict:
        await ws_manager.broadcast(event)
        return {"ok": True}

    return a


def test_broadcast_reaches_connected_ws_client_in_envelope_format():
    client = TestClient(_emit_app())
    event = make_event(EVENT_ALERT_NEW, {"alert_id": "a1"})
    with client.websocket_connect("/ws") as ws:
        client.post("/_emit", json=event)  # runs in the app loop -> broadcasts
        received = ws.receive_json()
    assert received == event
    assert set(received) == {"type", "data"}
    assert received["type"] == "alert.new"
    assert received["data"] == {"alert_id": "a1"}


def test_broadcast_reaches_all_connected_ws_clients():
    client = TestClient(_emit_app())
    event = make_event(EVENT_JOURNEY_UPDATED, {"journey_id": "J1"})
    with client.websocket_connect("/ws") as ws1, client.websocket_connect("/ws") as ws2:
        client.post("/_emit", json=event)
        assert ws1.receive_json() == event
        assert ws2.receive_json() == event


# =============================================================================
# (c) on_event is called with the right envelope at persist / attach / finalize.
# Fakes for consumer + assembler — no broker, no DB.
# =============================================================================


class _FakeResult:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """Async session usable both as a CM (consumers) and directly (assembler)."""

    def __init__(self, rowcount: int = 1) -> None:
        self._rowcount = rowcount
        self.commits = 0

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, stmt):
        return _FakeResult(self._rowcount)

    async def commit(self) -> None:
        self.commits += 1


def _sink():
    events: list[dict] = []

    async def on_event(ev: dict) -> None:
        events.append(ev)

    return events, on_event


def _log(*, message: str, ts: datetime = BASE, app_name: str = "cc-order-engine",
         level: str = "INFO", eventId=None, orderId=None, cartHeaderId=None,
         log_id: str | None = None) -> LogLine:
    return LogLine(
        log_id=log_id or f"log-{ts.isoformat()}",
        timestamp=ts,
        app_name=app_name,
        level=level,
        logger="c.c.test.Logger",
        host="CCECMEWEBT001",
        process_id="1",
        thread="t-1",
        eventId=eventId,
        orderId=orderId,
        cartHeaderId=cartHeaderId,
        message=message,
    )


def _alert(source: str = "ai") -> ProcessedAlert:
    return ProcessedAlert(
        alert_id="alert-1",
        emitted_at=BASE,
        log=_log(message="boom", level="ERROR"),
        explanation=None if source == "fallback" else "explained",
        department=None if source == "fallback" else Department.backend,
        confidence=None if source == "fallback" else 0.7,
        source=source,
    )


async def test_on_event_alert_new_on_persist():
    from backend.consumers import AlertsConsumer

    events, on_event = _sink()
    consumer = AlertsConsumer(session_factory=lambda: _FakeSession(rowcount=1), on_event=on_event)
    await consumer._process(_alert("ai"))

    assert [e["type"] for e in events] == ["alert.new"]
    assert set(events[0]) == {"type", "data"}
    assert events[0]["data"]["alert_id"] == "alert-1"
    assert events[0]["data"]["source"] == "ai"


async def test_on_event_journey_updated_on_attach():
    from backend.journeys import JourneyAssembler

    events, on_event = _sink()
    a = JourneyAssembler()
    await a.ingest(
        _FakeSession(),
        [_log(message="Received inbound order event evt-1", eventId="evt-1")],
        now=BASE + timedelta(seconds=1),
        on_event=on_event,
    )
    assert [e["type"] for e in events] == ["journey.updated"]
    assert set(events[0]) == {"type", "data"}
    assert events[0]["data"]["status"] == "IN_PROGRESS"
    assert events[0]["data"]["event_id"] == "evt-1"


async def test_on_event_journey_completed_on_finalize():
    from backend.journeys import JourneyAssembler

    events, on_event = _sink()
    a = JourneyAssembler()
    logs = [
        _log(message="Received inbound order event evt-1", eventId="evt-1"),
        _log(message="Received order creation response for event evt-1",
             ts=BASE + timedelta(seconds=2), eventId="evt-1", orderId="ORD-1", cartHeaderId="C1"),
        _log(message="Registered order ORD-1 for tracking",
             ts=BASE + timedelta(seconds=5), app_name="cc-track-trace",
             orderId="ORD-1", cartHeaderId="C1"),
    ]
    await a.ingest(_FakeSession(), logs, now=BASE + timedelta(seconds=6), on_event=on_event)

    # completes in this chunk -> only journey.completed (not journey.updated)
    assert [e["type"] for e in events] == ["journey.completed"]
    data = events[0]["data"]
    assert data["status"] == "SUCCESS" and data["outcome"] == "SUCCESS"
    assert "summary" in data
    assert [ev["log_id"] for ev in data["events"]] == [l.log_id for l in logs]
