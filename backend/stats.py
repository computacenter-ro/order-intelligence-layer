"""[5] Core Backend — insights aggregation (GET /stats/insights).

Two clearly separated layers, both pure and unit-testable in the style of
``build_alerts_query`` (compiled-SQL for the builders, plain-data for the
assembler):

* **Query builders** — each returns a ``Select`` with a ``GROUP BY`` (or a
  single-row aggregate); they build SQL and execute nothing. ``alerts_by`` is
  generic over the grouping column so the four alert breakdowns share one code
  path.
* :func:`assemble_overview` — takes the *executed* results (lists of
  ``(value, count)`` tuples plus the two scalar totals) and folds them into the
  :class:`OverviewStats` response. All the real logic lives here: the success
  rate (guarded against a zero denominator) and the explicit null buckets so
  every breakdown sums back to its total.

Importing this module performs no I/O.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import Select, extract, func, select

from backend.db import Alert, Journey
from backend.schemas import AlertStats, JourneyStats, OverviewStats

# Journey statuses that represent a finished journey — the only ones with a
# meaningful duration (first_ts → last_ts). IN_PROGRESS is excluded.
TERMINAL_STATUSES = ("SUCCESS", "FAILED", "TIMED_OUT")

# Alert columns exposed to the generic alerts_by() grouper.
_ALERT_GROUP_COLUMNS = {
    "department": Alert.department,
    "severity": Alert.severity,
    "level": Alert.level,
    "source": Alert.source,
}


# --- query builders (pure; execute nothing) ----------------------------------


def journeys_by_status() -> Select:
    """``(status, count)`` grouped by ``Journey.status``."""
    return select(Journey.status, func.count()).group_by(Journey.status)


def journeys_by_outcome() -> Select:
    """``(outcome, count)`` grouped by ``Journey.outcome`` (outcome may be null)."""
    return select(Journey.outcome, func.count()).group_by(Journey.outcome)


def alerts_by(column: str) -> Select:
    """``(value, count)`` grouped by one alert column (department/severity/level/source)."""
    col = _ALERT_GROUP_COLUMNS[column]
    return select(col, func.count()).group_by(col)


def alerts_resolution_counts() -> Select:
    """``(is_resolved, count)`` — open (False) vs resolved (True) alert counts."""
    return select(Alert.is_resolved, func.count()).group_by(Alert.is_resolved)


def journey_totals() -> Select:
    """Single row: ``(total_journeys, avg_duration_seconds)``.

    ``total`` counts every journey; the average duration is scoped with an
    aggregate ``FILTER`` to only terminal journeys that have both timestamps, so
    in-progress / half-assembled journeys never skew it (and it is ``NULL`` when
    none qualify).
    """
    return select(
        func.count(),
        func.avg(extract("epoch", Journey.last_ts - Journey.first_ts)).filter(
            Journey.first_ts.isnot(None),
            Journey.last_ts.isnot(None),
            Journey.status.in_(TERMINAL_STATUSES),
        ),
    )


def alert_total() -> Select:
    """Single row: ``(total_alerts,)``."""
    return select(func.count()).select_from(Alert)


# --- assembler (pure; operates on executed results) --------------------------


def _grouped(rows: Iterable[tuple], *, null_key: str) -> dict[str, int]:
    """Fold ``(value, count)`` rows into a ``{str: int}`` dict.

    A null ``value`` lands in ``null_key`` (dict keys must be strings and the
    bucket must still sum to the total). Repeated keys accumulate defensively.
    """
    out: dict[str, int] = {}
    for value, count in rows:
        key = null_key if value is None else str(value)
        out[key] = out.get(key, 0) + count
    return out


def assemble_overview(
    *,
    journeys_by_status: Iterable[tuple],
    journeys_by_outcome: Iterable[tuple],
    journey_total: int,
    journey_avg_duration: float | None,
    alerts_by_department: Iterable[tuple],
    alerts_by_severity: Iterable[tuple],
    alerts_by_level: Iterable[tuple],
    alerts_by_source: Iterable[tuple],
    alerts_resolution: Iterable[tuple],
    alert_total: int,
) -> OverviewStats:
    """Fold executed group-by results into the :class:`OverviewStats` response."""
    by_status = _grouped(journeys_by_status, null_key="unknown")
    by_outcome = _grouped(journeys_by_outcome, null_key="none")

    # success_rate over the finished journeys only; 0.0 when none have finished
    # (avoids a divide-by-zero and reads as "no successes yet").
    finished = sum(by_status.get(s, 0) for s in TERMINAL_STATUSES)
    success = by_status.get("SUCCESS", 0)
    success_rate = success / finished if finished else 0.0

    resolved = 0
    open_ = 0
    for is_resolved, count in alerts_resolution:
        if is_resolved:
            resolved += count
        else:
            open_ += count

    return OverviewStats(
        journeys=JourneyStats(
            total=journey_total,
            by_status=by_status,
            by_outcome=by_outcome,
            success_rate=success_rate,
            avg_duration_seconds=(
                float(journey_avg_duration) if journey_avg_duration is not None else None
            ),
        ),
        alerts=AlertStats(
            total=alert_total,
            open=open_,
            resolved=resolved,
            by_department=_grouped(alerts_by_department, null_key="unassigned"),
            by_severity=_grouped(alerts_by_severity, null_key="unrated"),
            by_level=_grouped(alerts_by_level, null_key="unknown"),
            by_source=_grouped(alerts_by_source, null_key="unknown"),
        ),
    )
