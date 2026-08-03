"""Tests for backend/linking.py — alert <-> journey FK linking.

Mostly no DB: we assert the SQL these functions build (compiled to text)
matches on the right correlation columns and — critically — never matches on
null ids (which would wrongly link unrelated orphans). A `_FakeSession` with
pre-seeded per-`execute()` results (same convention as tests/test_incidents.py)
exercises the late-arriving-alert incident backfill without a real DB. A live
round-trip against Postgres is gated behind ``AI_LIVE_DB=1`` for those who want
end-to-end confirmation.
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
        source="fallback",
    )


class _Journey:
    """A stand-in with the four attributes backfill reads."""
    def __init__(self, event_id=None, order_id=None, cart_header_id=None, jid="J1"):
        self.event_id, self.order_id, self.cart_header_id = event_id, order_id, cart_header_id
        self.journey_id = jid


class _Incident:
    """A minimal stand-in — the tests here only care that the RIGHT object
    (identity) comes back out, not its full shape (that's IncidentOut's job)."""
    def __init__(self, incident_id="inc-1"):
        self.incident_id = incident_id


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


# --- fakes (project convention — see tests/test_incidents.py, tests/test_api.py)
class _FakeResult:
    """A result whose `.first()` / `.scalar_one_or_none()` / `.rowcount` are
    whatever the test pre-seeds; anything unset reads as "nothing found"."""
    def __init__(self, *, first=None, scalar=None, rowcount=0):
        self._first = first
        self._scalar = scalar
        self.rowcount = rowcount

    def first(self):
        return self._first

    def scalar_one_or_none(self):
        return self._scalar


class _CapSession:
    """Captures every statement (for SQL-shape assertions) and returns
    pre-seeded results in order; an un-seeded call gets a default "nothing
    found" result, so existing no-incident-yet call sites need no seeding."""
    def __init__(self, results=None):
        self.stmts: list = []
        self._results = list(results or [])

    async def execute(self, stmt):
        self.stmts.append(stmt)
        if self._results:
            return self._results.pop(0)
        return _FakeResult()


# --- link_alert: no incident on the matched journey yet (existing behavior) --
async def test_link_alert_updates_by_alert_id_and_matches_journey():
    s = _CapSession()
    result = await link_alert(s, _alert())
    assert result is None
    assert len(s.stmts) == 1
    sql = _compiled(s.stmts[0])
    assert "UPDATE alerts" in sql
    assert "alert_id" in sql              # keyed on this alert
    assert "journey_id IS NULL" in sql    # never re-link an already-linked alert
    # links via subqueries over the journeys table (journey_id AND incident_id)
    assert "journeys" in sql and "journey_id" in sql and "incident_id" in sql


async def test_link_alert_noop_when_log_has_no_ids():
    s = _CapSession()
    result = await link_alert(s, _alert(log=_log(eventId=None, orderId=None, cartHeaderId=None)))
    assert result is None
    assert s.stmts == []  # nothing to match on -> no statement


# --- link_alert: the matched journey ALREADY has an incident (regression) ---
async def test_link_alert_backfills_incident_and_bumps_alert_count():
    incident = _Incident("inc-1")
    s = _CapSession([
        _FakeResult(first=("inc-1",)),  # the combined UPDATE...RETURNING incident_id
        _FakeResult(scalar=incident),    # the Incident bump UPDATE...RETURNING
    ])
    result = await link_alert(s, _alert())
    assert result is incident
    assert len(s.stmts) == 2
    bump_sql = _compiled(s.stmts[1])
    assert "UPDATE incidents" in bump_sql
    assert "alert_count" in bump_sql
    assert "last_ts" in bump_sql


async def test_link_alert_returns_none_when_alert_already_linked():
    # The UPDATE's WHERE (journey_id IS NULL) matches nothing -> RETURNING is
    # empty -> first() is None, same as "no journey found yet".
    s = _CapSession([_FakeResult(first=None)])
    result = await link_alert(s, _alert())
    assert result is None
    assert len(s.stmts) == 1  # no incident bump attempted


# --- backfill_journey_alerts: no incident on the journey yet (existing) -----
async def test_backfill_updates_orphan_alerts_for_each_journey():
    s = _CapSession()
    result = await backfill_journey_alerts(s, [_Journey(order_id="ORD-1", cart_header_id="C1")])
    assert result == []
    assert len(s.stmts) == 2  # the incident_id lookup, then the alerts UPDATE
    sql = _compiled(s.stmts[1])
    assert "UPDATE alerts" in sql
    assert "journey_id IS NULL" in sql    # only orphans
    assert "ORD-1" in sql                 # matched on the journey's ids


async def test_backfill_skips_journey_with_no_ids():
    s = _CapSession()
    result = await backfill_journey_alerts(s, [_Journey()])  # all ids None
    assert result == []
    assert s.stmts == []


# --- backfill_journey_alerts: the journey ALREADY has an incident (regression)
async def test_backfill_links_incident_and_bumps_alert_count_by_rowcount():
    incident = _Incident("inc-2")
    s = _CapSession([
        _FakeResult(first=("inc-2",)),        # the journey's own incident_id
        _FakeResult(rowcount=3),               # 3 orphan alerts just backfilled
        _FakeResult(scalar=incident),          # the Incident bump UPDATE...RETURNING
    ])
    result = await backfill_journey_alerts(
        s, [_Journey(order_id="ORD-1", cart_header_id="C1")]
    )
    assert result == [incident]
    assert len(s.stmts) == 3
    bump_sql = _compiled(s.stmts[2])
    assert "UPDATE incidents" in bump_sql
    assert "alert_count" in bump_sql


async def test_backfill_no_bump_when_zero_orphans_matched():
    # The journey has an incident, but this particular batch matched zero
    # orphan alerts (rowcount=0) -> nothing to bump, nothing returned.
    s = _CapSession([
        _FakeResult(first=("inc-3",)),
        _FakeResult(rowcount=0),
    ])
    result = await backfill_journey_alerts(
        s, [_Journey(order_id="ORD-1", cart_header_id="C1")]
    )
    assert result == []
    assert len(s.stmts) == 2  # no third (bump) statement issued


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
