"""[3]/[4] AI Service — RabbitMQ output publisher (CLAUDE.md [4]).

Publishes the AI service's two output streams to their durable queues:

* ``raw.events``       — EVERY deduped log (any level), the backend's journey
                         assembly material.
* ``processed.alerts`` — one ``ProcessedAlert`` per non-suppressed WARN/ERROR
                         (AI-explained or fallback pass-through).

Both queues are durable and messages are persistent: delivery is at-least-once,
and the backend consumers are the idempotent side (dedup on ``log_id`` /
``alert_id``). Payloads are serialized **through the Pydantic models**, never
hand-built dicts, so the wire format (canonical timestamp, exact field set)
matches what the backend expects.

The aio-pika usage mirrors ``pipeline/services/runner.py`` (connect_robust, idempotent
durable ``declare_queue``, ``default_exchange.publish`` with PERSISTENT delivery)
so the whole project speaks RabbitMQ the same way. The channel is injectable so
unit tests exercise routing/serialization with a fake — no broker required.
"""
from __future__ import annotations

import aio_pika
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractChannel, AbstractConnection

from ai_service import settings
from shared.models import LogLine, ProcessedAlert


class Publisher:
    """Async publisher for ``raw.events`` and ``processed.alerts``.

    Holds one connection + channel for reuse across many publishes. Use as an
    async context manager, or call :meth:`connect` / :meth:`close` explicitly.
    Inject a pre-made ``channel`` (e.g. a fake in tests) to skip the broker.
    """

    def __init__(
        self,
        *,
        url: str = settings.RABBITMQ_URL,
        raw_queue: str = settings.RAW_EVENTS_QUEUE,
        alerts_queue: str = settings.PROCESSED_ALERTS_QUEUE,
        channel: AbstractChannel | None = None,
    ) -> None:
        self._url = url
        self._raw_queue = raw_queue
        self._alerts_queue = alerts_queue
        self._channel = channel
        self._connection: AbstractConnection | None = None

    # --- lifecycle ------------------------------------------------------------
    async def connect(self) -> "Publisher":
        """Open the connection/channel (unless one was injected) and declare
        both queues durably. Idempotent declaration matches runner.py and keeps
        a default-exchange publish from silently dropping messages."""
        if self._channel is None:
            self._connection = await aio_pika.connect_robust(self._url)
            self._channel = await self._connection.channel()
        await self._channel.declare_queue(self._raw_queue, durable=True)
        await self._channel.declare_queue(self._alerts_queue, durable=True)
        return self

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def __aenter__(self) -> "Publisher":
        return await self.connect()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # --- publish --------------------------------------------------------------
    async def publish_raw(self, log: LogLine) -> None:
        """Publish one raw log to ``raw.events`` (every deduped log lands here)."""
        await self._publish(self._raw_queue, log.model_dump_json())

    async def publish_alert(self, alert: ProcessedAlert) -> None:
        """Publish one ProcessedAlert to ``processed.alerts``."""
        await self._publish(self._alerts_queue, alert.model_dump_json())

    async def _publish(self, queue: str, body_json: str) -> None:
        if self._channel is None:  # pragma: no cover - guards misuse
            raise RuntimeError("Publisher.connect() must be called before publishing")
        await self._channel.default_exchange.publish(
            Message(
                body=body_json.encode(),
                delivery_mode=DeliveryMode.PERSISTENT,
                content_type="application/json",
            ),
            routing_key=queue,
        )
