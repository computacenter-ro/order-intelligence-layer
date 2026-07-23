"""Tests for the backend REST API (backend/api.py + backend/main.py).

No database and no broker: the query-building logic is asserted by compiling the
statements to SQL (filters + ordering), and the HTTP layer is driven through
``fastapi``'s ``dependency_overrides`` with a fake session that returns seeded
ORM instances. This mirrors the compiled-SQL testing style already used for
``backend/consumers.py``.

Contract checked (CLAUDE.md [5] "API"):

* ``GET /alerts?since=&department=&source=`` — filtered, newest-first.
* ``GET /journeys?status=`` — filtered.
* ``GET /journeys/{id}`` — a journey + its events (ordered by ts) + summary; 404
  when it does not exist.
* responses are Pydantic schemas (never raw ORM), and every datetime is
  UTC-aware.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from backend.main import app
from backend.db import get_session, Alert, Journey, JourneyEvent
from backend.api import build_alerts_query, build_journeys_query
from backend.auth import get_current_user

UTC = timezone.utc


# --- fakes -------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _FakeResult:
    def __init__(self, *, items=None, one=None):
        self._items = items or []
        self._one = one

    def scalars(self):
        return _FakeScalars(self._items)

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    """Returns pre-seeded results per execute() call, recording the statements."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0)

    async def commit(self):
        pass


@pytest.fixture(autouse=True)
def _authenticated():
    """Every read route requires a session (get_current_user). These tests
    assert the *query/serialization* contract, not auth, so we satisfy the
    dependency with a stub user. The "auth is actually enforced" contract is
    covered separately in test_requires_auth below (which clears this override).
    """
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    yield
    app.dependency_overrides.clear()


def _use(results) -> _FakeSession:
    session = _FakeSession(results)

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    return session


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --- ORM factories (transient instances; unset columns read back as None) ----


def _alert(**over) -> Alert:
    base = dict(
        alert_id="alert-1",
        emitted_at=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
        log_id="log-1",
        level="ERROR",
        app_name="cc-order-engine",
        logger="c.c.orderengine.service.OrderService",
        message="boom",
        source="ai",
        explanation="explained",
        department="backend",
        is_resolved=False,
        confidence=0.8,
    )
    base.update(over)
    return Alert(**base)


def _journey(**over) -> Journey:
    base = dict(
        journey_id="J1",
        status="SUCCESS",
        outcome="SUCCESS",
        first_ts=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
        last_ts=datetime(2026, 7, 20, 8, 0, 5, tzinfo=UTC),
        event_id="evt-1",
        order_id="ORD-6001",
        cart_header_id="1840927365018240001",
        summary="all good",
    )
    base.update(over)
    return Journey(**base)


def _event(**over) -> JourneyEvent:
    base = dict(
        journey_id="J1",
        log_id="e-1",
        ts=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
        raw={"log_id": "e-1", "message": "hi"},
    )
    base.update(over)
    return JourneyEvent(**base)


# --- auth enforcement --------------------------------------------------------


@pytest.mark.parametrize("path", ["/alerts", "/journeys", "/journeys/J1"])
def test_requires_auth(path):
    """Without a valid session, every read route is 401 — no cookie, no data."""
    app.dependency_overrides.clear()  # drop the autouse stub user for this test
    r = TestClient(app).get(path)
    assert r.status_code == 401


def test_resolve_alert_requires_auth():
    """The write route sits on the same auth-guarded router as the reads."""
    app.dependency_overrides.clear()  # drop the autouse stub user for this test
    r = TestClient(app).patch("/alerts/a1/resolve")
    assert r.status_code == 401


# --- query builders (pure; asserted via compiled SQL) ------------------------


def test_alerts_query_applies_all_filters_and_desc_order():
    sql = _compiled(build_alerts_query(datetime(2026, 7, 20, tzinfo=UTC), "backend", "ai"))
    assert "emitted_at >=" in sql
    assert "department =" in sql
    assert "source =" in sql
    assert "ORDER BY alerts.emitted_at DESC" in sql


def test_alerts_query_no_filters_still_orders_desc():
    sql = _compiled(build_alerts_query(None, None, None))
    assert "WHERE" not in sql
    assert "ORDER BY alerts.emitted_at DESC" in sql


