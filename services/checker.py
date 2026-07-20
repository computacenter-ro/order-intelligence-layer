"""cc-checker-service emitter block (CLAUDE.md [1]) — per-line margin check.

Failure variant (``fail_at=margin``, scenario 6): overall margin below the
threshold → the order is blocked. The checker emits the FAILED + blocked lines,
the order engine logs that submission is halted (OE-identity), and the chain
stops.
"""
from __future__ import annotations

import random

from services.blocklib import emit_line, phase2_ids
from services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("checker")
_OE_PROF = profile("order_engine")
_LOG = "c.c.checker.service.MarginCheckService"
_OE_PROCESSING = "c.c.orderengine.service.OrderProcessingService"

_THRESHOLD = "15.00"


def _oe_thread(baton: Baton) -> str:
    return ORDER_ENGINE_WORKER_THREADS[abs(hash(baton.flow_id)) % len(ORDER_ENGINE_WORKER_THREADS)]


@register("checker", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    failing = ctx.fail_at == "margin"

    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=f"Running margin check for order {ctx.orderId}, account {ctx.accountNumber}",
                    ids=ids)

    # Per-line margins: low band when failing, healthy band otherwise.
    margins: list[float] = []
    for i, _line in enumerate(ctx.lines, start=1):
        margin = round(random.uniform(2.0, 12.0) if failing else random.uniform(18.0, 55.0), 2)
        margins.append(margin)
        cost = round(random.uniform(700, 8000), 2)
        sell = round(cost / (1 - margin / 100), 2)
        await emit_line(emit, _PROF, logger=_LOG, level="DEBUG",
                        message=(
                            f"Line {i}: cost={cost:.2f}, sell={sell:.2f}, "
                            f"margin={margin:.2f}%, threshold={_THRESHOLD}%"
                        ), ids=ids)

    overall = round(sum(margins) / len(margins), 2)

    if failing:
        await emit_line(emit, _PROF, logger=_LOG, level="ERROR",
                        message=(
                            f"Margin check FAILED for order {ctx.orderId}: overall margin "
                            f"{overall:.2f}% below threshold {_THRESHOLD}%"
                        ), ids=ids)
        await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                        message=f"Order {ctx.orderId} blocked pending commercial approval", ids=ids)
        await emit_line(emit, _OE_PROF, logger=_OE_PROCESSING, level="WARN",
                        thread=_oe_thread(baton),
                        message=f"Order {ctx.orderId} blocked by margin check; submission halted",
                        ids=ids)
        return False  # fatal

    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=(
                        f"Margin check passed for order {ctx.orderId}: overall margin "
                        f"{overall:.2f}% exceeds threshold {_THRESHOLD}%"
                    ), ids=ids)
    return True
