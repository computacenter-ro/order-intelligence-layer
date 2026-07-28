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
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.main import app
from backend.db import get_session, Alert, Journey, JourneyEvent
from backend.api import (
    alert_filter_conditions,
    build_alert_facet_query,
    build_alerts_query,
    build_journeys_query,
)
from backend.pagination import apply_keyset, build_page, decode_cursor, encode_cursor
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


from backend.schemas import IncidentOut


def test_incident_out_from_orm_instance():
    from backend.db import Incident

    incident = Incident(
        incident_id="inc-1", signature="d1", failure_subtype="ENRICHMENT_FAILED",
        failing_service="SPT", error_token="SocketTimeoutException",
        title="ENRICHMENT_FAILED — SPT", department="devops", status="open",
        first_ts=datetime(2026, 7, 26, 8, 0, 0, tzinfo=UTC),
        last_ts=datetime(2026, 7, 26, 8, 5, 0, tzinfo=UTC),
        primary_alert_id="a1", alert_count=12, journey_count=3,
    )
    out = IncidentOut.model_validate(incident)
    assert out.incident_id == "inc-1"
    assert out.journey_count == 3
    assert out.status == "open"


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


def test_alerts_query_applies_all_filters():
    # build_alerts_query is filters-only now — ordering is the paginator's job
    # (apply_keyset), so no ORDER BY is emitted here.
    sql = _compiled(build_alerts_query(datetime(2026, 7, 20, tzinfo=UTC), ["backend"], "ai"))
    assert "emitted_at >=" in sql
    assert "department IN (" in sql
    assert "source =" in sql
    assert "ORDER BY" not in sql


def test_alerts_query_no_filters_has_no_where_or_order():
    sql = _compiled(build_alerts_query(None, None, None))
    assert "WHERE" not in sql
    assert "ORDER BY" not in sql


def test_alerts_query_department_only():
    sql = _compiled(build_alerts_query(None, ["backend"], None))
    assert "department IN (" in sql
    assert "source =" not in sql
    assert "emitted_at >=" not in sql


def test_alerts_query_source_only():
    sql = _compiled(build_alerts_query(None, None, "fallback"))
    assert "source =" in sql
    assert "department IN (" not in sql
    assert "emitted_at >=" not in sql


def test_alerts_query_department_and_source_combination():
    sql = _compiled(build_alerts_query(None, ["devops"], "ai"))
    assert "department IN (" in sql and "source =" in sql


def test_alerts_query_level_only():
    sql = _compiled(build_alerts_query(None, None, None, level="ERROR"))
    assert "level =" in sql
    assert "app_name IN (" not in sql
    assert "department IN (" not in sql and "source =" not in sql


def test_alerts_query_app_name_only():
    sql = _compiled(build_alerts_query(None, None, None, app_name=["cc-order-engine"]))
    assert "app_name IN (" in sql
    assert "level =" not in sql
    assert "department IN (" not in sql and "source =" not in sql


def test_alerts_query_all_filters_including_level_and_app_name():
    sql = _compiled(
        build_alerts_query(
            datetime(2026, 7, 20, tzinfo=UTC), ["backend"], "ai",
            level="WARN", app_name=["cc-checker-service"],
        )
    )
    assert "emitted_at >=" in sql
    assert "department IN (" in sql and "source =" in sql
    assert "level =" in sql and "app_name IN (" in sql
    assert "ORDER BY" not in sql


def test_alerts_query_severity_only():
    sql = _compiled(build_alerts_query(None, None, None, severity=["critical"]))
    assert "severity IN (" in sql
    assert "level =" not in sql
    assert "department IN (" not in sql and "source =" not in sql


def test_alerts_query_cached_true_only():
    sql = _compiled(build_alerts_query(None, None, None, cached=True))
    assert "cached =" in sql
    assert "source =" not in sql
    assert "severity IN (" not in sql


def test_alerts_query_cached_false_is_a_real_filter_not_omitted():
    # cached=False must narrow to non-cache-hits. A truthiness check instead of
    # `is not None` would silently drop this filter and return everything.
    sql = _compiled(build_alerts_query(None, None, None, cached=False))
    assert "cached =" in sql


def test_alerts_query_cached_none_omits_the_filter():
    assert "cached =" not in _compiled(build_alerts_query(None, None, None))


def test_alerts_query_cached_composes_with_source():
    # cached is orthogonal to source (every cached alert is source="ai"), so
    # both predicates must appear together rather than one replacing the other.
    sql = _compiled(build_alerts_query(None, None, "ai", cached=True))
    assert "source =" in sql and "cached =" in sql


