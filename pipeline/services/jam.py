"""cc-jam-service emitter block (CLAUDE.md [1]) — user auth + privileges.

JAM sits on **Inbound's pre-creation enrichment leg** (documented: Inbound
calls JAM between the order engine's two turns), so its logs are phase 1 —
eventId only. The JWT the caller mints from the returned privileges is logged
by inbound's ``enrich_jam_resp`` block.

Failure variant (``fail_at=jam``, scenario 9): the account is disabled in JAM.
The satellite emits an auth-failure WARN; INBOUND then logs the 403 response
and aborts the flow (Inbound-identity lines — Inbound is the caller now), and
the chain stops. This is a PRE-CREATION failure: no order ids ever exist, so
the abort message is worded around the event, not an order number. The
``"not authorized"`` / ``"403 from JAM"`` / ``"submission aborted"`` markers
are load-bearing (backend/journeys.py classifies AUTH_FAILED on them) — change
them only together with the failure rules and their tests.
"""
from __future__ import annotations

import random

from pipeline.services.blocklib import emit_line, phase1_ids
from pipeline.services.inbound import CLIENT_LOGGER, ENRICH_THREAD
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("jam")
_CALLER_PROF = profile("inbound")
_LOG = "c.c.jam.service.UserProfileService"
_CALLER_CLIENT = CLIENT_LOGGER["jam"]
_CALLER_ORCHESTRATION = "c.c.inbound.service.OrderOrchestrationService"


@register("jam", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase1_ids(ctx)

    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=f"Authenticating user {ctx.user}", ids=ids)

    if ctx.fail_at == "jam":
        return await _auth_failed(baton, emit)

    privileges = random.randint(10, 14)
    await emit_line(emit, _PROF, logger=_LOG, level="INFO",
                    message=f"User {ctx.user} granted {privileges} privilege(s)", ids=ids)
    return True


async def _auth_failed(baton: Baton, emit: EmitFn) -> bool:
    """403 account-disabled: JAM WARN + Inbound 403 response + Inbound abort."""
    ctx = baton.ctx
    ids = phase1_ids(ctx)
    await emit_line(emit, _PROF, logger=_LOG, level="WARN",
                    message=f"Authentication failed for user {ctx.user}: account disabled in JAM",
                    ids=ids)
    await emit_line(emit, _CALLER_PROF, logger=_CALLER_CLIENT, level="ERROR",
                    thread=ENRICH_THREAD,
                    message=(
                        f"[JamClient#getUserProfileWithPrivilegesBySamAccountName] "
                        f"<--- HTTP/1.1 403 ({random.randint(200, 300)}ms)"
                    ), ids=ids)
    await emit_line(emit, _CALLER_PROF, logger=_CALLER_ORCHESTRATION, level="ERROR",
                    thread=ENRICH_THREAD,
                    message=(
                        f"Cannot process order request for event {ctx.eventId}: user "
                        f"{ctx.user} not authorized (403 from JAM); submission aborted"
                    ), ids=ids)
    return False  # fatal, pre-creation — eventId-only journey
