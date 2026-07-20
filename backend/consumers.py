"""[5] Core Backend — RabbitMQ input consumers (CLAUDE.md [4] → [5]).

Two idempotent consumers for the AI service's durable output queues:

* ``processed.alerts`` — one ``ProcessedAlert`` per non-suppressed WARN/ERROR
  (AI-explained or a fallback pass-through). Each is persisted as an ``Alert``
  row (the original ``LogLine`` fields + ``explanation`` / ``department`` /
  ``confidence`` / ``source``; the enrichment columns are null for
  ``source="fallback"``).
* ``raw.events`` — every deduped log line. Each is handed to the
  :class:`~backend.journeys.JourneyAssembler` for incremental journey assembly.

Delivery on both queues is **at-least-once**, so the consumers are the
idempotent side (CLAUDE.md "[4]"): re-delivering the same ``alert_id`` /
``log_id`` must change nothing. Idempotency is enforced by the DB, not by an
application-level "have I seen this?" check —

* alerts: ``INSERT ... ON CONFLICT DO NOTHING`` (the unique ``alert_id`` PK and
  the unique ``log_id`` both back it), and
* raw events: the assembler's own ``ON CONFLICT DO NOTHING`` on
  ``journey_events.log_id``.

The aio-pika usage mirrors ``pipeline/services/runner.py`` and
``ai_service/publisher.py`` (``connect_robust``, idempotent durable
``declare_queue``, ``set_qos`` prefetch, ``message.process``) so the whole
project speaks RabbitMQ the same way. Dependencies (channel, session factory,
assembler) are injectable so the decode/persist logic is unit-testable with
fakes — no broker and no database required.
"""

from __future__ import annotations

import os

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractIncomingMessage

from shared.models import LogLine, ProcessedAlert
from backend.journeys import JourneyAssembler

# --- Config (env-driven, matching ai_service/settings.py conventions) --------

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
RAW_EVENTS_QUEUE = os.getenv("RAW_EVENTS_QUEUE", "raw.events")
PROCESSED_ALERTS_QUEUE = os.getenv("PROCESSED_ALERTS_QUEUE", "processed.alerts")

# How often the background task asks the assembler to finalize stalled journeys
# (CLAUDE.md TIMED_OUT rule). A stalled journey stops receiving raw.events, so
# nothing else would ever trigger its evaluation — this periodic sweep does.
STALLED_SWEEP_INTERVAL = int(os.getenv("STALLED_SWEEP_INTERVAL", "15"))


# --- Pure mapping: ProcessedAlert -> Alert columns ---------------------------


def alert_row_values(alert: ProcessedAlert) -> dict:
    """Flatten a ``ProcessedAlert`` into the ``alerts`` table's columns.

    The full original log line's fields are hoisted onto the row; the AI
    enrichment columns (``explanation`` / ``department`` / ``confidence``) are
    ``None`` for a fallback pass-through. ``journey_id`` is intentionally left
    unset — an alert may arrive before its journey is assembled from
    ``raw.events`` (CLAUDE.md: the FK is nullable and fills in later).
    """
    log = alert.log
    return {
        "alert_id": alert.alert_id,
        "emitted_at": alert.emitted_at,
        "log_id": log.log_id,
        "level": log.level,
        "app_name": log.app_name,
        "logger": log.logger,
        "message": log.message,
        "event_id": log.eventId,
        "order_id": log.orderId,
        "cart_header_id": log.cartHeaderId,
        "account_number": log.accountNumber,
        "explanation": alert.explanation,
        "department": alert.department.value if alert.department is not None else None,
        "confidence": alert.confidence,
        "source": alert.source,
    }


# --- Base consumer -----------------------------------------------------------


class _QueueConsumer:
    """Shared consume loop: durable declare, prefetch, per-message processing.

    Subclasses implement :meth:`_decode` (bytes -> model) and :meth:`_process`
    (model -> persistence). An injected ``channel`` skips the broker for tests.
    """

    def __init__(
        self,
        *,
        queue: str,
        url: str = RABBITMQ_URL,
        prefetch: int = 1,
        channel: AbstractChannel | None = None,
        session_factory=None,
    ) -> None:
        self.queue = queue
        self._url = url
        self._prefetch = prefetch
        self._channel = channel
        self._session_factory = session_factory
        self._connection: AbstractConnection | None = None
        self._queue_obj = None

    # --- lifecycle -----------------------------------------------------------
    async def connect(self) -> "_QueueConsumer":
        """Open the connection/channel (unless injected), set prefetch, and
        declare the queue durably (idempotent — matches the publisher side)."""
        if self._channel is None:
            self._connection = await aio_pika.connect_robust(self._url)
            self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self._prefetch)
        self._queue_obj = await self._channel.declare_queue(self.queue, durable=True)
        return self

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    def _factory(self):
        """Resolve the session factory lazily so importing this module never
        needs the DB driver (mirrors backend/journeys.py)."""
        if self._session_factory is not None:
            return self._session_factory
        from backend.db import SessionLocal

        return SessionLocal

    # --- run loop ------------------------------------------------------------
    async def run(self) -> None:
        """Consume forever, dispatching every message to :meth:`_process`.

        A message that fails to decode is a poison message: it is acked and
        dropped (re-queuing it would loop forever). A message that decodes but
        fails to persist is re-queued (``requeue=True``) — a transient DB blip
        redelivers it, and idempotency makes the retry safe.

        Crucially, an exception raised while handling one message must NOT tear
        down the loop (which would kill this consumer, and via ``gather`` its
        siblings too). We catch it here, log it, and move on to the next message
        — exactly like the try/except in ``pipeline/services/runner.py``. The
        failing message has already been nacked+requeued by ``message.process``
        before the exception reaches us, so requeue semantics are preserved.
        """
        await self.connect()
        print(f"[{self.queue}] listening (RABBITMQ_URL={self._url})", flush=True)
        async with self._queue_obj.iterator() as messages:
            async for message in messages:
                try:
                    await self._on_message(message)
                except Exception as exc:  # noqa: BLE001 — one bad message must not kill the loop
                    print(
                        f"[{self.queue}] ERROR handling message (requeued, "
                        f"continuing): {exc}",
                        flush=True,
                    )

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        async with message.process(requeue=True, ignore_processed=True):
            try:
                payload = self._decode(message.body)
            except Exception as exc:  # noqa: BLE001 — poison message: ack + drop
                print(
                    f"[{self.queue}] dropping undecodable message: {exc}",
                    flush=True,
                )
                return
            # A raise here propagates out of message.process -> nack+requeue.
            await self._process(payload)

    # --- to be implemented by subclasses -------------------------------------
    def _decode(self, body: bytes):  # pragma: no cover - overridden
        raise NotImplementedError

    async def _process(self, payload) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