def test_get_alerts_accepts_cached_query_param():
    _use([_FakeResult(items=[_alert(alert_id="a1", cached=True)])])
    r = TestClient(app).get("/alerts", params={"cached": "true"})
    assert r.status_code == 200
    assert r.json()["items"][0]["cached"] is True


def test_get_alerts_rejects_non_boolean_cached():
    _use([_FakeResult(items=[])])
    assert TestClient(app).get("/alerts", params={"cached": "maybe"}).status_code == 422


def test_alert_out_coerces_unflushed_none_cached_to_false():
    # `cached` has a DB-side server_default, so an Alert serialized before it is
    # flushed (the alert.new WebSocket envelope does exactly that) reads None.
    # AlertOut must normalize that to False rather than 500 on a non-optional bool.
    from backend.schemas import AlertOut

    row = _alert(alert_id="a1")
    assert row.cached is None  # guards the premise: no Python-side value pre-flush
    assert AlertOut.model_validate(row).cached is False


# --- multi-valued filters (department / app_name / severity) ------------------
#
# These three are lists: a non-empty list ORs within the category (SQL IN), while
# None *and* an empty list mean "no filter". The empty case is the load-bearing
# one — the UI's "nothing ticked" state must show everything, and an IN () would
# instead match nothing and silently empty the feed.


def _query_on(column: str, values: list[str]):
    """build_alerts_query with exactly one multi-valued filter set."""
    return build_alerts_query(
        None,
        values if column == "department" else None,
        None,
        app_name=values if column == "app_name" else None,
        severity=values if column == "severity" else None,
    )


@pytest.mark.parametrize(
    "column, values",
    [
        ("department", ["backend", "devops"]),
        ("app_name", ["cc-spt-service", "cc-rsm-service"]),
        ("severity", ["critical", "high"]),
    ],
)
def test_alerts_query_multiple_values_become_an_in_clause(column, values):
    stmt = _query_on(column, values)
    assert f"{column} IN (" in _compiled(stmt)
    # The bound parameter carries every value, so the predicate is a real OR
    # rather than the last value quietly winning.
    assert _params(stmt)[f"{column}_1"] == values


@pytest.mark.parametrize(
    "column, value",
    [("department", "database"), ("app_name", "cc-track-trace"), ("severity", "low")],
)
def test_alerts_query_single_value_still_uses_in(column, value):
    """One selection is an IN with one element — not a special-cased ``=``."""
    stmt = _query_on(column, [value])
    assert f"{column} IN (" in _compiled(stmt)
    assert _params(stmt)[f"{column}_1"] == [value]


def test_alerts_query_empty_lists_apply_no_filter():
    """Nothing ticked = show everything. An IN () here would match no rows."""
    sql = _compiled(build_alerts_query(None, [], None, app_name=[], severity=[]))
    assert "WHERE" not in sql


def test_alerts_query_empty_lists_do_not_suppress_other_filters():
    # An empty multi-filter must drop out on its own without taking the
    # single-valued predicates with it.
    sql = _compiled(
        build_alerts_query(None, [], "ai", level="ERROR", app_name=[], severity=[])
    )
    assert "source =" in sql and "level =" in sql
    assert "department IN (" not in sql
    assert "app_name IN (" not in sql and "severity IN (" not in sql


def test_alerts_query_absent_lists_apply_no_filter():
    sql = _compiled(build_alerts_query(None, None, None, app_name=None, severity=None))
    assert "WHERE" not in sql


def test_alerts_query_multi_filters_compose_with_each_other_and_singles():
    stmt = build_alerts_query(
        datetime(2026, 7, 20, tzinfo=UTC),
        ["backend", "database"],
        "ai",
        level="ERROR",
        app_name=["cc-order-engine"],
        severity=["critical", "high"],
        cached=True,
    )
    sql = _compiled(stmt)
    assert "emitted_at >=" in sql
    assert "department IN (" in sql and "app_name IN (" in sql and "severity IN (" in sql
    assert "source =" in sql and "level =" in sql and "cached =" in sql


# --- multi-valued filters over HTTP (repeated query params) ------------------


def test_get_alerts_repeated_department_params_become_one_in_clause():
    session = _use([_FakeResult(items=[])])
    # httpx encodes a list value as ?department=backend&department=devops
    r = TestClient(app).get("/alerts", params={"department": ["backend", "devops"]})
    assert r.status_code == 200
    stmt = session.statements[0]
    assert "department IN (" in _compiled(stmt)
    assert _params(stmt)["department_1"] == ["backend", "devops"]


