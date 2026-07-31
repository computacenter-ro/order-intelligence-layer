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

  ``search=`` is a free-text, case-insensitive substring match (``ILIKE``) over
  ``message`` OR ``explanation``, ANDed with every other filter. Any text is
  valid; blank/whitespace-only is treated as absent.
* ``GET /alerts/facets`` — takes the same filter params as ``GET /alerts`` (no
  paging) and returns per-value counts for the three multi-select filters. Each
  facet is counted with every active filter EXCEPT its own, so ticking one value
  never collapses that facet's own list — but ``search`` has no facet of its own,
  so it scopes all three. Filter conditions come from the shared
  :func:`alert_filter_conditions`, so the feed and its counts cannot disagree.
* ``GET /journeys?status=&outcome=&search=`` — journeys filtered by ``status``
  and/or ``outcome`` (both free strings, exact match — see
  :func:`build_journeys_query` for why neither is a ``Literal``), plus a free-text
  ``search`` that substring-matches (``ILIKE``) any of the three alias ids
  (``event_id`` / ``order_id`` / ``cart_header_id``). Blank search = no filter.
* ``GET /journeys/{journey_id}`` — one journey + its events (ordered by ``ts``)
  + summary; 404 if the journey does not exist.
* ``GET /stats/insights`` — aggregate counters for the dashboard insights page.
  Query building and the folding of the group-by results both live in
  ``backend/stats.py`` (pure + unit-tested); this route only executes them.
* ``POST /chat`` — the authenticated front door for the AI service's grounded
  chat (:8100 is loopback + unauthenticated by design). Forwards to its
  ``/chat`` and decorates each cited source with a dashboard link. An optional
  ``context: {kind, id}`` anchors the question to one ``alert`` / ``journey`` /
  ``incident``, whose text is read from Postgres and prepended to the query.
  ``/chat`` and decorates each cited source with a dashboard link.
* ``GET /llm-stats`` — the same authenticated front door for the AI service's
  per-model LangSmith stats. Forwards to its ``/llm-stats`` and degrades to an
  all-nulls body when that service is down, so the panel never 502s.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, Select, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user
from backend.db import Alert, ChatFeedback, Incident, Journey, JourneyEvent, get_session
from backend.feedback import boosts_from
from backend.pagination import apply_keyset, build_page
# human_time lives in rag_client (the lower-level, shared module) so the indexer,
# the backfill script and this route all format LLM-facing timestamps identically.
from backend.llm_stats_client import fetch_llm_stats
from backend.rag_client import human_time
from backend import stats
from backend.schemas import (
    AlertFacets,
    AlertOut,
    ChatCoverage,
    ChatFeedbackRequest,
    ChatFeedbackResponse,
    ChatRequest,
    ChatResponse,
    ChatSource,
    IncidentDetailOut,
    IncidentOut,
    JourneyDetailOut,
    JourneyEventOut,
    JourneyOut,
    OverviewStats,
    Page,
)
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