# --- processed.alerts --------------------------------------------------------


class AlertsConsumer(_QueueConsumer):
    """Consumes ``processed.alerts`` → persists idempotent ``Alert`` rows."""

    def __init__(self, *, queue: str = PROCESSED_ALERTS_QUEUE, **kw) -> None:
        super().__init__(queue=queue, **kw)

    def _decode(self, body: bytes) -> ProcessedAlert:
        return ProcessedAlert.model_validate_json(body)

    async def _process(self, alert: ProcessedAlert) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from backend.db import Alert

        stmt = (
            pg_insert(Alert)
            .values(**alert_row_values(alert))
            .on_conflict_do_nothing()  # dedup on unique alert_id / log_id
        )
        async with self._factory()() as session:
            await session.execute(stmt)
            await session.commit()


# --- raw.events --------------------------------------------------------------


class RawEventsConsumer(_QueueConsumer):
    """Consumes ``raw.events`` → feeds the incremental journey assembler."""

    def __init__(
        self,
        *,
        queue: str = RAW_EVENTS_QUEUE,
        assembler: JourneyAssembler | None = None,
        **kw,
    ) -> None:
        super().__init__(queue=queue, **kw)
        # One long-lived assembler accumulates journey state across all polls
        # (CLAUDE.md: assembly is incremental/"lazy"). prefetch=1 (the base
        # default) keeps journey assembly strictly sequential.
        self._assembler = assembler if assembler is not None else JourneyAssembler()

    def _decode(self, body: bytes) -> LogLine:
        return LogLine.model_validate_json(body)

    async def _process(self, log: LogLine) -> None:
        # The assembler persists events (ON CONFLICT DO NOTHING on log_id) and
        # journeys, and commits — so raw-event consumption is idempotent too.
        async with self._factory()() as session:
            await self._assembler.ingest(session, [log])


# --- run both ----------------------------------------------------------------


async def _sweep_stalled_loop(assembler: JourneyAssembler) -> None:
    """Periodically finalize stalled (TIMED_OUT) journeys.

    A stalled journey stops receiving ``raw.events``, so the raw consumer never
    re-evaluates it — it would sit at IN_PROGRESS forever. This background task
    fills that gap: every ``STALLED_SWEEP_INTERVAL`` seconds it opens a session
    and runs ``assembler.sweep_stalled`` on the **same** assembler instance the
    raw consumer uses, so the in-memory journeys are visible.
    """
    import asyncio

    from backend.db import SessionLocal

    try:
        while True:
            await asyncio.sleep(STALLED_SWEEP_INTERVAL)
            try:
                async with SessionLocal() as session:
                    await assembler.sweep_stalled(session)
            except Exception as exc:  # noqa: BLE001 — a sweep blip must not kill the task
                print(f"[stalled-sweep] ERROR (continuing): {exc}", flush=True)
    except asyncio.CancelledError:
        # Clean shutdown: stop looping, let the cancellation propagate.
        print("[stalled-sweep] cancelled — stopping", flush=True)
        raise


async def run_consumers(assembler: JourneyAssembler | None = None) -> None:
    """Run both consumers + the stalled-journey sweep concurrently.

    Called by ``backend.main``. Each consumer gets its own channel so their
    prefetch windows are independent. The sweep task shares the raw consumer's
    assembler instance so it can time out in-memory journeys.
    """
    import asyncio

    # One shared assembler: the raw consumer builds journeys into it, the sweep
    # task times them out from the same in-memory state.
    assembler = assembler if assembler is not None else JourneyAssembler()

    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection:
        alerts_channel = await connection.channel()
        raw_channel = await connection.channel()
        alerts = AlertsConsumer(channel=alerts_channel)
        raw = RawEventsConsumer(channel=raw_channel, assembler=assembler)
        await asyncio.gather(
            alerts.run(),
            raw.run(),
            _sweep_stalled_loop(assembler),
        )


def main() -> int:  # pragma: no cover - process entrypoint
    import asyncio

    asyncio.run(run_consumers())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
