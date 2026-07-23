"""cc-validator-service emitter block (CLAUDE.md [1]) — validation strategies.

The validator runs a sequence of strategy validators. Several are benign
"Not implemented" WARNs (which the AI service must suppress, not alert on). For
US flows it also emits the **Avalara** ship-to verification (there is no
standalone cc-avalara-service — the validator's AvalaraClient does it here).

Failure variant (``fail_at=udf``, scenario 7): a mandatory line UDF
(``costCenter``) is missing → the UDF strategy errors, the access log records a
422, and the order engine aborts submission (OE-identity). The chain stops.
"""
from __future__ import annotations

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("validator")
_OE_PROF = profile("order_engine")

_LOG_INTERNAL = "c.c.validator.strategy.ValidateInternalContract"
_LOG_UDF = "c.c.validator.strategy.ValidateOrderLineUdfFields"
_LOG_AVALARA_STRATEGY = "c.c.validator.strategy.ValidateShipToWithAvalara"
_LOG_AVALARA_CLIENT = "c.c.validator.client.AvalaraClient"
_LOG_TEXT_TOTAL = "c.c.validator.strategy.ValidateOrderTextTotalLines"
_LOG_BOM_REBATES = "c.c.validator.strategy.ValidateGenericEnterpriseBomRebates"
_LOG_ACCESS = "c.c.validator.http.AccessLog"
_OE_PROCESSING = "c.c.orderengine.service.OrderProcessingService"

# US ship-to addresses used for the Avalara verification line.
_US_SHIPTO = "1401 Elm St, Dallas, TX 75202"


def _oe_thread(baton: Baton) -> str:
    return ORDER_ENGINE_WORKER_THREADS[abs(hash(baton.flow_id)) % len(ORDER_ENGINE_WORKER_THREADS)]


@register("validator", "validate")
async def validate(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase2_ids(ctx)

    await emit_line(emit, _PROF, logger=_LOG_INTERNAL, level="INFO",
                    message="Validating internal contracts for order lines.", ids=ids)
    await emit_line(emit, _PROF, logger=_LOG_UDF, level="INFO",
                    message="Validating line UDF fields", ids=ids)

    if ctx.fail_at == "udf":
        return await _udf_failed(baton, emit)

    # Avalara ship-to verification (US only) OR the benign "Not implemented" WARN.
    if ctx.country == "US":
        await emit_line(emit, _PROF, logger=_LOG_AVALARA_CLIENT, level="DEBUG",
                        message="[AvalaraClient#resolveAddress] ---> POST https://rest.avatax.com/api/v2/addresses/resolve HTTP/1.1",
                        ids=ids)
        await emit_line(emit, _PROF, logger=_LOG_AVALARA_STRATEGY, level="INFO",
                        message=f"Ship-to address verified: {_US_SHIPTO}, resolution quality: Premises",
                        ids=ids)
    else:
        await emit_line(emit, _PROF, logger=_LOG_AVALARA_STRATEGY, level="WARN",
                        message="Not implemented", ids=ids)

    # Two more benign "Not implemented" strategy WARNs (suppressed downstream).
    await emit_line(emit, _PROF, logger=_LOG_TEXT_TOTAL, level="WARN",
                    message="Not implemented", ids=ids)
    await emit_line(emit, _PROF, logger=_LOG_BOM_REBATES, level="WARN",
                    message="Not implemented", ids=ids)
    await emit_line(emit, _PROF, logger=_LOG_ACCESS, level="INFO",
                    message="POST /api/strategy/create-order-press-save-submit/run 200", ids=ids)
    return True


async def _udf_failed(baton: Baton, emit: EmitFn) -> bool:
    """Missing mandatory costCenter UDF → 422 → OE aborts submission."""
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    line_no = len(ctx.lines)  # blame the last line
    await emit_line(emit, _PROF, logger=_LOG_UDF, level="ERROR",
                    message=f"Validation failed: mandatory UDF 'costCenter' missing on line {line_no}",
                    ids=ids)
    await emit_line(emit, _PROF, logger=_LOG_ACCESS, level="INFO",
                    message="POST /api/strategy/create-order-press-save-submit/run 422", ids=ids)
    await emit_line(emit, _OE_PROF, logger=_OE_PROCESSING, level="ERROR",
                    thread=_oe_thread(baton),
                    message=f"Order {ctx.orderId} validation failed with 1 error(s); submission aborted",
                    ids=ids)
    return False  # fatal
