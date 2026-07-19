"""cc-rsm-service emitter block (CLAUDE.md [1]) — rebates / PVC rates."""
from __future__ import annotations

from services.blocklib import emit_line, phase2_ids
from services.profiles import profile
from services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("rsm")
_LOG = "c.c.rsm.service.RebateSchemeService"


@register("rsm", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    n = len(ctx.lines)
    await emit_line(
        emit, _PROF, logger=_LOG, level="INFO",
        message=(
            f"Calculating rebates for account {ctx.accountNumber}, "
            f"{n} product(s), country {ctx.country}"
        ),
        ids=phase2_ids(ctx),
    )
    return True