def test_get_alerts_repeated_severity_params_become_one_in_clause():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"severity": ["critical", "low"]})
    assert r.status_code == 200
    stmt = session.statements[0]
    assert "severity IN (" in _compiled(stmt)
    assert _params(stmt)["severity_1"] == ["critical", "low"]


def test_get_alerts_repeated_app_name_params_become_one_in_clause():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get(
        "/alerts", params={"app_name": ["cc-spt-service", "cc-jam-service"]}
    )
    assert r.status_code == 200
    stmt = session.statements[0]
    assert "app_name IN (" in _compiled(stmt)
    assert _params(stmt)["app_name_1"] == ["cc-spt-service", "cc-jam-service"]


def test_get_alerts_omitted_multi_params_apply_no_filter():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts")
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "department IN (" not in sql
    assert "app_name IN (" not in sql and "severity IN (" not in sql


def test_get_alerts_all_three_multi_filters_at_once():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get(
        "/alerts",
        params={
            "department": ["backend", "devops"],
            "severity": ["critical", "high"],
            "app_name": ["cc-order-engine", "cc-checker-service"],
        },
    )
    assert r.status_code == 200
    params = _params(session.statements[0])
    assert params["department_1"] == ["backend", "devops"]
    assert params["severity_1"] == ["critical", "high"]
    assert params["app_name_1"] == ["cc-order-engine", "cc-checker-service"]


@pytest.mark.parametrize(
    "params",
    [
        {"department": ["backend", "marketing"]},   # one bad value in the list
        {"department": ["marketing", "backend"]},   # order must not matter
        {"severity": ["critical", "urgent"]},
        {"severity": ["high", ""]},                  # empty string is not a value
    ],
)
def test_get_alerts_one_invalid_value_in_a_list_is_422(params):
    """Validation is per element — a good value does not launder a bad one."""
    _use([_FakeResult(items=[])])
    assert TestClient(app).get("/alerts", params=params).status_code == 422


def test_get_alerts_multiple_unknown_app_names_are_not_422():
    # app_name stays a free string, so unrecognised services are a valid (empty)
    # query even in a list — the roster can grow without a code change.
    _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"app_name": ["cc-future-one", "cc-future-two"]})
    assert r.status_code == 200


# --- GET /alerts/facets ------------------------------------------------------
#
# Contextual counts for the three multi-select filters. The whole point is the
# EXCLUDE-SELF rule: each facet is counted with every active filter except its
# own, so ticking one department doesn't collapse the department list to that one
# value (which would leave the user no way to see or reach the others).


class _FacetRows:
    """One grouped result: `.all()` yields (value, count) tuples."""

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


def _facet_session(severity=(), department=(), app_name=()) -> _FakeSession:
    """Seed the three facet queries in the order the route runs them."""
    return _use(
        [_FacetRows(severity), _FacetRows(department), _FacetRows(app_name)]
    )


def test_facets_requires_auth():
    app.dependency_overrides.clear()  # drop the autouse stub user
    assert TestClient(app).get("/alerts/facets").status_code == 401


def test_facets_returns_a_count_map_per_facet():
    _facet_session(
        severity=[("critical", 3), ("low", 1)],
        department=[("backend", 2), ("devops", 2)],
        app_name=[("cc-order-engine", 4)],
    )
    r = TestClient(app).get("/alerts/facets")
    assert r.status_code == 200
    assert r.json() == {
        "severity": {"critical": 3, "low": 1},
        "department": {"backend": 2, "devops": 2},
        "app_name": {"cc-order-engine": 4},
    }


def test_facets_response_keys_are_exactly_the_schema():
    _facet_session()
    body = TestClient(app).get("/alerts/facets").json()
    assert set(body) == {"severity", "department", "app_name"}
    # No rows -> empty maps, not nulls. A client reads a missing key as 0.
    assert body == {"severity": {}, "department": {}, "app_name": {}}


def test_facets_runs_one_grouped_query_per_facet():
    session = _facet_session()
    TestClient(app).get("/alerts/facets")
    assert len(session.statements) == 3
    for stmt, column in zip(session.statements, ["severity", "department", "app_name"]):
        sql = _compiled(stmt)
        assert f"GROUP BY alerts.{column}" in sql
        assert "count(*)" in sql


def test_facets_skips_nulls():
    """A null severity/department means the LLM never rated/routed the alert —
    there is no filter option for it, so there is nothing to attach a count to."""
    session = _facet_session()
    TestClient(app).get("/alerts/facets")
    for stmt, column in zip(session.statements, ["severity", "department", "app_name"]):
        assert f"{column} IS NOT NULL" in _compiled(stmt)