def _like_term(search: str) -> str:
    """Wrap a search string as a ``%substring%`` LIKE pattern, wildcards escaped.

    ``%`` and ``_`` are LIKE metacharacters, and the alert corpus is full of both
    — margin messages quote percentages, and logger/service names are peppered
    with underscores. Passing them through raw makes ``12%`` match every alert and
    ``order_engine`` match ``orderXengine``, so a literal search silently returns
    the wrong rows. Escaped here (with the escape char itself first, or escaping
    would corrupt its own output), paired with ``escape="\\\\"`` at the call site.
    """
    escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def alert_filter_conditions(
    since: datetime | None,
    department: list[str] | None,
    source: str | None,
    level: str | None = None,
    app_name: list[str] | None = None,
    severity: list[str] | None = None,
    resolved: bool | None = None,
    cached: bool | None = None,
    search: str | None = None,
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

    ``search`` is a case-insensitive **substring** match (``ILIKE``) across
    ``message`` OR ``explanation`` — the raw log line and the AI's plain-English
    take on it, which is where an agent's search terms actually live. Whitespace
    is stripped and a blank string is treated as absent, so a cleared search box
    is "no filter" rather than a match-everything ``%%``. ``explanation`` is NULL
    on fallback alerts; ``ILIKE`` on NULL is NULL (not true), so those still match
    on ``message`` alone and are never wrongly excluded by the OR.
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
    if search and search.strip():
        term = _like_term(search.strip())
        conditions.append(
            or_(
                Alert.message.ilike(term, escape="\\"),
                Alert.explanation.ilike(term, escape="\\"),
            )
        )
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
    search: str | None = None,
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
            search=search,
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


def build_journeys_query(
    status: str | None,
    outcome: str | None = None,
    search: str | None = None,
) -> Select:
    """Select journeys, optionally filtered by ``status``, ``outcome`` and ``search``.

    ``search`` is a case-insensitive substring over the three alias ids
    (``event_id`` / ``order_id`` / ``cart_header_id``) — the only human-meaningful
    text a journey row carries. Grouped in ONE ``or_`` so it ANDs with the other
    filters as a unit; flattening it would turn ``status = X AND (a OR b OR c)``
    into ``(status = X AND a) OR b OR c``, quietly ignoring the status filter for
    anything matching on the second or third id.

    Three deliberate choices, recorded so they read as decisions rather than
    oversights:

    * **``outcome`` is a free ``str``, not a ``Literal``.** Its sibling ``status``
      on the same route is already an unconstrained ``str``, and the ten outcome
      values are module constants in ``backend/journeys.py`` — restating them in a
      ``Literal`` here creates two lists to keep in step. The consequence is that a
      misspelled value yields an empty list rather than a 422, which is exactly how
      ``status`` already behaves.
    * **No NULL bucket for ``outcome``.** ``status=IN_PROGRESS`` already selects the
      journeys that have no outcome yet, and the convention borrowed from
      ``alert_filter_conditions`` is that a non-empty filter excludes NULLs, as SQL
      does. Note the corollary for search: ``ILIKE`` on a NULL column is NULL, so a
      journey with no ``order_id`` never matches an order-id search. Correct, not a
      gap — it genuinely has no such id.
    * **Both are sequential scans.** ``outcome`` is unindexed (like ``status``
      today), and while the three id columns ARE indexed, a leading-wildcard
      ``ILIKE '%x%'`` cannot use a btree index. Fine at these volumes; nobody
      should assume the indexes are helping here.

    Every clause stays strictly conditional: with no filters the statement compiles
    without a WHERE at all.
    """
    stmt = select(Journey)
    conditions = []
    if status is not None:
        conditions.append(Journey.status == status)
    if outcome is not None:
        conditions.append(Journey.outcome == outcome)
    if search and search.strip():
        # Same helper the alert search uses — it already escapes the LIKE
        # metacharacters (and the escape char first), and is already covered by
        # tests. A cart header id is 19 digits and an event id is a UUID, so `_`
        # and `%` are unlikely in practice, but a second implementation of this
        # would be a second thing to get wrong.
        term = _like_term(search.strip())
        conditions.append(
            or_(
                Journey.event_id.ilike(term, escape="\\"),
                Journey.order_id.ilike(term, escape="\\"),
                Journey.cart_header_id.ilike(term, escape="\\"),
            )
        )
    if conditions:
        stmt = stmt.where(*conditions)
    return stmt


def build_incidents_query(status: str | None, department: list[str] | None = None) -> Select:
    """Select incidents, optionally filtered by ``status`` ('open'/'resolved')
    and/or ``department``.

    ``department`` is multi-valued, same convention as the alerts filters: a
    non-empty list becomes an ``IN (...)`` (OR within the category); ``None``
    and an empty list both mean "no filter" (an ``IN ()`` would instead match
    nothing and empty the page).
    """
    stmt = select(Incident)
    if status is not None:
        stmt = stmt.where(Incident.status == status)
    if department:
        stmt = stmt.where(Incident.department.in_(department))
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
    # Free-text substring search over message OR explanation, case-insensitive.
    # A free string by nature — no value to validate, and a blank one is treated
    # as absent by alert_filter_conditions.
    search: Annotated[str | None, Query()] = None,
    # Cursor pagination (see backend/pagination.py). limit is clamped to 1..100
    # rather than 422'd so a caller can pass anything and still get a sane page.
    limit: Annotated[int, Query()] = 12,
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
            search=search,
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
    search: Annotated[str | None, Query()] = None,
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
            # Never excluded: search is not a facet (there is no list of values to
            # count), so it scopes every facet. The counts therefore describe the
            # search results, which is what makes them usable while searching.
            search=search,
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
    outcome: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 12,
    cursor: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> Page[JourneyOut]:
    limit = max(1, min(limit, 100))
    # last_ts is nullable (a journey may exist before its first event lands),
    # so NULLs sort last in the newest-first order.
    stmt = apply_keyset(
        build_journeys_query(status, outcome, search),
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


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    status: Annotated[Literal["open", "resolved"] | None, Query()] = None,
    department: Annotated[list[Department] | None, Query()] = None,
    limit: Annotated[int, Query()] = 12,
    cursor: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> Page[IncidentOut]:
    limit = max(1, min(limit, 100))
    stmt = apply_keyset(
        build_incidents_query(status, department),
        Incident.last_ts,
        Incident.incident_id,
        cursor=cursor,
        limit=limit,
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    items, next_cursor = build_page(
        rows, limit, lambda i: i.last_ts, lambda i: i.incident_id
    )
    return Page[IncidentOut](
        items=[IncidentOut.model_validate(i) for i in items],
        next_cursor=next_cursor,
    )


@router.get("/incidents/{incident_id}", response_model=IncidentDetailOut)
async def get_incident(
    incident_id: str,
    session: AsyncSession = Depends(get_session),
) -> IncidentDetailOut:
    result = await session.execute(
        select(Incident).where(Incident.incident_id == incident_id)
    )
    incident = result.scalar_one_or_none()
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")

    alerts_result = await session.execute(
        select(Alert).where(Alert.incident_id == incident_id).order_by(Alert.emitted_at.asc())
    )
    alerts = alerts_result.scalars().all()

    return IncidentDetailOut(
        **IncidentOut.model_validate(incident).model_dump(),
        alerts=[AlertOut.model_validate(a) for a in alerts],
    )


@router.patch("/incidents/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_incident(
    incident_id: str,
    session: AsyncSession = Depends(get_session),
) -> Incident:
    """Manual close — the PRIMARY lifecycle mechanism (source spec §4 step 7);
    the quiet-timeout sweep in backend/incidents.py is only the fallback.

    Cascades to every alert linked to this incident: an incident is a
    collapsed VIEW of those alerts, so leaving them "active" after their
    incident is closed would defeat the point of collapsing them — they'd
    never leave the live Alert Feed and never show up in History. Uses the
    same is_resolved/resolved_at values PATCH /alerts/{id}/resolve does, so a
    cascaded alert is indistinguishable from an individually-resolved one.
    Alerts already resolved (individually, earlier) are left with their
    original resolved_at.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        update(Incident)
        .where(Incident.incident_id == incident_id)
        .values(status="resolved")
        .returning(Incident)
    )
    result = await session.execute(stmt)
    incident = result.scalar_one_or_none()
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id!r} not found")

    await session.execute(
        update(Alert)
        .where(Alert.incident_id == incident_id, Alert.is_resolved.is_(False))
        .values(is_resolved=True, resolved_at=now)
    )
    await session.commit()
    return incident


# --- chat (authenticated proxy to the AI service) -----------------------------
#
# The AI service's :8100 is loopback + unauthenticated by design; this route is
# the authenticated front door for it. It sits on the same auth-guarded router as
# every read route, so a session cookie is required — declared once at the router
# level, which is why there is no per-route Depends here.


def _dashboard_link(metadata: dict, *, kind: str = "", record_id: str = "") -> str | None:
    """Dashboard journey link for a cited record, or None.

    Same rule as ``backend/teams.py::_dashboard_link``: DASHBOARD_URL + the
    journey_id (or order_id) the record carries. For a ``journey`` record the
    record id IS the journey id, so it is used as a last resort — otherwise a
    journey citation, the most link-worthy kind, would render without a link.
    None when DASHBOARD_URL is unset or nothing identifies a journey; the UI then
    shows the citation as plain text.
    """
    import os

    base = os.getenv("DASHBOARD_URL", "").rstrip("/")
    ref = metadata.get("journey_id") or metadata.get("order_id")
    if not ref and kind == "journey":
        ref = record_id
    if not base or not ref:
        return None
    return f"{base}/journeys/{ref}"


# How many of a journey's log lines to include in the scoped context.
#
# A journey can carry 40+ events, most of them DEBUG/INFO filler. Sending all of
# them would make every scoped question a large, slow prompt for little gain, so
# the selection is: every WARN/ERROR (where the story is), plus the first and last
# lines (where it started and how it ended), capped at this many.
_CONTEXT_EVENT_CAP = 24


def select_context_events(events: list) -> list:
    """The log lines worth showing for a scoped journey question (pure).

    Keeps WARN/ERROR lines plus the first and last event, in timestamp order,
    truncated to :data:`_CONTEXT_EVENT_CAP`. Returns the ORM/dict rows unchanged so
    the caller decides formatting.

    Why not all of them: the summary alone could not answer "what came next?",
    "how many retries?" or "what time did it fail?" — but the full DEBUG trace
    would bury those answers and cost a large prompt on every question. The
    WARN/ERROR lines are where a failure narrative actually lives.
    """
    if not events:
        return []
    keep: dict[int, object] = {}
    for i, event in enumerate(events):
        level = str((event.raw or {}).get("level", "")).upper()
        if level in ("WARN", "ERROR") or i == 0 or i == len(events) - 1:
            keep[i] = event
    ordered = [keep[i] for i in sorted(keep)]
    if len(ordered) <= _CONTEXT_EVENT_CAP:
        return ordered
    # Over the cap: keep the head and the tail, since a truncated middle costs
    # less than losing either the start or the terminal line.
    half = _CONTEXT_EVENT_CAP // 2
    return ordered[:half] + ordered[-half:]


def format_context_events(events: list, tz: str | None = None) -> str:
    """Render selected journey events as one line each (pure, testable)."""
    lines = []
    for event in events:
        raw = event.raw or {}
        ts = human_time(raw.get("timestamp") or event.ts, tz) or ""
        lines.append(
            f"  {ts} {raw.get('level', '')} {raw.get('app_name', '')}: {raw.get('message', '')}"
        )
    return "\n".join(lines)


# How many affected orders to include in an incident's scoped context.
#
# An INFRA-classed incident can absorb many orders (journey_count grows as more
# hit the same failure), so this is bounded like _CONTEXT_EVENT_CAP is. One
# representative line per order rather than every alert: breadth is what an
# incident question needs, and the per-journey detail is what the JOURNEY scope
# already provides.
_CONTEXT_ORDER_CAP = 12


def _incident_order_groups(alerts: list, journeys: list) -> list[dict]:
    """One entry per affected order, freshest first (pure).

    Mirrors ``dashboard/lib/incidents.ts::groupAlertsByOrder`` deliberately, so
    the assistant describes the same units the agent sees on screen: group on
    ``journey_id``, label from the representative alert's
    ``order_id``/``event_id``, and pick the representative as the LAST ERROR
    (else the last alert) — ``_pickOutcome``'s rule.

    Ordering is by the representative alert's ``emitted_at`` descending. That is
    PROCESSING time, not log time, so it is only ever used to choose *which*
    orders survive the cap — never presented to the model as chronology.
    """
    outcomes = {j.journey_id: j.outcome for j in journeys}

    grouped: dict[str, list] = {}
    orphans: list = []
    for alert in alerts:
        if alert.journey_id is None:
            orphans.append(alert)          # clustered before it was linked
            continue
        grouped.setdefault(alert.journey_id, []).append(alert)

    def _representative(group: list):
        ordered = sorted(group, key=lambda a: a.emitted_at)
        for alert in reversed(ordered):
            if alert.level == "ERROR":
                return alert
        return ordered[-1]

    groups: list[dict] = []
    for journey_id, group in grouped.items():
        alert = _representative(group)
        groups.append({
            "label": alert.order_id or alert.event_id or alert.alert_id,
            "outcome": outcomes.get(journey_id),
            "alert": alert,
        })
    for alert in orphans:
        groups.append({
            "label": alert.order_id or alert.event_id or alert.alert_id,
            "outcome": None,
            "alert": alert,
        })

    groups.sort(key=lambda g: g["alert"].emitted_at, reverse=True)
    return groups


def _format_span(seconds: float) -> str:
    """``466`` -> ``"7m 46s"``. Precomputed because date arithmetic is exactly
    what an LLM gets wrong, and "how long has this been going on?" is one of the
    most natural questions to ask an incident."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_incident_context(
    incident, alerts: list, journeys: list, tz: str | None = None
) -> str:
    """The text of an incident scope (pure, testable).

    Three rules are load-bearing and each has a test:

    * Counts are ``len()`` of the rows passed in, NEVER
      ``incident.alert_count`` / ``journey_count`` — those are maintained
      incrementally and drift as orders join.
    * ``incident.first_ts`` / ``last_ts`` are NOT emitted at all. They are
      clustering wall-clock times (``assign_incident`` / ``process_completion``),
      not failure times, and the model quotes whatever it is given.
    * The output is not a timeline. ``Alert`` carries no original-log timestamp,
      and ``emitted_at`` is processing time under concurrent processing, so no
      ordering between member alerts is implied.
    """
    groups = _incident_order_groups(alerts, journeys)

    facts = [f"status={incident.status}"]
    for key, value in (
        ("department", incident.department),
        ("failing_service", incident.failing_service),
        ("error_token", incident.error_token),
    ):
        if value:
            facts.append(f"{key}={value}")
    facts.append(
        f"orders={len(groups)} alerts={len(alerts)} total "
        f"(one representative line shown per order)"
    )

    # The REAL failure window: journeys are built from raw log lines, so their
    # first_ts/last_ts are true log timestamps (the 90s stall arithmetic relies
    # on that). Omitted entirely when no journey carries one, rather than
    # rendered as an empty or zero range.
    starts = [j.first_ts for j in journeys if j.first_ts is not None]
    ends = [j.last_ts for j in journeys if j.last_ts is not None]
    if starts and ends:
        first, last = min(starts), max(ends)
        facts.append(
            f"failures from {human_time(first, tz)} to {human_time(last, tz)} "
            f"(span {_format_span((last - first).total_seconds())})"
        )

    shown = groups[:_CONTEXT_ORDER_CAP]
    if len(groups) > len(shown):
        heading = f"Affected orders ({len(groups)}, showing {len(shown)}):"
    else:
        heading = f"Affected orders ({len(groups)}):"

    lines = [f"{incident.title} [{' '.join(facts)}]", "", heading]
    for group in shown:
        alert = group["alert"]
        label = group["label"]
        if group["outcome"]:
            label = f"{label} ({group['outcome']})"
        lines.append(
            f"  {label} — {alert.level} {alert.app_name} {alert.logger}: "
            f"{alert.message}"
        )
        if alert.explanation:
            lines.append(f"    {alert.explanation.strip()}")
    return "\n".join(lines)


async def _context_text(
    session: AsyncSession, kind: str, record_id: str, tz: str | None = None
) -> str | None:
    """The text of the record a question is scoped to, or None if absent.

    Read from THIS service's Postgres (the backend owns the DB) rather than asking
    the AI service — the index holds an embedded copy, but the DB is the source of
    truth and may be newer, and it has detail the index deliberately does not (the
    per-journey log lines).
    """
    if kind == "alert":
        row = (
            await session.execute(select(Alert).where(Alert.alert_id == record_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        parts = [f"{row.app_name} {row.level} {row.logger}: {row.message}"]
        if row.explanation:
            parts.append(row.explanation)
        # Ids + time so a scoped question can ask "when did this happen?" or
        # "which journey is this part of?" — previously unanswerable because the
        # context was only the message and the explanation.
        facts = [f"at={human_time(row.emitted_at, tz)}" if row.emitted_at else ""]
        for key, value in (
            ("order_id", row.order_id),
            ("event_id", row.event_id),
            ("journey_id", row.journey_id),
            ("department", row.department),
            ("severity", row.severity),
        ):
            if value:
                facts.append(f"{key}={value}")
        return " ".join(parts) + " [" + " ".join(f for f in facts if f) + "]"

    if kind == "journey":
        row = (
            await session.execute(select(Journey).where(Journey.journey_id == record_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        header = f"Journey {row.outcome or row.status}"
        facts = []
        for key, value in (
            ("order_id", row.order_id),
            ("event_id", row.event_id),
            ("cart_header_id", row.cart_header_id),
            ("started", human_time(row.first_ts, tz)),
            ("ended", human_time(row.last_ts, tz)),
        ):
            if value:
                facts.append(f"{key}={value}")
        blocks = [f"{header} [{' '.join(facts)}]"]
        if row.summary:
            blocks.append(row.summary.strip())

        # The events are the point of this change: the summary is 2-4 sentences,
        # while the DB holds the actual sequence the question is usually about.
        events = (
            (
                await session.execute(
                    select(JourneyEvent)
                    .where(JourneyEvent.journey_id == record_id)
                    .order_by(JourneyEvent.ts.asc())
                )
            )
            .scalars()
            .all()
        )
        selected = select_context_events(list(events))
        if selected:
            blocks.append(
                f"Log lines ({len(selected)} of {len(events)} shown — "
                f"WARN/ERROR plus first and last):\n{format_context_events(selected, tz)}"
            )
        return "\n".join(blocks)

    if kind == "incident":
        row = (
            await session.execute(
                select(Incident).where(Incident.incident_id == record_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        # Membership is read live from Postgres rather than from the retrieval
        # index: the index has no notion of incidents at all (an alert is indexed
        # at persist time, before its journey completes and long before
        # clustering runs), and only the DB can give the EXACT, CURRENT member
        # set instead of a top-k semantic sample.
        alerts = (
            (
                await session.execute(
                    select(Alert).where(Alert.incident_id == record_id)
                )
            )
            .scalars()
            .all()
        )
        journeys = (
            (
                await session.execute(
                    select(Journey).where(Journey.incident_id == record_id)
                )
            )
            .scalars()
            .all()
        )
        return format_incident_context(row, list(alerts), list(journeys), tz)

    return None


def build_scoped_query(query: str, context_text: str | None) -> str:
    """Prepend the scoped record's text to the question (pure, testable).

    Anchors both halves of the pipeline at once: retrieval matches against the
    record's own wording, and the composer sees what the agent is looking at. No
    context (or an id that no longer exists) degrades to the bare question rather
    than erroring — the answer is then merely unscoped, which is still useful.
    """
    if not context_text:
        return query
    return f"Regarding this incident: {context_text}\n\nQuestion: {query}"


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    session: AsyncSession = Depends(get_session),
) -> ChatResponse:
    """Ask a grounded question about incident history (auth required).

    Forwards to the AI service's ``/chat`` and decorates each cited source with a
    dashboard link. ``mode`` is passed through untouched (``ai`` when the LLM
    composed the answer, ``retrieval-only`` when it was unavailable) so the UI can
    badge it exactly like the alert feed badges AI vs fallback.
    """
    from backend.rag_client import ask

    context_text = None
    if body.context is not None:
        context_text = await _context_text(
            session, body.context.kind, body.context.id, body.tz
        )

    # Agent feedback is the backend's data, but ranking happens in the AI service
    # (which owns the index and must stay DB-free), so the counts travel WITH the
    # request. Failure here is non-fatal: no boosts means ranking falls back to
    # pure relevance, which is the pre-feedback behaviour.
    boosts = await _feedback_boosts(session)

    result = await ask(
        build_scoped_query(body.query, context_text),
        k=body.k,
        filters=body.filters,
        boosts=boosts,
    )
    return ChatResponse(
        answer=result["answer"],
        sources=[
            ChatSource(
                id=str(s.get("id", "")),
                kind=str(s.get("kind", "")),
                score=float(s.get("score") or 0.0),
                snippet=str(s.get("snippet", "")),
                link=_dashboard_link(
                    s.get("metadata") or {},
                    kind=str(s.get("kind", "")),
                    record_id=str(s.get("id", "")),
                ),
            )
            for s in result["sources"]
        ],
        mode=result["mode"],
        # Passed straight through — the AI service computed it from the actual
        # retrieval, and the dashboard renders it (a badge on counting answers)
        # rather than reading it out of the prose.
        coverage=ChatCoverage(**(result.get("coverage") or {})),
        # Minted here so the dashboard can rate THIS answer. Not persisted until a
        # vote actually arrives — an unrated answer leaves no row.
        answer_id=uuid.uuid4().hex,
    )


# Most recent votes to fold into the boosts. Bounded so one query stays cheap on
# the chat path; older votes are decayed to near-nothing anyway
# (backend/feedback.py HALF_LIFE_DAYS), so the tail contributes little.
_FEEDBACK_SCAN_LIMIT = 2000


def settings_feedback_weight() -> float:
    """The configured blend weight, read from the AI service's settings.

    Read at call time rather than import time so a test can monkeypatch it, and
    kept in one function so the "is the feature on?" check has a single home.
    """
    from ai_service import settings as ai_settings

    return float(getattr(ai_settings, "RAGINDEX_FEEDBACK_WEIGHT", 0.0))


async def _feedback_boosts(session: AsyncSession) -> dict[str, float]:
    """Per-record feedback boosts for ranking, or ``{}`` on any problem.

    Returns ``{}`` — never raises — when the weight is 0 (feature off) or the query
    fails: retrieval then ranks on relevance alone, exactly as it did before
    feedback existed. Search must not break because a vote table is unavailable.
    """
    if settings_feedback_weight() <= 0.0:
        return {}
    try:
        rows = (
            await session.execute(
                select(ChatFeedback).order_by(ChatFeedback.created_at.desc()).limit(
                    _FEEDBACK_SCAN_LIMIT
                )
            )
        ).scalars().all()
        return boosts_from(rows)
    except Exception as exc:  # noqa: BLE001 — ranking degrades, never fails
        print(f"[chat] feedback boosts unavailable: {type(exc).__name__}: {exc}", flush=True)
        return {}


@router.post("/chat/feedback", response_model=ChatFeedbackResponse)
async def chat_feedback(
    body: ChatFeedbackRequest,
    user: str = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatFeedbackResponse:
    """Record a thumbs up/down on one answer (auth required).

    Upserts on ``answer_id``, so voting again REPLACES the previous vote rather
    than stacking — an agent can change their mind without inflating the tally.

    Stored per ANSWER, not per record: the vote rates the reply that was read, and
    credit is attributed to its sources as a derivation (rank-weighted, see
    ``backend/feedback.py``). That keeps the attribution rule changeable later.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    vote = 1 if body.liked else -1
    values = {
        "answer_id": body.answer_id,
        "created_at": datetime.now(timezone.utc),
        "vote": vote,
        "query": body.query,
        "record_ids": list(body.record_ids),
        "answer_mode": body.answer_mode,
        "scoped_kind": body.scoped_kind,
        "scoped_id": body.scoped_id,
        "username": user,
    }
    stmt = (
        pg_insert(ChatFeedback)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["answer_id"],
            # Refresh created_at too: a changed vote is new evidence, and recency
            # decay should treat it as such.
            set_={k: values[k] for k in ("vote", "created_at")},
        )
    )
    await session.execute(stmt)
    await session.commit()
    return ChatFeedbackResponse(recorded=True, liked=body.liked)


@router.get("/llm-stats")
async def llm_stats(window: str = "24h") -> dict:
    """Per-logical-model LLM stats, forwarded from the AI service. Read-only.

    Guarded by the router-level ``get_current_user`` like every other route here,
    so :8100 stays unreachable from the browser. ``window`` is passed through
    untouched — the AI service owns which values it understands and treats an
    unknown one as its default, so there is nothing to validate twice.

    Never 5xx: an unreachable AI service yields the same all-nulls body it would
    return with LangSmith unconfigured (see ``llm_stats_client.degraded``).
    """
    return await fetch_llm_stats(window)


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
    cache = (await session.execute(stats.alerts_cache_counts())).all()
    by_department = (await session.execute(stats.alerts_by("department"))).all()
    by_severity = (await session.execute(stats.alerts_by("severity"))).all()
    by_level = (await session.execute(stats.alerts_by("level"))).all()
    by_source = (await session.execute(stats.alerts_by("source"))).all()

    incident_total = (await session.execute(stats.incident_total())).scalar_one()
    incidents_by_status = (await session.execute(stats.incidents_by_status())).all()
    alerts_clustered = (
        await session.execute(stats.alerts_clustered_count())
    ).scalar_one()

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
        alerts_cache=cache,
        alert_total=alert_total,
        incidents_by_status=incidents_by_status,
        incident_total=incident_total,
        alerts_clustered=alerts_clustered,
    )
