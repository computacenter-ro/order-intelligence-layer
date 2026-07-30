"""[5] Core Backend — insights aggregation (GET /stats/insights).

Two clearly separated layers, both pure and unit-testable in the style of
``build_alerts_query`` (compiled-SQL for the builders, plain-data for the
assembler):

* **Query builders** — each returns a ``Select`` with a ``GROUP BY`` (or a
  single-row aggregate); they build SQL and execute nothing. ``alerts_by`` is
  generic over the grouping column so the four alert breakdowns share one code
  path.
* :func:`assemble_overview` — takes the *executed* results (lists of
  ``(value, count)`` tuples plus the scalar totals) and folds them into the
  :class:`OverviewStats` response. All the real logic lives here: the success
  rate and the alerts-per-incident ratio (both guarded against a zero
  denominator) and the explicit null buckets so every breakdown sums back to its
  total.

Importing this module performs no I/O.
"""

from __future__ import annotations

from typing import Iterable

from sqlalchemy import Select, extract, func, select

from backend.db import Alert, Incident, Journey
from backend.schemas import AlertStats, IncidentStats, JourneyStats, OverviewStats

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


def alerts_cache_counts() -> Select:
    """``(cached, count)`` — fresh (False) vs semantic-cache-served (True) alerts.

    Not routed through :func:`alerts_by`: ``cached`` is a boolean, and the generic
    grouper stringifies its values, which would put the Python-capitalised keys
    ``"True"`` / ``"False"`` into the public JSON. Folded into two named scalars
    by the assembler instead, exactly like :func:`alerts_resolution_counts`.
    """
    return select(Alert.cached, func.count()).group_by(Alert.cached)


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


def incidents_by_status() -> Select:
    """``(status, count)`` grouped by ``Incident.status``.

    ``status`` is a free string on the model ("open" / "resolved" today, with no
    enum and no CHECK constraint), so the fold keeps it open-ended exactly like
    the department/outcome breakdowns — a third status must not need a schema
    change here.
    """
    return select(Incident.status, func.count()).group_by(Incident.status)


def incident_total() -> Select:
    """Single row: ``(total_incidents,)``."""
    return select(func.count()).select_from(Incident)


def alerts_clustered_count() -> Select:
    """Single row: ``(alerts_assigned_to_an_incident,)``.

    Deliberately a ``COUNT`` over ``alerts.incident_id`` (which is indexed) and
    NOT ``SUM(incidents.alert_count)``. That denormalized column is bumped once,
    when a journey is clustered, and an alert that lands *after* its journey was
    clustered is never back-filled into it — so summing it would undercount. The
    join side is the authoritative answer.
    """
    return select(func.count()).select_from(Alert).where(Alert.incident_id.isnot(None))


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
    alerts_cache: Iterable[tuple],
    alert_total: int,
    incidents_by_status: Iterable[tuple],
    incident_total: int,
    alerts_clustered: int,
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

    # How many AI answers the semantic cache served instead of calling the LLM.
    # ``cached`` is NOT NULL on the model, so unlike department/severity there is
    # no null bucket to account for and the two sides always sum to the total.
    cached = 0
    fresh = 0
    for is_cached, count in alerts_cache:
        if is_cached:
            cached += count
        else:
            fresh += count

    # How much noise the clustering engine absorbed. Only FAILED / TIMED_OUT
    # journeys get clustered, so the alerts of a successful order (benign WARNs)
    # never carry an incident_id — the denominator is the CLUSTERED alerts, not
    # every alert. ``alerts_unclustered`` is reported alongside so the two still
    # sum to the alert total, the same discipline as the null buckets above; it
    # is also what stops a reader turning "520 alerts, 37 incidents" into a
    # 14x claim the data does not support.
    #
    # max(0, ...) because the two counts come from separate statements: under
    # READ COMMITTED, alerts inserted between them could make the later
    # clustered count exceed the earlier total. A negative count would be a
    # visible lie; clamping is the honest read of a torn snapshot.
    alerts_unclustered = max(0, alert_total - alerts_clustered)
    # None, not 0.0, when there are no incidents: "nothing has been clustered
    # yet" and "clustering achieved no compression" are different statements and
    # the UI renders them differently.
    alerts_per_incident = (alerts_clustered / incident_total) if incident_total else None

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
            cached=cached,
            fresh=fresh,
            by_department=_grouped(alerts_by_department, null_key="unassigned"),
            by_severity=_grouped(alerts_by_severity, null_key="unrated"),
            by_level=_grouped(alerts_by_level, null_key="unknown"),
            by_source=_grouped(alerts_by_source, null_key="unknown"),
        ),
        incidents=IncidentStats(
            total=incident_total,
            by_status=_grouped(incidents_by_status, null_key="unknown"),
            alerts_clustered=alerts_clustered,
            alerts_unclustered=alerts_unclustered,
            alerts_per_incident=alerts_per_incident,
        ),
    )