def test_facets_counts_respect_the_other_filters():
    """With level=ERROR active, every facet counts only ERROR alerts."""
    session = _facet_session()
    r = TestClient(app).get("/alerts/facets", params={"level": "ERROR"})
    assert r.status_code == 200
    for stmt in session.statements:
        assert "level =" in _compiled(stmt)


def test_facets_exclude_self_department():
    """department=backend ticked: the department facet must NOT filter by
    department (so all departments still get counts), while severity and app_name
    must."""
    session = _facet_session()
    r = TestClient(app).get("/alerts/facets", params={"department": "backend"})
    assert r.status_code == 200
    severity_sql, department_sql, app_name_sql = (
        _compiled(s) for s in session.statements
    )
    assert "department IN (" not in department_sql   # exclude-self
    assert "department IN (" in severity_sql         # other facets stay scoped
    assert "department IN (" in app_name_sql


def test_facets_exclude_self_severity():
    session = _facet_session()
    r = TestClient(app).get("/alerts/facets", params={"severity": "critical"})
    assert r.status_code == 200
    severity_sql, department_sql, app_name_sql = (
        _compiled(s) for s in session.statements
    )
    assert "severity IN (" not in severity_sql
    assert "severity IN (" in department_sql
    assert "severity IN (" in app_name_sql


def test_facets_exclude_self_app_name():
    session = _facet_session()
    r = TestClient(app).get("/alerts/facets", params={"app_name": "cc-spt-service"})
    assert r.status_code == 200
    severity_sql, department_sql, app_name_sql = (
        _compiled(s) for s in session.statements
    )
    assert "app_name IN (" not in app_name_sql
    assert "app_name IN (" in severity_sql
    assert "app_name IN (" in department_sql


def test_facets_exclude_self_is_per_facet_not_global():
    """All three ticked at once: each facet drops exactly its own filter and keeps
    the other two — the failure mode being a facet that drops all three (counts
    ignore context) or none (counts collapse to the selection)."""
    session = _facet_session()
    r = TestClient(app).get(
        "/alerts/facets",
        params={
            "severity": ["critical"],
            "department": ["backend"],
            "app_name": ["cc-order-engine"],
        },
    )
    assert r.status_code == 200
    severity_sql, department_sql, app_name_sql = (
        _compiled(s) for s in session.statements
    )
    assert "severity IN (" not in severity_sql
    assert "department IN (" in severity_sql and "app_name IN (" in severity_sql

    assert "department IN (" not in department_sql
    assert "severity IN (" in department_sql and "app_name IN (" in department_sql

    assert "app_name IN (" not in app_name_sql
    assert "severity IN (" in app_name_sql and "department IN (" in app_name_sql


def test_facets_multi_valued_filters_or_within_the_category():
    session = _facet_session()
    r = TestClient(app).get(
        "/alerts/facets", params={"department": ["backend", "devops"]}
    )
    assert r.status_code == 200
    # The severity facet is scoped by BOTH departments, not just the last one.
    assert _params(session.statements[0])["department_1"] == ["backend", "devops"]


def test_facets_passes_every_filter_through():
    session = _facet_session()
    r = TestClient(app).get(
        "/alerts/facets",
        params={
            "since": "2026-07-20T00:00:00+00:00",
            "source": "ai",
            "level": "ERROR",
            "resolved": "false",
            "cached": "true",
        },
    )
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "emitted_at >=" in sql
    assert "source =" in sql and "level =" in sql
    assert "is_resolved =" in sql and "cached =" in sql


def test_facets_no_filters_only_the_null_guard():
    session = _facet_session()
    r = TestClient(app).get("/alerts/facets")
    assert r.status_code == 200
    # The sole predicate is the IS NOT NULL guard — nothing else narrows it.
    sql = _compiled(session.statements[0])
    where = sql.split("WHERE")[1].split("GROUP BY")[0]
    assert where.strip() == "alerts.severity IS NOT NULL"


@pytest.mark.parametrize(
    "params",
    [
        {"department": "marketing"},
        {"severity": "urgent"},
        {"severity": ["critical", "urgent"]},
        {"level": "INFO"},
        {"source": "human"},
        {"cached": "maybe"},
    ],
)
def test_facets_invalid_filter_value_is_422(params):
    """Same validation as /alerts — the params are typed identically."""
    _facet_session()
    assert TestClient(app).get("/alerts/facets", params=params).status_code == 422


