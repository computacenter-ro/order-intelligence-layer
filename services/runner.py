"""Shared baton-consuming loop for the mock services ([1] in CLAUDE.md).

Each mock service is a standalone script that only emits *its own* logs and then
forwards a **baton** — a control message that tells the next service "your turn
to emit", carrying the flow context. This module implements that
consume → emit → forward loop **once**; every service module simply registers
its log blocks in the ``BLOCKS`` registry via the ``@register`` decorator.

Transport is RabbitMQ control queues, one per service: ``sim.step.<service>``.
A service consumes a baton, emits the log block for ``steps[cursor]``, advances
the cursor, and publishes the baton to the next step's queue.

No service block is implemented here — only the loop and the dispatch.

Run one service::

    python -m services.runner inbound
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys

import aio_pika
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractChannel

from shared.log_client import LogClient
from shared.models import Baton

RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

# The registry lives in ``services.registry`` (a single module object) so that
# blocks register into the same ``BLOCKS`` the dispatch loop reads, regardless of
# the ``python -m`` double-import of this module. ``register`` is re-exported so
# existing ``from services.runner import register`` imports keep working.
from services.registry import BLOCKS, Block, EmitFn, register  # noqa: E402,F401


def _queue_name(service: str) -> str:
    return f"sim.step.{service}"


async def _publish(channel: AbstractChannel, service: str, baton: Baton) -> None:
    """Publish the baton (JSON) to a service's control queue via the default exchange."""
    queue_name = _queue_name(service)
    # Declaring the target queue is idempotent and keeps the default-exchange
    # publish from silently dropping the message when nobody has declared it yet.
    await channel.declare_queue(queue_name, durable=True)
    await channel.default_exchange.publish(
        Message(
            body=baton.model_dump_json().encode(),
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
        ),
        routing_key=queue_name,
    )


async def _handle(
    baton: Baton, service_name: str, channel: AbstractChannel, emit: EmitFn
) -> None:
    """Emit the current step's block, then forward the baton (unless fatal failure)."""
    service, block = baton.steps[baton.cursor]

    if service != service_name:
        # Should never happen: we always publish to steps[cursor][0]. A mismatch
        # means a routing bug upstream — log loudly and drop (don't requeue).
        print(
            f"[{service_name}] ERROR: baton flow={baton.flow_id} at cursor "
            f"{baton.cursor} targets '{service}', not this service - dropping",
            flush=True,
        )
        return

    handler = BLOCKS.get((service, block))
    if handler is None:
        print(
            f"[{service_name}] no block registered for ({service}, {block}) - "
            f"cannot emit, chain stops for flow={baton.flow_id}",
            flush=True,
        )
        return

    last = len(baton.steps) - 1
    print(
        f"[{service_name}] flow={baton.flow_id} scenario={baton.scenario} "
        f"step {baton.cursor}/{last} -> running block '{block}'",
        flush=True,
    )

    forward = await handler(baton, emit)
    if not forward:
        print(
            f"[{service_name}] flow={baton.flow_id} block '{block}' signalled "
            f"fatal failure - not forwarding baton",
            flush=True,
        )
        return

    baton.cursor += 1
    if baton.cursor >= len(baton.steps):
        print(
            f"[{service_name}] flow={baton.flow_id} chain complete after "
            f"block '{block}'",
            flush=True,
        )
        return

    next_service = baton.steps[baton.cursor][0]
    await _publish(channel, next_service, baton)
    print(
        f"[{service_name}] flow={baton.flow_id} forwarded baton -> "
        f"{_queue_name(next_service)} (cursor {baton.cursor})",
        flush=True,
    )


def _load_blocks(service_name: str) -> None:
    """Import ``services.<service_name>`` so its ``@register`` blocks populate ``BLOCKS``.

    Registration is an import side-effect: unless the service module is
    imported, its ``@register(...)`` decorators never run and ``BLOCKS`` stays
    empty for that service. The service stem (``inbound``, ``order_engine``, ...)
    is also the module name, so this resolves generically.
    """
    module = f"services.{service_name}"
    try:
        importlib.import_module(module)
    except ModuleNotFoundError as exc:
        # A missing module means this service has no emitter yet — fail loudly
        # rather than silently listen on a queue whose blocks can never fire.
        raise SystemExit(
            f"[{service_name}] no emitter module {module!r} - cannot register "
            f"any blocks ({exc})"
        ) from exc
    registered = sorted(block for svc, block in BLOCKS if svc == service_name)
    print(
        f"[{service_name}] loaded {module}: blocks {registered or '(none registered!)'}",
        flush=True,
    )


async def run_service(service_name: str) -> None:
    """Declare and consume ``sim.step.<service_name>``, dispatching each baton."""
    _load_blocks(service_name)
    queue_name = _queue_name(service_name)
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    async with connection, LogClient() as log_client:
        # The runner owns one LogClient for the whole service lifetime; every
        # block emits through log_client.emit (connection reuse across lines).
        emit = log_client.emit
        channel = await connection.channel()
        # One baton at a time keeps per-service log emission strictly sequential.
        await channel.set_qos(prefetch_count=1)
        queue = await channel.declare_queue(queue_name, durable=True)
        print(
            f"[{service_name}] listening on {queue_name} (RABBITMQ_URL={RABBITMQ_URL})",
            flush=True,
        )
        async with queue.iterator() as messages:
            async for message in messages:
                # requeue=False: a poison message is dropped, never looped forever.
                async with message.process(requeue=False):
                    try:
                        baton = Baton.model_validate_json(message.body)
                    except Exception as exc:  # noqa: BLE001 — log and drop bad messages
                        print(
                            f"[{service_name}] ERROR: undecodable baton dropped: {exc}",
                            flush=True,
                        )
                        continue
                    try:
                        await _handle(baton, service_name, channel, emit)
                    except Exception as exc:  # noqa: BLE001 — one bad flow must not kill the loop
                        print(
                            f"[{service_name}] ERROR handling baton "
                            f"flow={baton.flow_id}: {exc}",
                            flush=True,
                        )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m services.runner <service_name>", file=sys.stderr)
        return 2
    asyncio.run(run_service(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
