"""cc-inbound-service emitter blocks (CLAUDE.md [1]).

Blocks:
  * ``receive`` — phase 1: receive the inbound event, transform + SKU-map each
    line, publish to order.inbound.queue. Failure variant (``fail_at=transform``,
    scenario 4): an unknown product has no SKU mapping → 3 delivery attempts →
    route to the DLQ, and the chain stops.
  * ``bridge``  — the one log where eventId coexists with the new order id(s),
    per ``ctx.bridge_ids``. Also emits the failure-response variant when the
    order was never created (scenario 5).

Id discipline: ``receive`` is pure phase 1 (eventId only). ``bridge`` is the
hinge — it logs eventId AND whichever order id(s) ctx.bridge_ids selected.
"""
from __future__ import annotations

import random

from services.blocklib import emit_line, phase1_ids
from services.profiles import profile
from services.registry import EmitFn, register
from shared.models import Baton, BatonContext

_PROF = profile("inbound")

# Loggers (verbatim from the reference dataset).
_LOG_ORDER_LISTENER = "c.c.inbound.listener.OrderListener"
_LOG_TRANSFORM = "c.c.inbound.transform.TransformService"
_LOG_PUBLISHER = "c.c.inbound.publisher.RabbitPublisher"
_LOG_RESPONSE_LISTENER = "c.c.inbound.listener.ResponseListener"

_RECEIVE_THREAD = "rabbit-listener-1"
_RESPONSE_THREAD = "rabbit-listener-2"  # the response listener runs on a 2nd listener


@register("inbound", "receive")
async def receive(baton: Baton, emit: EmitFn) -> bool:
    """Phase 1: receive → transform + SKU-map → publish (or fail in transform)."""
    ctx = baton.ctx
    ids = phase1_ids(ctx)
    n = len(ctx.lines)

    await emit_line(
        emit, _PROF, logger=_LOG_ORDER_LISTENER, level="INFO", thread=_RECEIVE_THREAD,
        message=f"Received inbound order event {ctx.eventId} for account {ctx.accountNumber}",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_TRANSFORM, level="INFO", thread=_RECEIVE_THREAD,
        message=f"Transforming inbound payload for event {ctx.eventId}, {n} line(s)",
        ids=ids,
    )

    if ctx.fail_at == "transform":
        return await _transform_failure(emit, ctx)

    # Happy transform: one DEBUG "Mapped product X to internal SKU Y" per line.
    for line in ctx.lines:
        await emit_line(
            emit, _PROF, logger=_LOG_TRANSFORM, level="DEBUG", thread=_RECEIVE_THREAD,
            message=f"Mapped product {line.productId} to internal SKU {line.sku}",
            ids=ids,
        )
    await emit_line(
        emit, _PROF, logger=_LOG_PUBLISHER, level="INFO", thread=_RECEIVE_THREAD,
        message=f"Published event {ctx.eventId} to queue order.inbound.queue",
        ids=ids,
    )
    return True  # forward the baton to order_engine/create


async def _transform_failure(emit: EmitFn, ctx: BatonContext) -> bool:
    """Unknown-product transform failure: 3 attempts, then route to the DLQ.

    The first line with no SKU triggers the error; RabbitMQ redelivers up to
    3 times (attempts 2/3 and 3/3 are logged), then the message is dead-
    lettered. The baton is NOT forwarded (return False) — a fatal failure.
    """
    ids = phase1_ids(ctx)
    # The offending product is the first line without a resolvable SKU.
    bad = next((line.productId for line in ctx.lines if line.sku is None), ctx.lines[0].productId)

    for attempt in (1, 2, 3):
        await emit_line(
            emit, _PROF, logger=_LOG_TRANSFORM, level="ERROR", thread=_RECEIVE_THREAD,
            message=f"No internal SKU mapping found for product {bad}",
            ids=ids,
        )
        if attempt < 3:
            await emit_line(
                emit, _PROF, logger=_LOG_ORDER_LISTENER, level="WARN", thread=_RECEIVE_THREAD,
                message=(
                    f"Requeueing event {ctx.eventId} for redelivery "
                    f"(attempt {attempt + 1}/3)"
                ),
                ids=ids,
            )
    await emit_line(
        emit, _PROF, logger=_LOG_ORDER_LISTENER, level="ERROR", thread=_RECEIVE_THREAD,
        message=(
            f"Max redelivery attempts reached for event {ctx.eventId}; "
            f"routing message to order.inbound.dlq"
        ),
        ids=ids,
    )
    return False  # fatal — never created; eventId-only journey


@register("inbound", "bridge")
async def bridge(baton: Baton, emit: EmitFn) -> bool:
    """The bridge log: eventId + the order id(s) selected by ctx.bridge_ids.

    This is the only line where eventId coexists with the order ids. Which ids
    appear is driven by ctx.bridge_ids: ``both`` / ``order`` / ``cart`` /
    ``random`` (resolved here). The message text mirrors the real system's
    ResponseListener line and includes exactly the ids that are present.
    """
    ctx = baton.ctx
    expose = _resolve_bridge_ids(ctx.bridge_ids)

    order_id = ctx.orderId if expose in ("both", "order") else None
    cart_id = ctx.cartHeaderId if expose in ("both", "cart") else None

    # Message lists only the ids actually exposed on this line (fixture-faithful).
    parts = []
    if order_id is not None:
        parts.append(f"orderNumber={order_id}")
    if cart_id is not None:
        parts.append(f"cartHeaderId={cart_id}")
    detail = ", ".join(parts)

    await emit_line(
        emit, _PROF, logger=_LOG_RESPONSE_LISTENER, level="INFO", thread=_RESPONSE_THREAD,
        message=f"Received order creation response for event {ctx.eventId}: {detail}",
        ids={
            "eventId": ctx.eventId,
            "orderId": order_id,
            "cartHeaderId": cart_id,
            "accountNumber": ctx.accountNumber,
        },
    )
    return True


def _resolve_bridge_ids(bridge_ids: str) -> str:
    """Resolve ``random`` to a concrete both/order/cart choice."""
    if bridge_ids == "random":
        return random.choice(("both", "order", "cart"))
    return bridge_ids