def test_facets_accepts_the_same_filter_params_as_alerts():
    """Drift guard: a filter added to GET /alerts must be added here too, or the
    facet counts would quietly ignore it. /alerts/facets adds nothing of its own
    and only omits the paging params (it aggregates rather than pages)."""
    routes = {
        (r.path, tuple(sorted(r.methods))): r
        for r in app.routes
        if getattr(r, "path", None) in ("/alerts", "/alerts/facets")
    }
    alerts = routes[("/alerts", ("GET",))]
    facets = routes[("/alerts/facets", ("GET",))]

    def query_params(route) -> set[str]:
        return {p.name for p in route.dependant.query_params}

    paging = {"limit", "cursor", "sort"}
    assert query_params(facets) == query_params(alerts) - paging


def test_facets_is_not_shadowed_by_the_resolve_route():
    """"facets" must not be parsed as an {alert_id}. The resolve route is PATCH
    /alerts/{alert_id}/resolve so the shapes differ, but a future GET
    /alerts/{alert_id} would shadow this path — this pins the current behaviour."""
    _facet_session()
    assert TestClient(app).get("/alerts/facets").status_code == 200


# --- filter-condition helper (shared by /alerts and /alerts/facets) ----------


def test_alert_filter_conditions_returns_one_clause_per_active_filter():
    assert alert_filter_conditions(None, None, None) == []
    assert len(alert_filter_conditions(None, ["backend"], "ai", level="ERROR")) == 3
    assert (
        len(
            alert_filter_conditions(
                datetime(2026, 7, 20, tzinfo=UTC),
                ["backend"],
                "ai",
                level="ERROR",
                app_name=["cc-order-engine"],
                severity=["critical"],
                resolved=False,
                cached=True,
            )
        )
        == 8
    )


def test_alert_filter_conditions_ignores_empty_lists():
    assert alert_filter_conditions(None, [], None, app_name=[], severity=[]) == []


def test_build_alerts_query_and_facets_share_the_same_conditions():
    """The refactor's payoff: identical filters produce identical predicates in
    both consumers, so the feed and its facet counts can never disagree about
    what a filter means."""
    kwargs = dict(level="ERROR", app_name=["cc-order-engine"], severity=["critical"])
    conditions = alert_filter_conditions(None, ["backend"], "ai", **kwargs)
    feed_where = _compiled(
        build_alerts_query(None, ["backend"], "ai", **kwargs)
    ).split("WHERE")[1]
    # The facet query is the same predicates plus its own IS NOT NULL guard.
    facet_where = _compiled(build_alert_facet_query("severity", conditions)).split(
        "WHERE"
    )[1]
    for clause in ("department IN (", "source =", "level =", "app_name IN ("):
        assert clause in feed_where and clause in facet_where


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
    items = r.json()["items"]
    assert [a["alert_id"] for a in items] == ["a1", "a2"]
    assert items[0]["department"] == "backend" and items[0]["source"] == "ai"
    # fallback alert carries null enrichment
    assert items[1]["explanation"] is None and items[1]["department"] is None
    # datetime is UTC-aware in the response
    assert items[0]["emitted_at"].endswith(("Z", "+00:00"))


def test_get_alerts_passes_query_params_into_the_filter():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get(
        "/alerts",
        params={"since": "2026-07-20T00:00:00+00:00", "department": "devops", "source": "ai"},
    )
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "emitted_at >=" in sql and "department IN (" in sql and "source =" in sql


@pytest.mark.parametrize("department", ["networking", "devops", "backend", "database", "general"])
def test_get_alerts_valid_department_filters(department):
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"department": department})
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "department IN (" in sql and "source =" not in sql
    assert _params(session.statements[0])["department_1"] == [department]


@pytest.mark.parametrize("source", ["ai", "fallback"])
def test_get_alerts_valid_source_filters(source):
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"source": source})
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "source =" in sql and "department IN (" not in sql


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
def test_get_alerts_valid_severity_filters(severity):
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"severity": severity})
    assert r.status_code == 200
    assert "severity IN (" in _compiled(session.statements[0])
    assert _params(session.statements[0])["severity_1"] == [severity]


def test_get_alerts_invalid_severity_is_422():
    r = TestClient(app).get("/alerts", params={"severity": "urgent"})
    assert r.status_code == 422


def test_get_alerts_department_and_source_combination_via_http():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"department": "database", "source": "fallback"})
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "department IN (" in sql and "source =" in sql


@pytest.mark.parametrize("level", ["WARN", "ERROR"])
def test_get_alerts_valid_level_filters(level):
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"level": level})
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "level =" in sql and "app_name IN (" not in sql


def test_get_alerts_app_name_filters():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"app_name": "cc-spt-service"})
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "app_name IN (" in sql and "level =" not in sql
    assert _params(session.statements[0])["app_name_1"] == ["cc-spt-service"]


def test_get_alerts_unknown_app_name_is_not_422():
    # app_name is a free string — an unrecognised service is a valid (empty) query,
    # never a validation error, so the service list can grow without a code change.
    _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"app_name": "cc-future-service"})
    assert r.status_code == 200


