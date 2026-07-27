"""Tests for the insights aggregation (backend/stats.py + GET /stats/insights).

Three layers, no database and no broker:

* **query builders** — asserted by compiling to SQL (the same compiled-SQL style
  as ``build_alerts_query`` in test_api.py); they must group by the right column
  of the right table and execute nothing.
* :func:`~backend.stats.assemble_overview` — pure, fed hand-written
  ``(value, count)`` rows: counters, the success rate (including the zero
  denominator), the explicit null buckets, and the "buckets sum to the total"
  invariant.
* the route — driven through ``dependency_overrides`` with a fake session that
  returns seeded group-by rows in execution order, asserting the response is a
  well-formed ``OverviewStats``.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from backend import stats
from backend.auth import get_current_user
from backend.db import get_session
from backend.main import app
from backend.schemas import OverviewStats


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --- query builders (pure; asserted via compiled SQL) ------------------------


def test_journeys_by_status_groups_by_status():
    sql = _compiled(stats.journeys_by_status())
    assert "count(*)" in sql
    assert "FROM journeys" in sql
    assert "GROUP BY journeys.status" in sql


def test_journeys_by_outcome_groups_by_outcome():
    sql = _compiled(stats.journeys_by_outcome())
    assert "count(*)" in sql
    assert "FROM journeys" in sql
    assert "GROUP BY journeys.outcome" in sql


@pytest.mark.parametrize("column", ["department", "severity", "level", "source"])
def test_alerts_by_groups_by_the_given_column(column):
    """One generic grouper serves all four alert breakdowns."""
    sql = _compiled(stats.alerts_by(column))
    assert "count(*)" in sql
    assert "FROM alerts" in sql
    assert f"GROUP BY alerts.{column}" in sql
    assert f"SELECT alerts.{column}" in sql


def test_alerts_by_rejects_an_unknown_column():
    """The column name is looked up in a whitelist, never interpolated into SQL."""
    with pytest.raises(KeyError):
        stats.alerts_by("message; DROP TABLE alerts")


def test_alerts_resolution_counts_groups_by_is_resolved():
    sql = _compiled(stats.alerts_resolution_counts())
    assert "FROM alerts" in sql
    assert "GROUP BY alerts.is_resolved" in sql


def test_journey_totals_counts_and_averages_terminal_durations():
    stmt = stats.journey_totals()
    sql = _compiled(stmt)
    assert "count(*)" in sql
    assert "avg(EXTRACT(epoch FROM journeys.last_ts - journeys.first_ts))" in sql
    # The average is scoped with an aggregate FILTER so the total count stays
    # over ALL journeys while the duration only sees finished ones with both
    # timestamps.
    assert "FILTER (WHERE" in sql
    assert "journeys.first_ts IS NOT NULL" in sql
    assert "journeys.last_ts IS NOT NULL" in sql
    assert "GROUP BY" not in sql  # single-row aggregate
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert list(params.values()) == [list(stats.TERMINAL_STATUSES)]


def test_journey_totals_terminal_statuses_exclude_in_progress():
    assert set(stats.TERMINAL_STATUSES) == {"SUCCESS", "FAILED", "TIMED_OUT"}


def test_alert_total_counts_alerts():
    sql = _compiled(stats.alert_total())
    assert "count(*)" in sql
    assert "FROM alerts" in sql
    assert "GROUP BY" not in sql


# --- assembler (pure; fed hand-written group-by rows) -----------------------


def _assemble(**over) -> OverviewStats:
    base = dict(
        journeys_by_status=[("SUCCESS", 3), ("FAILED", 1), ("TIMED_OUT", 1)],
        journeys_by_outcome=[("SUCCESS", 3), ("MARGIN_CHECK_FAILED", 1)],
        journey_total=5,
        journey_avg_duration=4.5,
        alerts_by_department=[("backend", 2), ("devops", 1)],
        alerts_by_severity=[("critical", 1), ("high", 2)],
        alerts_by_level=[("ERROR", 2), ("WARN", 1)],
        alerts_by_source=[("ai", 2), ("fallback", 1)],
        alerts_resolution=[(True, 1), (False, 2)],
        alert_total=3,
    )
    base.update(over)
    return stats.assemble_overview(**base)


def test_assemble_overview_folds_the_counts():
    out = _assemble()
    assert out.journeys.total == 5
    assert out.journeys.by_status == {"SUCCESS": 3, "FAILED": 1, "TIMED_OUT": 1}
    assert out.journeys.by_outcome == {"SUCCESS": 3, "MARGIN_CHECK_FAILED": 1}
    assert out.journeys.avg_duration_seconds == 4.5
    assert out.alerts.total == 3
    assert out.alerts.by_department == {"backend": 2, "devops": 1}
    assert out.alerts.by_severity == {"critical": 1, "high": 2}
    assert out.alerts.by_level == {"ERROR": 2, "WARN": 1}
    assert out.alerts.by_source == {"ai": 2, "fallback": 1}


def test_assemble_overview_success_rate_over_finished_journeys():
    """3 SUCCESS out of 5 finished = 0.6."""
    out = _assemble()
    assert out.journeys.success_rate == pytest.approx(0.6)


def test_assemble_overview_success_rate_ignores_in_progress():
    """IN_PROGRESS journeys are not in the denominator — they haven't failed yet."""
    out = _assemble(
        journeys_by_status=[("SUCCESS", 1), ("FAILED", 1), ("IN_PROGRESS", 98)],
        journey_total=100,
    )
    assert out.journeys.success_rate == pytest.approx(0.5)


