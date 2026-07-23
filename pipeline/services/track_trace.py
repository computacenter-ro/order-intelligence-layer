"""cc-track-trace emitter block (CLAUDE.md [1]) — success terminal.

The ``register`` block registers the order for tracking. Its message
("Registered order ... for tracking") is the SUCCESS terminal that the
backend's journey assembler matches on — treat the text as an API.
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
    return True  # terminal; runner sees chain complete
