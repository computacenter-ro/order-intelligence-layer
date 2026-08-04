"""cc-track-trace emitter block (CLAUDE.md [1]) — order tracking registration.

The ``register`` block registers the order for tracking. It runs MID-FLOW,
immediately after creation and BEFORE the checks (the documented step-11
position) — it is **no longer the success terminal**, and the backend's
journey assembler must NOT treat "Registered order ... for tracking" as one
(a journey can carry this line and still fail at SPT, the validator, the
checker or SAP). The SUCCESS terminal is Inbound's order_created close
(``pipeline/services/inbound.py``).
"""
from __future__ import annotations

import random

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("track_trace")
_LOG = "c.c.tracktrace.service.TrackingService"


@register("track_trace", "register")
async def register_order(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    sap_ref = random.randint(5000000, 5999999)
    await emit_line(
        emit, _PROF, logger=_LOG, level="INFO",
        message=f"Registered order {ctx.orderId} for tracking, SAP ref: {sap_ref}",
        ids=phase2_ids(ctx),
    )
    return True  # forward to the order engine's enrichment leg
