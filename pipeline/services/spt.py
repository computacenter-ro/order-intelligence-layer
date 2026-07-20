"""cc-spt-service emitter block (CLAUDE.md [1]) — price-list lookup.

The ``serve`` block is the satellite's server-side log during enrichment.
Failure variant (``fail_at=spt``, scenario 8): the SPT web service is
unreachable. In the reference dataset this failure is expressed entirely from
the *order engine* side (SptClient socket timeouts ×3 → OrderProcessingService
abort) — the satellite emits nothing itself. So this block, on failure, emits
those order-engine-identity lines and stops the chain.
"""
from __future__ import annotations

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("spt")
_OE_PROF = profile("order_engine")
_LOG = "c.c.spt.service.PriceListService"
_OE_CLIENT = "c.c.orderengine.client.SptClient"
_OE_PROCESSING = "c.c.orderengine.service.OrderProcessingService"


def _oe_thread(baton: Baton) -> str:
    return ORDER_ENGINE_WORKER_THREADS[abs(hash(baton.flow_id)) % len(ORDER_ENGINE_WORKER_THREADS)]


@register("spt", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase2_ids(ctx)

    if ctx.fail_at == "spt":
        return await _spt_down(baton, emit)

    # Happy path: server-side price-list resolution.
    price_list = f"PL-{ctx.country}-TIER2"
    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=f"Fetching price list for account {ctx.accountNumber}", ids=ids)
    await emit_line(emit, _PROF, logger=_LOG, level="DEBUG",
                    message=f"Resolved price list code {price_list} for account {ctx.accountNumber}", ids=ids)
    return True


async def _spt_down(baton: Baton, emit: EmitFn) -> bool:
    """SPT unreachable: OE-side socket timeouts ×3 → processing aborted."""
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    thread = _oe_thread(baton)
    for attempt in (1, 2, 3):
        await emit_line(emit, _OE_PROF, logger=_OE_CLIENT, level="ERROR", thread=thread,
                        message=(
                            "[SptClient#getSptPriceListCode] <--- ERROR "
                            "java.net.SocketTimeoutException: connect timed out (10014ms)"
                        ), ids=ids)
        if attempt < 3:
            await emit_line(emit, _OE_PROF, logger=_OE_CLIENT, level="WARN", thread=thread,
                            message=(
                                f"Retrying SPT price list call for account "
                                f"{ctx.accountNumber} (attempt {attempt + 1}/3)"
                            ), ids=ids)
    await emit_line(emit, _OE_PROF, logger=_OE_PROCESSING, level="ERROR", thread=thread,
                    message=(
                        f"Order processing aborted for order {ctx.orderId}: SPT price "
                        f"list service unavailable after 3 attempt(s)"
                    ), ids=ids)
    return False  # fatal
