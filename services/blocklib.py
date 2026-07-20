"""Small helpers shared by the mock service emitter blocks.

A block's job is narrow: build a few ``LogLine``s from the baton ctx, emit them
through the runner-provided ``emit`` callable, and sleep a little between lines
so timestamps interleave realistically across concurrent flows (CLAUDE.md [1]
"Timing"). This module holds the two utilities every block reuses so the
emitter modules stay declarative.
"""
from __future__ import annotations

import asyncio
import os
import random

from services.profiles import ServiceProfile, make_log
from services.registry import EmitFn
from shared.models import BatonContext, Level, LogLine

# Inter-line delay bounds (seconds) — CLAUDE.md: "sleeps 10-110 ms between lines".
_MIN_DELAY = 0.010
_MAX_DELAY = 0.110

# Tests (and any fast, non-real-time driver) set MOCK_EMIT_NO_SLEEP=1 to skip the
# inter-line sleep — the realistic timing matters only for live runs where
# timestamps must interleave across concurrent flows.
_NO_SLEEP = os.getenv("MOCK_EMIT_NO_SLEEP") == "1"


async def _tick() -> None:
    """Sleep a realistic 10-110 ms between log lines (skipped when _NO_SLEEP)."""
    if _NO_SLEEP:
        return
    await asyncio.sleep(random.uniform(_MIN_DELAY, _MAX_DELAY))


def phase1_ids(ctx: BatonContext) -> dict[str, str | None]:
    """Id kwargs for a phase-1 log: eventId + accountNumber only.

    Pre-creation logs physically cannot carry order ids — they do not exist yet
    (CLAUDE.md correlation model, Invariant #1). This helper makes that the
    path of least resistance for phase-1 blocks.
    """
    return {"eventId": ctx.eventId, "accountNumber": ctx.accountNumber}


def phase2_ids(ctx: BatonContext) -> dict[str, str | None]:
    """Id kwargs for a phase-2 log: both order ids + accountNumber, never eventId.

    After creation the eventId disappears and every log carries both the
    orderId and the cartHeaderId (CLAUDE.md correlation model).
    """
    return {
        "orderId": ctx.orderId,
        "cartHeaderId": ctx.cartHeaderId,
        "accountNumber": ctx.accountNumber,
    }


async def emit_line(
    emit: EmitFn,
    prof: ServiceProfile,
    *,
    logger: str,
    level: Level,
    message: str,
    thread: str | None = None,
    ids: dict[str, str | None],
) -> None:
    """Build one LogLine (stamped with ``prof``) and emit it, then tick.

    ``ids`` is one of :func:`phase1_ids` / :func:`phase2_ids` (or a hand-built
    dict for the bridge line). Emitting one line at a time — rather than
    batching — is what produces realistic, interleaved per-line timestamps.
    """
    line: LogLine = make_log(
        prof,
        logger=logger,
        level=level,
        message=message,
        thread=thread,
        **ids,
    )
    await emit(line)
    await _tick()
