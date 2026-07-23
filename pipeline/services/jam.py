"""cc-jam-service emitter block (CLAUDE.md [1]) — user auth + privileges + JWT.

Failure variant (``fail_at=jam``, scenario 9): the account is disabled in JAM.
The satellite emits an auth-failure WARN; the order engine then logs the 403
response and aborts (OE-identity lines), and the chain stops.
"""
from __future__ import annotations

import random

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("jam")
_OE_PROF = profile("order_engine")
_LOG = "c.c.jam.service.UserProfileService"
_OE_CLIENT = "c.c.orderengine.client.JamClient"
_OE_PROCESSING = "c.c.orderengine.service.OrderProcessingService"


def _oe_thread(baton: Baton) -> str:
    return ORDER_ENGINE_WORKER_THREADS[abs(hash(baton.flow_id)) % len(ORDER_ENGINE_WORKER_THREADS)]


@register("jam", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase2_ids(ctx)

    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=f"Authenticating user {ctx.user}", ids=ids)

    if ctx.fail_at == "jam":
        return await _auth_failed(baton, emit)

    privileges = random.randint(10, 14)
    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=f"User {ctx.user} granted {privileges} privilege(s)", ids=ids)
    return True


async def _auth_failed(baton: Baton, emit: EmitFn) -> bool:
    """403 account-disabled: JAM WARN + OE 403 response + OE abort."""
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    await emit_line(emit, _PROF, logger=_LOG, level="WARN",
                    message=f"Authentication failed for user {ctx.user}: account disabled in JAM",
                    ids=ids)
    thread = _oe_thread(baton)
    await emit_line(emit, _OE_PROF, logger=_OE_CLIENT, level="ERROR", thread=thread,
                    message=(
                        f"[JamClient#getUserProfileWithPrivilegesBySamAccountName] "
                        f"<--- HTTP/1.1 403 ({random.randint(200, 300)}ms)"
                    ), ids=ids)
    await emit_line(emit, _OE_PROF, logger=_OE_PROCESSING, level="ERROR", thread=thread,
                    message=(
                        f"Cannot process order {ctx.orderId}: user {ctx.user} not "
                        f"authorized (403 from JAM); submission aborted"
                    ), ids=ids)
    return False  # fatal