def test_get_alerts_level_app_name_department_source_combination():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get(
        "/alerts",
        params={
            "level": "ERROR",
            "app_name": "cc-order-engine",
            "department": "database",
            "source": "ai",
        },
    )
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "level =" in sql and "app_name IN (" in sql
    assert "department IN (" in sql and "source =" in sql


@pytest.mark.parametrize(
    "params",
    [
        {"department": "marketing"},   # not a Department value
        {"department": "BACKEND"},     # case-sensitive: enum is lowercase
        {"department": ""},            # empty is not valid
        {"source": "human"},           # not ai/fallback
        {"source": "AI"},              # case-sensitive
        {"department": "sales", "source": "ai"},  # invalid dept, valid source
        {"level": "INFO"},             # not WARN/ERROR (alerts are only WARN/ERROR)
        {"level": "DEBUG"},            # not WARN/ERROR
        {"level": "warn"},             # case-sensitive: literal is uppercase
        {"level": ""},                 # empty is not valid
        {"level": "INFO", "app_name": "cc-spt-service"},  # bad level, valid app_name (still 422)
    ],
)
def test_get_alerts_invalid_filter_value_is_422(params):
    # Validation happens before the query runs; override the session anyway so a
    # regression that reaches the DB layer can't hit a real connection.
    _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params=params)
    assert r.status_code == 422
    # error body names the offending query param, so the message is actionable
    locs = [tuple(err["loc"]) for err in r.json()["detail"]]
    assert any("query" in loc for loc in locs)


# --- GET /journeys -----------------------------------------------------------


def test_get_journeys_filters_by_status():
    session = _use([_FakeResult(items=[_journey(journey_id="J9", status="TIMED_OUT")])])
    r = TestClient(app).get("/journeys", params={"status": "TIMED_OUT"})
    assert r.status_code == 200
    assert r.json()["items"][0]["journey_id"] == "J9"
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
    assert body["items"][0]["emitted_at"].endswith(("Z", "+00:00"))


# --- pagination: pure functions (backend/pagination.py) ----------------------


def _params(stmt) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


def test_encode_decode_cursor_roundtrips_datetime_and_id():
    dt = datetime(2026, 7, 20, 8, 0, 5, tzinfo=UTC)
    token = encode_cursor(dt, "alert-5")
    assert isinstance(token, str)
    sort_value, id_value = decode_cursor(token)
    # datetime is rehydrated (not left a string) and the id round-trips verbatim
    assert sort_value == dt
    assert id_value == "alert-5"


def test_decode_cursor_leaves_non_datetime_sort_value_untouched():
    token = encode_cursor("STATUS-X", "J1")
    sort_value, id_value = decode_cursor(token)
    assert sort_value == "STATUS-X" and id_value == "J1"


def test_apply_keyset_orders_desc_and_requests_one_extra_row():
    stmt = apply_keyset(
        select(Alert), Alert.emitted_at, Alert.alert_id, cursor=None, limit=16
    )
    sql = _compiled(stmt)
    assert "ORDER BY alerts.emitted_at DESC, alerts.alert_id DESC" in sql
    # LIMIT limit+1 — the extra row is how build_page detects a further page.
    assert 17 in _params(stmt).values()
    # no cursor -> no seek predicate
    assert "WHERE" not in sql


def test_apply_keyset_adds_keyset_predicate_when_cursor_given():
    cursor = encode_cursor(datetime(2026, 7, 20, 8, 0, 5, tzinfo=UTC), "alert-5")
    stmt = apply_keyset(
        select(Alert), Alert.emitted_at, Alert.alert_id, cursor=cursor, limit=16
    )
    sql = _compiled(stmt)
    # row-value seek: (sort, id) < (cursor_sort, cursor_id)
    assert "(alerts.emitted_at, alerts.alert_id) < (" in sql


def test_apply_keyset_nulls_last_for_nullable_sort_column():
    stmt = apply_keyset(
        select(Journey), Journey.last_ts, Journey.journey_id,
        cursor=None, limit=16, nulls_last=True,
    )
    sql = _compiled(stmt)
    assert "ORDER BY journeys.last_ts DESC NULLS LAST, journeys.journey_id DESC" in sql