def test_journeys_query_status_filter():
    assert "status =" in _compiled(build_journeys_query("SUCCESS"))
    assert "WHERE" not in _compiled(build_journeys_query(None))


# --- GET /alerts -------------------------------------------------------------


def test_get_alerts_serializes_schema_not_orm():
    _use([_FakeResult(items=[_alert(alert_id="a1", source="ai"),
                             _alert(alert_id="a2", source="fallback",
                                    explanation=None, department=None, confidence=None)])])
    r = TestClient(app).get("/alerts")
    assert r.status_code == 200
    body = r.json()
    assert [a["alert_id"] for a in body] == ["a1", "a2"]
    assert body[0]["department"] == "backend" and body[0]["source"] == "ai"
    # fallback alert carries null enrichment
    assert body[1]["explanation"] is None and body[1]["department"] is None
    # datetime is UTC-aware in the response
    assert body[0]["emitted_at"].endswith(("Z", "+00:00"))


def test_get_alerts_passes_query_params_into_the_filter():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get(
        "/alerts",
        params={"since": "2026-07-20T00:00:00+00:00", "department": "devops", "source": "ai"},
    )
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "emitted_at >=" in sql and "department =" in sql and "source =" in sql


# --- PATCH /alerts/{id}/resolve -----------------------------------------------


def test_resolve_alert_marks_resolved_and_returns_updated_alert():
    session = _use([_FakeResult(one=_alert(
        alert_id="a1",
        is_resolved=True,
        resolved_at=datetime(2026, 7, 23, 9, 0, 0, tzinfo=UTC),
    ))])
    r = TestClient(app).patch("/alerts/a1/resolve")
    assert r.status_code == 200
    body = r.json()
    assert body["is_resolved"] is True
    assert body["resolved_at"] is not None
    sql = _compiled(session.statements[0])
    assert "UPDATE alerts" in sql and "is_resolved" in sql


def test_resolve_alert_404_when_missing():
    _use([_FakeResult(one=None)])
    r = TestClient(app).patch("/alerts/missing/resolve")
    assert r.status_code == 404


# --- GET /journeys -----------------------------------------------------------


def test_get_journeys_filters_by_status():
    session = _use([_FakeResult(items=[_journey(journey_id="J9", status="TIMED_OUT")])])
    r = TestClient(app).get("/journeys", params={"status": "TIMED_OUT"})
    assert r.status_code == 200
    assert r.json()[0]["journey_id"] == "J9"
    assert "status =" in _compiled(session.statements[0])


# --- GET /journeys/{id} ------------------------------------------------------


def test_get_journey_detail_includes_events_ordered_and_summary():
    session = _use([
        _FakeResult(one=_journey(journey_id="J1", summary="done")),
        _FakeResult(items=[
            _event(log_id="l1", ts=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC),
                   raw={"log_id": "l1", "message": "first"}),
            _event(log_id="l2", ts=datetime(2026, 7, 20, 8, 0, 5, tzinfo=UTC),
                   raw={"log_id": "l2", "message": "second"}),
        ]),
    ])
    r = TestClient(app).get("/journeys/J1")
    assert r.status_code == 200
    body = r.json()
    assert body["journey_id"] == "J1" and body["summary"] == "done"
    assert [e["log_id"] for e in body["events"]] == ["l1", "l2"]
    assert body["events"][0]["raw"]["message"] == "first"
    # events are queried ordered by ts ascending
    assert "ORDER BY journey_events.ts ASC" in _compiled(session.statements[1])


def test_get_journey_404_when_missing():
    _use([_FakeResult(one=None)])
    r = TestClient(app).get("/journeys/NOPE")
    assert r.status_code == 404


# --- datetime contract -------------------------------------------------------


def test_naive_datetime_is_returned_as_utc_aware():
    # A naive datetime sneaking out of the DB must still be rendered UTC-aware.
    _use([_FakeResult(items=[_alert(emitted_at=datetime(2026, 7, 20, 8, 0, 0))])])
    body = TestClient(app).get("/alerts").json()
    assert body[0]["emitted_at"].endswith(("Z", "+00:00"))
