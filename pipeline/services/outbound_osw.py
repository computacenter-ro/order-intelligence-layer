"""cc-outbound-osw emitter block (CLAUDE.md [1]) — SAP submission.

Failure variant (``fail_at=sap``, scenario 10): the SAP RFC partner is
unreachable → RFC_COMMUNICATION_FAILURE ×3 → the message is moved to the
outbound DLQ. The chain stops.
"""
from __future__ import annotations

import random

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("outbound_osw")
_LOG_SUBMISSION = "c.c.outbound.service.SubmissionService"
_LOG_MAPPER = "c.c.outbound.mapper.SapMapper"
_LOG_RFC = "c.c.outbound.client.SapRfcClient"

_CURRENCY = {"UK": "GBP", "DE": "EUR", "US": "USD"}


@register("outbound_osw", "submit")
async def submit(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    n = len(ctx.lines)
    currency = _CURRENCY.get(ctx.country, "GBP")
    total = round(random.uniform(15000, 50000), 2)

    await emit_line(emit, _PROF, logger=_LOG_SUBMISSION, level="INFO",
                    message=f"Submitting order {ctx.orderId} to SAP ({n} lines, total: {total:.2f} {currency})",
                    ids=ids)
    await emit_line(emit, _PROF, logger=_LOG_MAPPER, level="DEBUG",
                    message=f"Mapped order {ctx.orderId} to SAP document type ZOR", ids=ids)

    if ctx.fail_at == "sap":
        return await _sap_failed(baton, emit)

    sap_ref = random.randint(5000000, 5999999)
    await emit_line(emit, _PROF, logger=_LOG_SUBMISSION, level="INFO",
                    message=f"Order {ctx.orderId} submitted to SAP successfully, SAP order number: {sap_ref}",
                    ids=ids)
    # Stash the SAP ref for track_trace via ctx? ctx has no field; track_trace
    # mints its own ref — realistic enough for the mock.
    return True


async def _sap_failed(baton: Baton, emit: EmitFn) -> bool:
    """RFC communication failure ×3 → move to the outbound DLQ."""
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    for attempt in (1, 2, 3):
        await emit_line(emit, _PROF, logger=_LOG_RFC, level="ERROR",
                        message=(
                            "[SapRfcClient#submitOrder] RFC_COMMUNICATION_FAILURE: "
                            "partner 'sapecc-prod.computacenter.com:3300' not reached"
                        ), ids=ids)
        if attempt < 3:
            await emit_line(emit, _PROF, logger=_LOG_SUBMISSION, level="WARN",
                            message=f"Retrying SAP submission for order {ctx.orderId} (attempt {attempt + 1}/3)",
                            ids=ids)
    await emit_line(emit, _PROF, logger=_LOG_SUBMISSION, level="ERROR",
                    message=(
                        f"Order {ctx.orderId} submission failed after 3 attempt(s); "
                        f"message moved to order.outbound.dlq for manual intervention"
                    ), ids=ids)
    return False  # fatal
