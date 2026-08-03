"""Backfill the AI service's retrieval index from Postgres.

    python -m backend.scripts.backfill_rag

The live hooks (``backend/consumers.py``, ``backend/journeys.py``) only index
records created from now on, so an existing database — and any window where the
AI service was down — leaves gaps. This walks every alert and every COMPLETED
journey with a summary and pushes each to ``POST /index``.

**Idempotent.** The index upserts by record id, so re-running re-embeds the same
ids in place rather than duplicating them. Safe to run repeatedly; the honest
recovery for "is the index stale?" is simply to run it again.

Reads through the same ``SessionLocal`` as the rest of the backend, and pushes
through the same ``rag_client`` as the live hooks — so the text and metadata a
backfilled record gets are byte-identical to what the live path would have
produced. One ``httpx`` client is reused across the whole run (connection reuse:
a few thousand short-lived clients would be needlessly slow).

Failures are per-record and non-fatal: the count of failures is reported at the
end and the run continues, because a partial backfill is strictly better than
none. Exit code is non-zero only if EVERY push failed, which means the AI service
is unreachable rather than a few records being odd.
"""

from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import select

from backend.db import Alert, Journey, SessionLocal
from backend.rag_client import (
    AI_SERVICE_URL,
    RAG_INDEX_TIMEOUT,
    alert_text,
    human_time,
    journey_text,
    push,
)

# Journey states worth indexing: a journey still IN_PROGRESS has no outcome and
# nothing useful to retrieve on.
_TERMINAL = ("SUCCESS", "FAILED", "TIMED_OUT")


async def _backfill_alerts(session, client: httpx.AsyncClient) -> tuple[int, int]:
    """Push every alert row. Returns (pushed, failed)."""
    rows = (await session.execute(select(Alert).order_by(Alert.emitted_at))).scalars().all()
    pushed = failed = 0
    for row in rows:
        ok = await push(
            row.alert_id,
            "alert",
            alert_text(
                row.app_name,
                row.level,
                row.logger,
                row.message,
                row.explanation,
                order_id=row.order_id,
                event_id=row.event_id,
                # The backfill has this where the live push does not: an alert is
                # indexed at persist time, when its journey may not be assembled
                # yet. Re-running the backfill therefore ENRICHES older records —
                # another reason the upsert-by-id idempotency matters.
                journey_id=row.journey_id,
                ts=human_time(row.emitted_at),
            ),
            {
                "department": row.department,
                "severity": row.severity,
                "source": row.source,
                "app_name": row.app_name,
                "level": row.level,
                "order_id": row.order_id,
                "event_id": row.event_id,
                "journey_id": row.journey_id,
                "ts": row.emitted_at.isoformat() if row.emitted_at is not None else None,
            },
            client=client,
        )
        pushed += ok
        failed += not ok
    print(f"[backfill_rag] alerts: {pushed} indexed, {failed} failed (of {len(rows)})", flush=True)
    return pushed, failed


async def _backfill_journeys(session, client: httpx.AsyncClient) -> tuple[int, int]:
    """Push every completed journey that has a summary. Returns (pushed, failed)."""
    stmt = (
        select(Journey)
        .where(Journey.status.in_(_TERMINAL))
        .where(Journey.summary.is_not(None))
        .order_by(Journey.last_ts)
    )
    rows = (await session.execute(stmt)).scalars().all()
    pushed = failed = 0
    for row in rows:
        ok = await push(
            row.journey_id,
            "journey",
            journey_text(
                row.outcome or row.status,
                row.summary,
                order_id=row.order_id,
                event_id=row.event_id,
                journey_id=row.journey_id,
                started=human_time(row.first_ts),
                ended=human_time(row.last_ts),
            ),
            {
                "outcome": row.outcome or row.status,
                "status": row.status,
                "journey_id": row.journey_id,
                "order_id": row.order_id,
                "event_id": row.event_id,
                "ts": row.last_ts.isoformat() if row.last_ts is not None else None,
            },
            client=client,
        )
        pushed += ok
        failed += not ok
    print(
        f"[backfill_rag] journeys: {pushed} indexed, {failed} failed (of {len(rows)})",
        flush=True,
    )
    return pushed, failed


async def backfill() -> tuple[int, int]:
    """Index all alerts + completed journeys. Returns (pushed, failed) totals."""
    print(f"[backfill_rag] target {AI_SERVICE_URL}/index", flush=True)
    async with httpx.AsyncClient(timeout=RAG_INDEX_TIMEOUT) as client:
        async with SessionLocal() as session:
            a_ok, a_bad = await _backfill_alerts(session, client)
            j_ok, j_bad = await _backfill_journeys(session, client)
    return a_ok + j_ok, a_bad + j_bad


def main() -> int:
    pushed, failed = asyncio.run(backfill())
    total = pushed + failed
    print(f"[backfill_rag] done — {pushed}/{total} record(s) indexed", flush=True)
    if total and pushed == 0:
        # Nothing at all got through: the AI service is down or the index is
        # disabled. Non-zero so a caller/CI notices, unlike a few odd records.
        print("[backfill_rag] every push failed — is the AI service running?", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
