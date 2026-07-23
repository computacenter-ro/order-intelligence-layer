"""Tests for backend/linking.py — alert <-> journey FK linking.

No DB: we assert the SQL these functions build (compiled to text) matches on the
right correlation columns and — critically — never matches on null ids (which
would wrongly link unrelated orphans). A live round-trip against Postgres is
gated behind ``AI_LIVE_DB=1`` for those who want end-to-end confirmation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from backend.linking import _alert_matches_journey_cols, backfill_journey_alerts, link_alert
from shared.models import Department, LogLine, ProcessedAlert


def _log(**over) -> LogLine:
    base = dict(
        log_id="log-1", timestamp=datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc),
        app_name="cc-order-engine", level="ERROR", logger="l", host="h",
        process_id="1", thread="t", message="boom",
        eventId="evt-1", orderId="ORD-1", cartHeaderId="C1",
    )
    base.update(over)
    return LogLine(**base)


def _alert(**over) -> ProcessedAlert:
    return ProcessedAlert(
        alert_id=over.pop("alert_id", "a1"),
        emitted_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=timezone.utc),
        log=over.pop("log", _log()), explanation=None, department=None,
        confidence=None, source="fallback",
    )


class _Journey:
    """A stand-in with the three alias attributes backfill reads."""
    def __init__(self, event_id=None, order_id=None, cart_header_id=None, jid="J1"):
        self.event_id, self.order_id, self.cart_header_id = event_id, order_id, cart_header_id
        self.journey_id = jid


def _compiled(stmt) -> str:
    from sqlalchemy.dialects import postgresql
    return str(stmt.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True}))


# --- null-safe column matching ----------------------------------------------
def test_match_clause_uses_only_non_null_ids():
    clause = _alert_matches_journey_cols("evt-1", "ORD-1", None)
    sql = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "event_id" in sql and "order_id" in sql
    assert "cart_header_id" not in sql  # null id must not be matched on


def test_match_clause_none_when_all_ids_null():
    assert _alert_matches_journey_cols(None, None, None) is None


# --- link_alert: capture the UPDATE it runs ---------------------------------
class _CapSession:
    def __init__(self): self.stmts = []
    async def execute(self, stmt): self.stmts.append(stmt); return None


async def test_link_alert_updates_by_alert_id_and_matches_journey():
    s = _CapSession()
    await link_alert(s, _alert())
    assert len(s.stmts) == 1
    sql = _compiled(s.stmts[0])
    assert "UPDATE alerts" in sql
    assert "alert_id" in sql              # keyed on this alert
    assert "journey_id IS NULL" in sql    # never re-link an already-linked alert
    # links via a subquery over the journeys table
    assert "journeys" in sql and "journey_id" in sql


async def test_link_alert_noop_when_log_has_no_ids():
    s = _CapSession()
    await link_alert(s, _alert(log=_log(eventId=None, orderId=None, cartHeaderId=None)))
    assert s.stmts == []  # nothing to match on -> no statement


# --- backfill_journey_alerts -------------------------------------------------
async def test_backfill_updates_orphan_alerts_for_each_journey():
    s = _CapSession()
    await backfill_journey_alerts(s, [_Journey(order_id="ORD-1", cart_header_id="C1")])
    assert len(s.stmts) == 1
    sql = _compiled(s.stmts[0])
    assert "UPDATE alerts" in sql
    assert "journey_id IS NULL" in sql    # only orphans
    assert "ORD-1" in sql                 # matched on the journey's ids


async def test_backfill_skips_journey_with_no_ids():
    s = _CapSession()
    await backfill_journey_alerts(s, [_Journey()])  # all ids None
    assert s.stmts == []


# --- live DB round-trip (opt-in) --------------------------------------------
@pytest.mark.skipif(
    os.getenv("AI_LIVE_DB") != "1",
    reason="live Postgres linking round-trip; set AI_LIVE_DB=1 (docker compose up -d postgres)",
)
async def test_live_alert_links_to_existing_journey():
    """Insert a journey, then an alert sharing its order_id, link, and assert."""
    import uuid

    from sqlalchemy import select
    from backend.db import Alert, Journey, SessionLocal

    oid = f"ORD-{uuid.uuid4().hex[:8]}"
    jid = uuid.uuid4().hex
    aid = f"a-{uuid.uuid4().hex[:8]}"
    async with SessionLocal() as s:
        s.add(Journey(journey_id=jid, status="SUCCESS", order_id=oid, cart_header_id="C-live"))
        await s.commit()

    async with SessionLocal() as s:
        s.add(Alert(
            alert_id=aid, emitted_at=datetime.now(timezone.utc),
            log_id=f"l-{uuid.uuid4().hex[:8]}", level="ERROR", app_name="x", logger="l",
            message="m", order_id=oid, source="fallback",
        ))
        await s.commit()
        # The ProcessedAlert passed to link_alert must carry the SAME alert_id as
        # the persisted row (link_alert updates WHERE alert_id = ...).
        alert = _alert(alert_id=aid, log=_log(eventId=None, orderId=oid, cartHeaderId=None))
        await link_alert(s, alert)  # matches on order_id
        await s.commit()

    async with SessionLocal() as s:
        rows = (await s.execute(select(Alert).where(Alert.order_id == oid))).scalars().all()
        assert rows and all(r.journey_id == jid for r in rows)
