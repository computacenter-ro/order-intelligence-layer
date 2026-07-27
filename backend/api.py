"""[5] Core Backend — REST API (CLAUDE.md [5] "API").

Async FastAPI routes over the three tables in ``backend/db.py`` (``Alert``,
``Journey``, ``JourneyEvent``), using the ``get_session`` dependency. Mostly
read-only — journeys/alerts are produced by the consumers — except for the one
manual-triage write below.

Responses are Pydantic schemas (``AlertOut`` / ``JourneyOut`` /
``JourneyDetailOut``, defined in ``backend/schemas.py``), never the ORM models,
so the wire contract is explicit and decoupled from the DB layer.

Endpoints:

* ``GET /alerts?since=&department=&source=&level=&app_name=&severity=`` — alerts
  filtered by ``emitted_at >= since`` / ``department`` / ``source`` / ``level`` /
  ``app_name`` / ``severity``, newest first.

  ``department``, ``app_name`` and ``severity`` are **multi-valued**: repeat the
  param (``?department=backend&department=devops``) to OR within that category
  (SQL ``IN``). Omitting a param means no filter on it. ``department`` values
  must each be one of the five ``Department`` values and ``severity`` one of
  ``critical``/``high``/``medium``/``low``; any other value is a 422 (validated at
  the route, so ``build_alerts_query`` stays pure). ``app_name`` is a free string
  (the set of services can grow) — an unknown value simply matches nothing.

  ``source`` (``ai``/``fallback``) and ``level`` (``WARN``/``ERROR``) stay
  single-valued, likewise 422 on anything else. ``cached=true|false`` narrows by
  semantic-cache provenance; it is orthogonal to ``source`` (every cached alert is
  ``source="ai"``), so it filters *within* AI answers rather than beside them.
* ``GET /alerts/facets`` — takes the same filter params as ``GET /alerts`` (no
  paging) and returns per-value counts for the three multi-select filters. Each
  facet is counted with every active filter EXCEPT its own, so ticking one value
  never collapses that facet's own list. Filter conditions come from the shared
  :func:`alert_filter_conditions`, so the feed and its counts cannot disagree.
* ``GET /journeys?status=`` — journeys filtered by ``status``.
* ``GET /journeys/{journey_id}`` — one journey + its events (ordered by ``ts``)
  + summary; 404 if the journey does not exist.
* ``GET /stats/insights`` — aggregate counters for the dashboard insights page.
  Query building and the folding of the group-by results both live in
  ``backend/stats.py`` (pure + unit-tested); this route only executes them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user
from backend.db import Alert, Journey, JourneyEvent, get_session
from backend.pagination import apply_keyset, build_page
from backend.schemas import (
    AlertFacets,
    AlertOut,
    JourneyDetailOut,
    JourneyEventOut,
    JourneyOut,
    OverviewStats,
    Page,
)
from backend import stats
from shared.models import Department


# --- query builders (pure, unit-testable) ------------------------------------


# Alert columns exposed as facets, in the order GET /alerts/facets computes them.
# Only the three multi-select filters get facets — a count is only useful where
# the user picks from a list of values.
_FACET_COLUMNS = {
    "severity": Alert.severity,
    "department": Alert.department,
    "app_name": Alert.app_name,
}


def alert_filter_conditions(
    since: datetime | None,
    department: list[str] | None,
    source: str | None,
    level: str | None = None,
    app_name: list[str] | None = None,
    severity: list[str] | None = None,
    resolved: bool | None = None,
    cached: bool | None = None,
) -> list[ColumnElement[bool]]:
    """The WHERE clauses for a set of alert filters — the one place they live.

    Returned as a list rather than applied to a statement so both consumers can
    use them: :func:`build_alerts_query` applies all of them, while
    ``GET /alerts/facets`` deliberately omits one filter per facet (see
    :func:`build_alert_facet_query`). Any new filter added here is picked up by
    both without further wiring.

    ``department`` / ``app_name`` / ``severity`` are **multi-valued**: a non-empty
    list becomes an ``IN (...)`` (OR within the category), while ``None`` *and*
    an empty list both mean "no filter". Collapsing empty to no-filter is
    deliberate — the UI's "nothing ticked" state is "show everything", and an
    ``IN ()`` would instead match nothing and render an empty feed.

    Note a non-empty list excludes NULLs, exactly as SQL ``IN`` does: filtering
    by any department therefore drops ``source="fallback"`` alerts, which have no
    department. That is the same behaviour the single-value ``==`` had.

    ``level`` / ``source`` / ``resolved`` / ``cached`` stay single-valued.
    """
    conditions: list[ColumnElement[bool]] = []
    if since is not None:
        conditions.append(Alert.emitted_at >= since)
    if department:
        conditions.append(Alert.department.in_(department))
    if source is not None:
        conditions.append(Alert.source == source)
    if resolved is not None:
        conditions.append(Alert.is_resolved == resolved)
    if level is not None:
        conditions.append(Alert.level == level)
    if app_name:
        conditions.append(Alert.app_name.in_(app_name))
    if severity:
        conditions.append(Alert.severity.in_(severity))
    if cached is not None:
        conditions.append(Alert.cached == cached)
    return conditions


def build_alerts_query(
    since: datetime | None,
    department: list[str] | None,
    source: str | None,
    level: str | None = None,
    app_name: list[str] | None = None,
    severity: list[str] | None = None,
    resolved: bool | None = None,
    cached: bool | None = None,
) -> Select:
    """Select alerts matching the given filters (see :func:`alert_filter_conditions`).

    Filters only — ordering is applied by the paginator (:func:`apply_keyset`),
    which owns the sort column so callers can page by ``emitted_at`` or
    ``resolved_at`` over the same filter set.
    """
    return select(Alert).where(
        *alert_filter_conditions(
            since,
            department,
            source,
            level=level,
            app_name=app_name,
            severity=severity,
            resolved=resolved,
            cached=cached,
        )
    )


def build_alert_facet_query(
    column: str, conditions: list[ColumnElement[bool]]
) -> Select:
    """``(value, count)`` for one facet column, under the given conditions.

    The caller is responsible for the **exclude-self** rule: the conditions passed
    in must be every active filter EXCEPT this column's own. That is what makes a
    facet list usable — with ``department=backend`` ticked, the department counts
    still show every department (so the user can see what else is available and
    widen the selection), while the severity counts do narrow to backend alerts.
    Including a facet's own filter would collapse it to the one ticked value with
    every other option showing 0.

    NULLs are excluded: a null severity/department means "the LLM never rated or
    routed this" (a ``source="fallback"`` alert), and there is no such option in
    the filter list to attach a count to. ``app_name`` is non-nullable, so the
    predicate is a no-op there — kept uniform rather than special-cased.
    """
    col = _FACET_COLUMNS[column]
    return (
        select(col, func.count())
        .where(*conditions)
        .where(col.is_not(None))
        .group_by(col)
    )


def build_journeys_query(status: str | None) -> Select:
    """Select journeys, optionally filtered by ``status``."""
    stmt = select(Journey)
    if status is not None:
        stmt = stmt.where(Journey.status == status)
    return stmt


# --- routes ------------------------------------------------------------------

# Router-level auth: every read route requires a valid session (get_current_user
# raises 401 otherwise). Declared once here so no endpoint can be added
# unguarded by accident. The scripts/injector paths don't hit this API, so dev
# flow replay is unaffected.
router = APIRouter(dependencies=[Depends(get_current_user)])


# Sort keys accepted by GET /alerts, mapped to the ORM column the paginator
# orders/seeks on. emitted_at = the live feed's newest-first; resolved_at =
# History's most-recently-resolved-first.
_ALERT_SORT_COLUMNS = {
    "emitted_at": Alert.emitted_at,
    "resolved_at": Alert.resolved_at,
}


@router.get("/alerts", response_model=Page[AlertOut])
async def list_alerts(
    since: Annotated[datetime | None, Query()] = None,
    # department / app_name / severity are MULTI-valued: repeat the param
    # (?department=backend&department=devops) for an OR within the category.
    # Omitting it entirely means no filter.
    #
    # Typing these as the Department enum / a Literal makes FastAPI reject
    # out-of-domain values with a 422 (listing the allowed options) before the
    # query runs — per element, so one bad value in a list is still a 422.
    # build_alerts_query stays pure and str-typed; we pass validated values through.
    department: Annotated[list[Department] | None, Query()] = None,
    source: Annotated[Literal["ai", "fallback"] | None, Query()] = None,
    resolved: Annotated[bool | None, Query()] = None,
    # Alerts are only ever WARN/ERROR, so the Literal rejects anything else with
    # a 422. app_name stays a free str — the set of services can grow, so an
    # unknown value should just match nothing, not be a validation error.
    level: Annotated[Literal["WARN", "ERROR"] | None, Query()] = None,
    app_name: Annotated[list[str] | None, Query()] = None,
    # Router LLM severity (critical/high/medium/low); Literal → 422 on anything
    # else. Null for fallback / not-yet-rated alerts, so a non-empty severity
    # filter excludes those — exactly as SQL ``IN`` does.
    severity: Annotated[
        list[Literal["critical", "high", "medium", "low"]] | None, Query()
    ] = None,
    # Semantic-cache provenance. Orthogonal to ``source``: cached alerts are all
    # source="ai", so ?cached=true narrows within AI answers rather than being an
    # alternative to them. Omitted = both.
    cached: Annotated[bool | None, Query()] = None,
    # Cursor pagination (see backend/pagination.py). limit is clamped to 1..100
    # rather than 422'd so a caller can pass anything and still get a sane page.
    limit: Annotated[int, Query()] = 16,
    cursor: Annotated[str | None, Query()] = None,
    sort: Annotated[Literal["emitted_at", "resolved_at"], Query()] = "emitted_at",
    session: AsyncSession = Depends(get_session),
) -> Page[AlertOut]:
    limit = max(1, min(limit, 100))
    sort_col = _ALERT_SORT_COLUMNS[sort]
    stmt = apply_keyset(
        build_alerts_query(
            since,
            # Enum members -> their str values; None stays None so the builder's
            # "no filter" branch is reached (an empty list would too, but keeping
            # None distinct means the query reads exactly as the caller asked).
            [d.value for d in department] if department else None,
            source,
            level=level,
            app_name=app_name,
            severity=list(severity) if severity else None,
            resolved=resolved,
            cached=cached,
        ),
        sort_col,
        Alert.alert_id,
        cursor=cursor,
        limit=limit,
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    items, next_cursor = build_page(
        rows, limit, lambda a: getattr(a, sort), lambda a: a.alert_id
    )
    return Page[AlertOut](
        items=[AlertOut.model_validate(a) for a in items],
        next_cursor=next_cursor,
    )


@router.get("/alerts/facets", response_model=AlertFacets)
async def get_alert_facets(
    # Exactly the filter params GET /alerts takes, with the same types and so the
    # same 422s — minus limit/cursor/sort, since facets aggregate rather than page.
    # A test (test_facets_accepts_the_same_filter_params_as_alerts) fails if the
    # two lists drift apart, so a filter added to /alerts cannot be silently
    # ignored here.
    since: Annotated[datetime | None, Query()] = None,
    department: Annotated[list[Department] | None, Query()] = None,
    source: Annotated[Literal["ai", "fallback"] | None, Query()] = None,
    resolved: Annotated[bool | None, Query()] = None,
    level: Annotated[Literal["WARN", "ERROR"] | None, Query()] = None,
    app_name: Annotated[list[str] | None, Query()] = None,
    severity: Annotated[
        list[Literal["critical", "high", "medium", "low"]] | None, Query()
    ] = None,
    cached: Annotated[bool | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> AlertFacets:
    """Per-value alert counts for the three multi-select filters. Read-only.

    Each facet is counted with every active filter EXCEPT its own, so the numbers
    answer "how many would I get if I ticked this?" while the other filters still
    scope them. See :func:`build_alert_facet_query` for why.
    """
    departments = [d.value for d in department] if department else None
    severities = list(severity) if severity else None

    def conditions_excluding(facet: str) -> list[ColumnElement[bool]]:
        return alert_filter_conditions(
            since,
            None if facet == "department" else departments,
            source,
            level=level,
            app_name=None if facet == "app_name" else app_name,
            severity=None if facet == "severity" else severities,
            resolved=resolved,
            cached=cached,
        )

    counts: dict[str, dict[str, int]] = {}
    # Sequential, in _FACET_COLUMNS order — one AsyncSession is not safe to use
    # concurrently.
    for facet in _FACET_COLUMNS:
        rows = (
            await session.execute(
                build_alert_facet_query(facet, conditions_excluding(facet))
            )
        ).all()
        counts[facet] = {str(value): count for value, count in rows}

    return AlertFacets(**counts)


@router.patch("/alerts/{alert_id}/resolve", response_model=AlertOut)
async def resolve_alert(
    alert_id: str,
    session: AsyncSession = Depends(get_session),
) -> Alert:
    stmt = (
        update(Alert)
        .where(Alert.alert_id == alert_id)
        .values(is_resolved=True, resolved_at=datetime.now(timezone.utc))
        .returning(Alert)
    )
    result = await session.execute(stmt)
    alert = result.scalar_one_or_none()
    if alert is None:
        raise HTTPException(status_code=404, detail=f"alert {alert_id!r} not found")
    await session.commit()
    return alert


@router.get("/journeys", response_model=Page[JourneyOut])
async def list_journeys(
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 16,
    cursor: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> Page[JourneyOut]:
    limit = max(1, min(limit, 100))
    # last_ts is nullable (a journey may exist before its first event lands),
    # so NULLs sort last in the newest-first order.
    stmt = apply_keyset(
        build_journeys_query(status),
        Journey.last_ts,
        Journey.journey_id,
        cursor=cursor,
        limit=limit,
        nulls_last=True,
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    items, next_cursor = build_page(
        rows, limit, lambda j: j.last_ts, lambda j: j.journey_id
    )
    return Page[JourneyOut](
        items=[JourneyOut.model_validate(j) for j in items],
        next_cursor=next_cursor,
    )


@router.get("/journeys/{journey_id}", response_model=JourneyDetailOut)
async def get_journey(
    journey_id: str,
    session: AsyncSession = Depends(get_session),
) -> JourneyDetailOut:
    result = await session.execute(
        select(Journey).where(Journey.journey_id == journey_id)
    )
    journey = result.scalar_one_or_none()
    if journey is None:
        raise HTTPException(status_code=404, detail=f"journey {journey_id!r} not found")

    events_result = await session.execute(
        select(JourneyEvent)
        .where(JourneyEvent.journey_id == journey_id)
        .order_by(JourneyEvent.ts.asc())
    )
    events = events_result.scalars().all()

    return JourneyDetailOut(
        **JourneyOut.model_validate(journey).model_dump(),
        events=[JourneyEventOut.model_validate(e) for e in events],
    )


@router.get("/stats/insights", response_model=OverviewStats)
async def get_overview_stats(
    session: AsyncSession = Depends(get_session),
) -> OverviewStats:
    """Aggregate counters for the insights page. Read-only.

    Runs the ``backend/stats.py`` builders and hands the raw group-by rows to
    :func:`~backend.stats.assemble_overview`. The statements execute one after
    another (one AsyncSession is not safe to use concurrently) and this route
    holds no logic of its own — everything derived lives in the assembler.
    """
    journey_total, journey_avg_duration = (
        await session.execute(stats.journey_totals())
    ).one()
    by_status = (await session.execute(stats.journeys_by_status())).all()
    by_outcome = (await session.execute(stats.journeys_by_outcome())).all()

    alert_total = (await session.execute(stats.alert_total())).scalar_one()
    resolution = (await session.execute(stats.alerts_resolution_counts())).all()
    by_department = (await session.execute(stats.alerts_by("department"))).all()
    by_severity = (await session.execute(stats.alerts_by("severity"))).all()
    by_level = (await session.execute(stats.alerts_by("level"))).all()
    by_source = (await session.execute(stats.alerts_by("source"))).all()

    return stats.assemble_overview(
        journeys_by_status=by_status,
        journeys_by_outcome=by_outcome,
        journey_total=journey_total,
        journey_avg_duration=journey_avg_duration,
        alerts_by_department=by_department,
        alerts_by_severity=by_severity,
        alerts_by_level=by_level,
        alerts_by_source=by_source,
        alerts_resolution=resolution,
        alert_total=alert_total,
    )