def test_assemble_overview_success_rate_is_zero_when_no_journey_finished():
    """Zero denominator → 0.0, never a ZeroDivisionError."""
    out = _assemble(
        journeys_by_status=[("IN_PROGRESS", 4)],
        journeys_by_outcome=[(None, 4)],
        journey_total=4,
        journey_avg_duration=None,
    )
    assert out.journeys.success_rate == 0.0
    assert out.journeys.avg_duration_seconds is None


def test_assemble_overview_success_rate_is_zero_on_a_completely_empty_db():
    out = _assemble(
        journeys_by_status=[],
        journeys_by_outcome=[],
        journey_total=0,
        journey_avg_duration=None,
        alerts_by_department=[],
        alerts_by_severity=[],
        alerts_by_level=[],
        alerts_by_source=[],
        alerts_resolution=[],
        alert_total=0,
    )
    assert out.journeys.success_rate == 0.0
    assert out.journeys.by_status == {}
    assert out.alerts.open == 0 and out.alerts.resolved == 0


def test_assemble_overview_buckets_null_department_and_severity():
    """Fallback alerts have no department/severity — they get explicit buckets
    rather than being dropped (a dropped null would make the breakdown lie)."""
    out = _assemble(
        alerts_by_department=[("backend", 2), (None, 1)],
        alerts_by_severity=[("critical", 1), (None, 2)],
    )
    assert out.alerts.by_department == {"backend": 2, "unassigned": 1}
    assert out.alerts.by_severity == {"critical": 1, "unrated": 2}


def test_assemble_overview_buckets_null_outcome():
    """An in-progress journey has no outcome yet."""
    out = _assemble(
        journeys_by_outcome=[("SUCCESS", 3), (None, 2)],
        journey_total=5,
    )
    assert out.journeys.by_outcome == {"SUCCESS": 3, "none": 2}


def test_assemble_overview_buckets_sum_to_their_totals():
    """The invariant the null buckets exist for: nothing is silently dropped."""
    out = _assemble(
        journeys_by_status=[("SUCCESS", 3), ("FAILED", 1), ("IN_PROGRESS", 2)],
        journeys_by_outcome=[("SUCCESS", 3), ("SAP_SUBMISSION_FAILED", 1), (None, 2)],
        journey_total=6,
        alerts_by_department=[("backend", 2), (None, 1)],
        alerts_by_severity=[("critical", 1), (None, 2)],
        alerts_by_level=[("ERROR", 2), ("WARN", 1)],
        alerts_by_source=[("ai", 2), ("fallback", 1)],
        alerts_resolution=[(True, 1), (False, 2)],
        alert_total=3,
    )
    assert sum(out.journeys.by_status.values()) == out.journeys.total
    assert sum(out.journeys.by_outcome.values()) == out.journeys.total
    for bucket in (
        out.alerts.by_department,
        out.alerts.by_severity,
        out.alerts.by_level,
        out.alerts.by_source,
    ):
        assert sum(bucket.values()) == out.alerts.total
    assert out.alerts.open + out.alerts.resolved == out.alerts.total


def test_assemble_overview_splits_open_and_resolved():
    out = _assemble(alerts_resolution=[(True, 4), (False, 6)], alert_total=10)
    assert (out.alerts.resolved, out.alerts.open) == (4, 6)


def test_assemble_overview_handles_a_single_sided_resolution_group():
    """GROUP BY only yields rows that exist — no resolved alerts, no True row."""
    out = _assemble(alerts_resolution=[(False, 3)], alert_total=3)
    assert (out.alerts.resolved, out.alerts.open) == (0, 3)


def test_assemble_overview_coerces_a_decimal_average_to_float():
    """Postgres ``avg()`` comes back as a Decimal; the wire contract is float."""
    out = _assemble(journey_avg_duration=Decimal("4.250"))
    assert isinstance(out.journeys.avg_duration_seconds, float)
    assert out.journeys.avg_duration_seconds == pytest.approx(4.25)


# --- the route ---------------------------------------------------------------


class _FakeResult:
    """One execute() result; serves the shape the route asks it for."""

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def one(self):
        (row,) = self._rows
        return row

    def scalar_one(self):
        (row,) = self._rows
        return row[0]


class _FakeSession:
    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0)


