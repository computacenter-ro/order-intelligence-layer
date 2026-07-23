"""Tests for backend/consumers.py — the two idempotent output-queue consumers.

Exercised without a broker or a DB:

* ``alert_row_values`` — pure mapping ProcessedAlert -> Alert columns; fallback
  alerts carry null explanation/department/confidence.
* ``_process`` on each consumer — with a fake session (and, for raw events, a
  fake assembler) we assert the persistence call shape without Postgres.
* **Idempotency** is asserted structurally: the alert INSERT compiles to an
  ``ON CONFLICT DO NOTHING`` statement, so a redelivered ``alert_id`` (or
  ``log_id``) can never duplicate a row; raw events delegate to the already
  idempotent JourneyAssembler.
* Queue names come from the environment with the documented defaults.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from shared.models import Department, LogLine, ProcessedAlert
from backend.consumers import (
    AlertsConsumer,
    RawEventsConsumer,
    _QueueConsumer,
    alert_row_values,
    PROCESSED_ALERTS_QUEUE,
    RAW_EVENTS_QUEUE,
    RABBITMQ_URL,
)
from backend.journeys import JourneyAssembler, JourneyStatus, TIMED_OUT


def _log(**over) -> LogLine:
    base = dict(
        log_id="log-1",
        timestamp=datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc),
        app_name="cc-order-engine",
        level="ERROR",
        logger="c.c.orderengine.service.OrderService",
        host="CCECMEWEBT001",
        process_id="1234",
        thread="rabbit-listener-1",
        message="boom",
        eventId="evt-1",
        orderId="ORD-6001",
        cartHeaderId="1840927365018240001",
        accountNumber="81036533",
    )
    base.update(over)
    return LogLine(**base)


def _alert(source: str = "ai", **over) -> ProcessedAlert:
    return ProcessedAlert(
        alert_id=over.pop("alert_id", "alert-1"),
        emitted_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=timezone.utc),
        log=over.pop("log", _log()),
        explanation=None if source == "fallback" else "SPT was unreachable",
        department=None if source == "fallback" else Department.backend,
        confidence=None if source == "fallback" else 0.82,
        source=source,
    )


# --- fakes -------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rowcount: int = 1) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """Records execute() statements and commit()s; usable as an async CM."""

    def __init__(self, rowcount: int = 1) -> None:
        self.executed: list[object] = []
        self.commits = 0
        self._rowcount = rowcount

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def execute(self, stmt):
        self.executed.append(stmt)
        return _FakeResult(self._rowcount)

    async def commit(self) -> None:
        self.commits += 1


class _FakeAssembler:
    def __init__(self) -> None:
        self.ingested: list[tuple[object, list, object]] = []

    async def ingest(self, session, logs, now=None, on_event=None):
        self.ingested.append((session, list(logs), on_event))
        return []


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --- alert_row_values (pure) -------------------------------------------------


def test_alert_row_values_ai_maps_log_and_enrichment():
    values = alert_row_values(_alert("ai"))
    assert values["alert_id"] == "alert-1"
    assert values["log_id"] == "log-1"
    assert values["level"] == "ERROR"
    assert values["app_name"] == "cc-order-engine"
    assert values["message"] == "boom"
    assert values["event_id"] == "evt-1"
    assert values["order_id"] == "ORD-6001"
    assert values["cart_header_id"] == "1840927365018240001"
    assert values["account_number"] == "81036533"
    assert values["explanation"] == "SPT was unreachable"
    assert values["department"] == "backend"  # enum -> its string value
    assert values["confidence"] == 0.82
    assert values["source"] == "ai"


def test_alert_row_values_fallback_nulls_enrichment():
    values = alert_row_values(_alert("fallback"))
    assert values["source"] == "fallback"
    assert values["explanation"] is None
    assert values["department"] is None
    assert values["confidence"] is None
    # the log fields are still present
    assert values["log_id"] == "log-1"


# --- AlertsConsumer ----------------------------------------------------------


def test_alerts_decode_parses_processedalert():
    body = _alert("ai").model_dump_json().encode()
    alert = AlertsConsumer()._decode(body)
    assert isinstance(alert, ProcessedAlert)
    assert alert.alert_id == "alert-1"


async def test_alerts_process_persists_with_on_conflict_do_nothing():
    session = _FakeSession()
    consumer = AlertsConsumer(session_factory=lambda: session)
    await consumer._process(_alert("fallback"))
    assert session.commits == 1
    # A new alert runs two statements: the INSERT, then a linking UPDATE that
    # attaches it to its journey (Fix 2). The first is the idempotent insert.
    assert len(session.executed) == 2
    # idempotency is enforced by ON CONFLICT DO NOTHING (dedup on the unique
    # alert_id / log_id) so a redelivered message inserts nothing new.
    assert "ON CONFLICT DO NOTHING" in _compiled(session.executed[0])


async def test_alerts_process_skips_linking_when_duplicate():
    # A redelivered alert (rowcount 0 = nothing inserted) must NOT run the
    # linking UPDATE — only the insert attempt.
    session = _FakeSession(rowcount=0)
    consumer = AlertsConsumer(session_factory=lambda: session)
    await consumer._process(_alert("fallback"))
    assert len(session.executed) == 1  # insert only, no link


# --- RawEventsConsumer -------------------------------------------------------


def test_raw_decode_parses_logline():
    body = _log(message="hello").model_dump_json().encode()
    log = RawEventsConsumer()._decode(body)
    assert isinstance(log, LogLine)
    assert log.message == "hello"


async def test_raw_process_forwards_log_to_assembler():
    session = _FakeSession()
    assembler = _FakeAssembler()
    consumer = RawEventsConsumer(session_factory=lambda: session, assembler=assembler)
    log = _log(message="one")
    await consumer._process(log)
    assert len(assembler.ingested) == 1
    passed_session, passed_logs, passed_on_event = assembler.ingested[0]
    assert passed_session is session
    assert passed_logs == [log]
    assert passed_on_event is None  # no hub wired -> no broadcasting


async def test_raw_process_forwards_on_event_to_assembler():
    session = _FakeSession()
    assembler = _FakeAssembler()
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    consumer = RawEventsConsumer(
        session_factory=lambda: session, assembler=assembler, on_event=sink
    )
    await consumer._process(_log(message="one"))
    _s, _logs, passed_on_event = assembler.ingested[0]
    assert passed_on_event is sink  # the assembler does the actual emitting


# --- config ------------------------------------------------------------------


def test_queue_defaults_from_env():
    assert RAW_EVENTS_QUEUE == "raw.events"
    assert PROCESSED_ALERTS_QUEUE == "processed.alerts"
    assert RABBITMQ_URL.startswith("amqp://")
    assert AlertsConsumer().queue == "processed.alerts"
    assert RawEventsConsumer().queue == "raw.events"
    assert AlertsConsumer(queue="custom.alerts").queue == "custom.alerts"


# --- resilience: one bad message must not kill the consume loop --------------
#
# Fakes that mimic just enough of aio-pika to drive _QueueConsumer.run() with no
# broker: a channel that hands back a fake queue, a queue whose iterator yields
# our messages, and a message.process() context manager that (like aio-pika)
# acks on success and, on an exception, records the requeue flag and re-raises.


class _FakeProcessCtx:
    def __init__(self, message: "_FakeMessage", requeue: bool) -> None:
        self._message = message
        self._requeue = requeue

    async def __aenter__(self) -> "_FakeProcessCtx":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._message.requeued = self._requeue  # nack + requeue
            return False  # propagate (do NOT suppress) — matches aio-pika
        self._message.acked = True
        return False


class _FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.acked = False
        self.requeued: bool | None = None

    def process(self, requeue: bool = True, ignore_processed: bool = True):
        return _FakeProcessCtx(self, requeue)


class _FakeQueueIterator:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    async def __aenter__(self) -> "_FakeQueueIterator":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def __aiter__(self) -> "_FakeQueueIterator":
        return self

    async def __anext__(self) -> _FakeMessage:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FakeQueue:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = messages

    def iterator(self) -> _FakeQueueIterator:
        return _FakeQueueIterator(self._messages)


class _FakeChannel:
    def __init__(self, queue: _FakeQueue) -> None:
        self._queue = queue

    async def set_qos(self, prefetch_count: int = 1) -> None:
        pass

    async def declare_queue(self, name: str, durable: bool = False) -> _FakeQueue:
        return self._queue


class _CountingConsumer(_QueueConsumer):
    """Decodes bytes to str; records processed payloads; can fail on demand."""

    def __init__(self, *, fail_on: set[str] | None = None, **kw) -> None:
        super().__init__(queue="test.q", **kw)
        self.processed: list[str] = []
        self._fail_on = fail_on or set()

    def _decode(self, body: bytes) -> str:
        return body.decode()

    async def _process(self, payload: str) -> None:
        if payload in self._fail_on:
            raise RuntimeError(f"boom on {payload}")
        self.processed.append(payload)


async def test_process_exception_does_not_stop_the_loop():
    messages = [_FakeMessage(b"a"), _FakeMessage(b"b"), _FakeMessage(b"c")]
    channel = _FakeChannel(_FakeQueue(list(messages)))
    consumer = _CountingConsumer(fail_on={"b"}, channel=channel)

    await consumer.run()

    # "b" raised, yet the loop kept going and processed "c".
    assert consumer.processed == ["a", "c"]
    # good messages were acked; the failing one was nacked + requeued.
    assert messages[0].acked is True
    assert messages[2].acked is True
    assert messages[1].requeued is True


# --- sweep_stalled finalizes a timed-out journey ----------------------------


async def test_sweep_stalled_finalizes_timed_out_journey():
    # An in-progress journey whose last activity is older than the timeout must
    # be finalized as TIMED_OUT by the sweep (no new message needed).
    start = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)
    assembler = JourneyAssembler(stalled_timeout=90)
    assembler.add([_log(log_id="x-1", timestamp=start, message="Received inbound order event evt-1")])

    session = _FakeSession()
    now = start + timedelta(seconds=120)  # well past the 90s stall window
    completions = await assembler.sweep_stalled(session, now=now)

    assert len(completions) == 1
    assert completions[0].status is JourneyStatus.TIMED_OUT
    assert completions[0].outcome == TIMED_OUT
    # the finalization was persisted (an UPDATE + a commit)
    assert len(session.executed) == 1
    assert session.commits == 1


async def test_sweep_stalled_leaves_fresh_journey_in_progress():
    start = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)
    assembler = JourneyAssembler(stalled_timeout=90)
    assembler.add([_log(log_id="y-1", timestamp=start, message="Received inbound order event evt-2")])

    session = _FakeSession()
    now = start + timedelta(seconds=30)  # still within the window
    completions = await assembler.sweep_stalled(session, now=now)

    assert completions == []
    assert session.commits == 0  # nothing finalized, nothing committed


# --- alert.new broadcast -----------------------------------------------------


async def test_alerts_process_broadcasts_alert_new_after_persist():
    session = _FakeSession(rowcount=1)  # a genuinely new row
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    consumer = AlertsConsumer(session_factory=lambda: session, on_event=sink)
    await consumer._process(_alert("ai"))

    assert session.commits == 1  # persisted first
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "alert.new"
    assert ev["data"]["alert_id"] == "alert-1"
    assert ev["data"]["source"] == "ai"
    assert ev["data"]["department"] == "backend"


async def test_alerts_process_does_not_broadcast_a_duplicate():
    # ON CONFLICT DO NOTHING inserts nothing on redelivery (rowcount 0) — no
    # WebSocket push, so at-least-once delivery stays idempotent end-to-end.
    session = _FakeSession(rowcount=0)
    events: list[dict] = []

    async def sink(ev):
        events.append(ev)

    consumer = AlertsConsumer(session_factory=lambda: session, on_event=sink)
    await consumer._process(_alert("ai"))

    assert session.commits == 1
    assert events == []


async def test_alerts_process_without_hub_does_not_broadcast():
    session = _FakeSession()
    consumer = AlertsConsumer(session_factory=lambda: session)  # on_event=None
    await consumer._process(_alert("fallback"))  # must not raise
    assert session.commits == 1