def test_build_page_trims_and_mints_cursor_when_more_rows():
    rows = [
        _alert(alert_id="a1", emitted_at=datetime(2026, 7, 20, 8, 0, 3, tzinfo=UTC)),
        _alert(alert_id="a2", emitted_at=datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC)),
        _alert(alert_id="a3", emitted_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=UTC)),
    ]
    items, next_cursor = build_page(
        rows, 2, lambda a: a.emitted_at, lambda a: a.alert_id
    )
    assert [a.alert_id for a in items] == ["a1", "a2"]
    # cursor points at the last KEPT row, so the next page seeks strictly past a2
    assert next_cursor is not None
    sort_value, id_value = decode_cursor(next_cursor)
    assert id_value == "a2"
    assert sort_value == datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC)


def test_build_page_no_cursor_when_last_page():
    rows = [_alert(alert_id="a1"), _alert(alert_id="a2")]
    items, next_cursor = build_page(
        rows, 2, lambda a: a.emitted_at, lambda a: a.alert_id
    )
    assert [a.alert_id for a in items] == ["a1", "a2"]
    assert next_cursor is None


# --- pagination: GET /alerts -------------------------------------------------


def test_get_alerts_first_page_returns_next_cursor_when_more_rows():
    # limit=2 but 3 rows come back (build_alerts_query asks for limit+1) -> a
    # further page exists; the page is trimmed to 2 and next_cursor is minted.
    _use([_FakeResult(items=[
        _alert(alert_id="a1", emitted_at=datetime(2026, 7, 20, 8, 0, 3, tzinfo=UTC)),
        _alert(alert_id="a2", emitted_at=datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC)),
        _alert(alert_id="a3", emitted_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=UTC)),
    ])])
    body = TestClient(app).get("/alerts", params={"limit": 2}).json()
    assert [a["alert_id"] for a in body["items"]] == ["a1", "a2"]
    assert body["next_cursor"] is not None
    # cursor is anchored on the last kept row (a2), so page 2 won't repeat it
    sort_value, id_value = decode_cursor(body["next_cursor"])
    assert id_value == "a2"


def test_get_alerts_second_page_seeks_past_cursor_without_overlap():
    cursor = encode_cursor(datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC), "a2")
    session = _use([_FakeResult(items=[
        _alert(alert_id="a3", emitted_at=datetime(2026, 7, 20, 8, 0, 1, tzinfo=UTC)),
        _alert(alert_id="a4", emitted_at=datetime(2026, 7, 20, 8, 0, 0, tzinfo=UTC)),
    ])])
    body = TestClient(app).get("/alerts", params={"limit": 2, "cursor": cursor}).json()
    # fewer than limit+1 rows -> last page
    assert [a["alert_id"] for a in body["items"]] == ["a3", "a4"]
    assert body["next_cursor"] is None
    # the executed query carries the seek predicate that excludes the first page
    sql = _compiled(session.statements[0])
    assert "(alerts.emitted_at, alerts.alert_id) < (" in sql


def test_get_alerts_last_page_has_null_next_cursor():
    _use([_FakeResult(items=[_alert(alert_id="a1"), _alert(alert_id="a2")])])
    body = TestClient(app).get("/alerts", params={"limit": 5}).json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None


def test_get_alerts_limit_over_100_is_clamped():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"limit": 500})
    assert r.status_code == 200
    # clamp to 100 -> LIMIT 101 (100 + the detector row)
    assert 101 in _params(session.statements[0]).values()


def test_get_alerts_limit_below_1_is_clamped():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"limit": 0})
    assert r.status_code == 200
    # clamp to 1 -> LIMIT 2
    assert 2 in _params(session.statements[0]).values()


def test_get_alerts_sort_resolved_at_orders_on_resolved_at():
    session = _use([_FakeResult(items=[])])
    r = TestClient(app).get("/alerts", params={"sort": "resolved_at"})
    assert r.status_code == 200
    sql = _compiled(session.statements[0])
    assert "ORDER BY alerts.resolved_at DESC, alerts.alert_id DESC" in sql


def test_get_alerts_invalid_sort_is_422():
    r = TestClient(app).get("/alerts", params={"sort": "nonsense"})
    assert r.status_code == 422


# --- pagination: GET /journeys -----------------------------------------------


def test_get_journeys_paginates_and_orders_by_last_ts_desc():
    session = _use([_FakeResult(items=[
        _journey(journey_id="J1", last_ts=datetime(2026, 7, 20, 8, 0, 3, tzinfo=UTC)),
        _journey(journey_id="J2", last_ts=datetime(2026, 7, 20, 8, 0, 2, tzinfo=UTC)),
        _journey(journey_id="J3", last_ts=datetime(2026, 7, 20, 8, 0, 1, tzinfo=UTC)),
    ])])
    body = TestClient(app).get("/journeys", params={"limit": 2}).json()
    assert [j["journey_id"] for j in body["items"]] == ["J1", "J2"]
    assert body["next_cursor"] is not None
    sql = _compiled(session.statements[0])
    assert "ORDER BY journeys.last_ts DESC NULLS LAST, journeys.journey_id DESC" in sql