@pytest.fixture(autouse=True)
def _authenticated():
    """/stats/insights sits on the auth-guarded router; these tests assert the
    aggregation contract, so the dependency is satisfied with a stub user.
    Enforcement itself is asserted in test_overview_requires_auth."""
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    yield
    app.dependency_overrides.clear()


def _use(results) -> _FakeSession:
    session = _FakeSession(results)

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    return session


# Seeded results in the order backend.api.get_overview_stats executes them.
def _seed(**over):
    base = dict(
        journey_totals=[(5, 4.5)],
        by_status=[("SUCCESS", 3), ("FAILED", 1), ("TIMED_OUT", 1)],
        by_outcome=[("SUCCESS", 3), ("MARGIN_CHECK_FAILED", 1), (None, 1)],
        alert_total=[(3,)],
        resolution=[(True, 1), (False, 2)],
        by_department=[("backend", 2), (None, 1)],
        by_severity=[("critical", 1), (None, 2)],
        by_level=[("ERROR", 2), ("WARN", 1)],
        by_source=[("ai", 2), ("fallback", 1)],
    )
    base.update(over)
    return [
        _FakeResult(base[key])
        for key in (
            "journey_totals",
            "by_status",
            "by_outcome",
            "alert_total",
            "resolution",
            "by_department",
            "by_severity",
            "by_level",
            "by_source",
        )
    ]


def test_overview_requires_auth():
    app.dependency_overrides.clear()  # drop the autouse stub user for this test
    assert TestClient(app).get("/stats/insights").status_code == 401


def test_overview_returns_the_assembled_stats():
    _use(_seed())
    r = TestClient(app).get("/stats/insights")
    assert r.status_code == 200
    body = r.json()

    # The payload validates as OverviewStats (shape contract), and the numbers
    # are the seeded rows folded by assemble_overview.
    parsed = OverviewStats.model_validate(body)
    assert parsed.journeys.total == 5
    assert parsed.journeys.by_status == {"SUCCESS": 3, "FAILED": 1, "TIMED_OUT": 1}
    assert parsed.journeys.by_outcome == {
        "SUCCESS": 3,
        "MARGIN_CHECK_FAILED": 1,
        "none": 1,
    }
    assert parsed.journeys.success_rate == pytest.approx(0.6)
    assert parsed.journeys.avg_duration_seconds == pytest.approx(4.5)
    assert parsed.alerts.total == 3
    assert (parsed.alerts.open, parsed.alerts.resolved) == (2, 1)
    assert parsed.alerts.by_department == {"backend": 2, "unassigned": 1}
    assert parsed.alerts.by_severity == {"critical": 1, "unrated": 2}
    assert parsed.alerts.by_level == {"ERROR": 2, "WARN": 1}
    assert parsed.alerts.by_source == {"ai": 2, "fallback": 1}


def test_overview_response_keys_are_exactly_the_schema():
    _use(_seed())
    body = TestClient(app).get("/stats/insights").json()
    assert set(body) == {"journeys", "alerts"}
    assert set(body["journeys"]) == {
        "total",
        "by_status",
        "by_outcome",
        "success_rate",
        "avg_duration_seconds",
    }
    assert set(body["alerts"]) == {
        "total",
        "open",
        "resolved",
        "by_department",
        "by_severity",
        "by_level",
        "by_source",
    }


def test_overview_runs_the_stats_builders():
    """The route executes the builders from backend/stats.py — nine grouped /
    aggregate statements, no ad-hoc SQL of its own."""
    session = _use(_seed())
    TestClient(app).get("/stats/insights")
    sql = [_compiled(s) for s in session.statements]
    assert len(sql) == 9
    assert sql[0] == _compiled(stats.journey_totals())
    assert sql[1] == _compiled(stats.journeys_by_status())
    assert sql[2] == _compiled(stats.journeys_by_outcome())
    assert sql[3] == _compiled(stats.alert_total())
    assert sql[4] == _compiled(stats.alerts_resolution_counts())
    assert sql[5:] == [
        _compiled(stats.alerts_by(c))
        for c in ("department", "severity", "level", "source")
    ]


def test_overview_on_an_empty_database():
    """Nothing ingested yet: zeros, empty buckets, null average — not a 500."""
    _use(
        _seed(
            journey_totals=[(0, None)],
            by_status=[],
            by_outcome=[],
            alert_total=[(0,)],
            resolution=[],
            by_department=[],
            by_severity=[],
            by_level=[],
            by_source=[],
        )
    )
    r = TestClient(app).get("/stats/insights")
    assert r.status_code == 200
    body = r.json()
    assert body["journeys"] == {
        "total": 0,
        "by_status": {},
        "by_outcome": {},
        "success_rate": 0.0,
        "avg_duration_seconds": None,
    }
    assert body["alerts"]["total"] == 0
    assert body["alerts"]["open"] == 0
    assert body["alerts"]["resolved"] == 0