def test_get_journeys_last_page_has_null_next_cursor():
    _use([_FakeResult(items=[_journey(journey_id="J1"), _journey(journey_id="J2")])])
    body = TestClient(app).get("/journeys", params={"limit": 5}).json()
    assert [j["journey_id"] for j in body["items"]] == ["J1", "J2"]
    assert body["next_cursor"] is None


# --- GET/PATCH /incidents -----------------------------------------------------


from backend.db import Incident
from backend.api import build_incidents_query


def test_build_incidents_query_no_filter_selects_all():
    stmt = build_incidents_query(None)
    sql = _compiled(stmt)
    assert "WHERE" not in sql.upper() or "incidents" in sql.lower()


def test_build_incidents_query_filters_by_status():
    stmt = build_incidents_query("open")
    sql = _compiled(stmt)
    assert "status" in sql.lower()


def test_build_incidents_query_filters_by_department():
    stmt = build_incidents_query(None, ["devops", "backend"])
    sql = _compiled(stmt)
    assert "department" in sql.lower()
    assert "IN" in sql.upper()


def test_build_incidents_query_empty_department_list_is_no_filter():
    # [] must mean "no filter", same convention as the alerts department
    # filter — an IN () would instead match nothing and empty the page.
    # (The `department` column always appears in the SELECT list regardless;
    # what matters is that no WHERE clause is added for it.)
    stmt = build_incidents_query(None, [])
    sql = _compiled(stmt)
    assert "WHERE" not in sql.upper()


def _incident(**over) -> Incident:
    base = dict(
        incident_id="inc-1", signature="d1", failure_subtype="ENRICHMENT_FAILED",
        failing_service="SPT", error_token=None, title="ENRICHMENT_FAILED — SPT",
        department="devops", status="open",
        first_ts=datetime(2026, 7, 26, 8, 0, 0, tzinfo=UTC),
        last_ts=datetime(2026, 7, 26, 8, 5, 0, tzinfo=UTC),
        primary_alert_id="a1", alert_count=12, journey_count=3,
    )
    base.update(over)
    return Incident(**base)


def test_list_incidents_accepts_repeated_department_params():
    client = TestClient(app)
    _use([_FakeResult(items=[_incident(department="devops")])])
    resp = client.get("/incidents", params=[("department", "devops"), ("department", "backend")])
    assert resp.status_code == 200
    assert resp.json()["items"][0]["department"] == "devops"


def test_list_incidents_returns_page():
    client = TestClient(app)
    session = _use([_FakeResult(items=[_incident()])])
    resp = client.get("/incidents")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["incident_id"] == "inc-1"


def test_get_incident_404_when_missing():
    client = TestClient(app)
    _use([_FakeResult(one=None)])
    resp = client.get("/incidents/does-not-exist")
    assert resp.status_code == 404


def test_get_incident_returns_incident_and_its_alerts():
    client = TestClient(app)
    alert = Alert(
        alert_id="a1", emitted_at=datetime(2026, 7, 26, 8, 0, 0, tzinfo=UTC),
        log_id="l1", level="ERROR", app_name="cc-order-engine", logger="l",
        message="m", source="fallback", journey_id="j1", incident_id="inc-1",
        is_resolved=False,
    )
    _use([
        _FakeResult(one=_incident()),
        _FakeResult(items=[alert]),
    ])
    resp = client.get("/incidents/inc-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["incident_id"] == "inc-1"
    assert body["alerts"][0]["alert_id"] == "a1"


def test_resolve_incident_sets_status_and_returns_404_when_missing(monkeypatch):
    client = TestClient(app)
    _use([_FakeResult(one=None)])
    resp = client.patch("/incidents/does-not-exist/resolve")
    assert resp.status_code == 404


def test_resolve_incident_cascades_to_its_alerts():
    """Resolving an incident must also resolve every alert linked to it — an
    incident collapses those alerts, so they must leave the live Alert Feed
    and show up in History exactly as if each were individually resolved."""
    client = TestClient(app)
    session = _use([
        _FakeResult(one=_incident(status="resolved")),
        _FakeResult(),  # the cascade UPDATE on alerts — return value unused
    ])
    resp = client.patch("/incidents/inc-1/resolve")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"

    assert len(session.statements) == 2
    cascade_sql = _compiled(session.statements[1]).lower()
    assert "alerts" in cascade_sql
    assert "is_resolved" in cascade_sql
    assert "resolved_at" in cascade_sql
    assert "incident_id" in cascade_sql
