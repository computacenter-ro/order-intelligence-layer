"""[5] Core Backend — REST API (CLAUDE.md [5] "API").

Async FastAPI routes over the three tables in ``backend/db.py`` (``Alert``,
``Journey``, ``JourneyEvent``), using the ``get_session`` dependency. Mostly
read-only — journeys/alerts are produced by the consumers — except for the one
manual-triage write below.

Responses are Pydantic schemas (``AlertOut`` / ``JourneyOut`` /
``JourneyDetailOut``, defined in ``backend/schemas.py``), never the ORM models,
so the wire contract is explicit and decoupled from the DB layer.

Endpoints:

* ``GET /alerts?since=&department=&source=&level=&app_name=`` — alerts filtered
  by ``emitted_at >= since`` / ``department`` / ``source`` / ``level`` /
  ``app_name``, newest first. ``department`` must be one of the five
  ``Department`` values, ``source`` one of ``ai`` / ``fallback``, and ``level``
  one of ``WARN`` / ``ERROR``; any other value is a 422 (validated at the route,
  so ``build_alerts_query`` stays pure). ``app_name`` is a free string (the set
  of services can grow) — an unknown value simply matches nothing.
  ``cached=true|false`` narrows by semantic-cache provenance; it is orthogonal to
  ``source`` (every cached alert is ``source="ai"``), so it filters *within* AI
  answers rather than beside them.
* ``GET /journeys?status=`` — journeys filtered by ``status``.
* ``GET /journeys/{journey_id}`` — one journey + its events (ordered by ``ts``)
  + summary; 404 if the journey does not exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_user
from backend.db import Alert, Journey, JourneyEvent, get_session
from backend.pagination import apply_keyset, build_page
from backend.schemas import (
    AlertOut,
    ChatCoverage,
    ChatRequest,
    ChatResponse,
    ChatSource,
    JourneyDetailOut,
    JourneyEventOut,
    JourneyOut,
    Page,
)
from shared.models import Department


# --- query builders (pure, unit-testable) ------------------------------------


def build_alerts_query(
    since: datetime | None,
    department: str | None,
    source: str | None,
    level: str | None = None,
    app_name: str | None = None,
    severity: str | None = None,
    resolved: bool | None = None,
    cached: bool | None = None,
) -> Select:
    """Select alerts filtered by the given criteria.

    Filters only — ordering is applied by the paginator (:func:`apply_keyset`),
    which owns the sort column so callers can page by ``emitted_at`` or
    ``resolved_at`` over the same filter set.
    """
    stmt = select(Alert)
    if since is not None:
        stmt = stmt.where(Alert.emitted_at >= since)
    if department is not None:
        stmt = stmt.where(Alert.department == department)
    if source is not None:
        stmt = stmt.where(Alert.source == source)
    if resolved is not None:
        stmt = stmt.where(Alert.is_resolved == resolved)
    if level is not None:
        stmt = stmt.where(Alert.level == level)
    if app_name is not None:
        stmt = stmt.where(Alert.app_name == app_name)
    if severity is not None:
        stmt = stmt.where(Alert.severity == severity)
    if cached is not None:
        stmt = stmt.where(Alert.cached == cached)
    return stmt


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
    # Typing these as the Department enum / a source Literal makes FastAPI reject
    # out-of-domain values with a 422 (listing the allowed options) before the
    # query runs. build_alerts_query stays pure and str-typed — we pass the
    # validated value straight through.
    department: Annotated[Department | None, Query()] = None,
    source: Annotated[Literal["ai", "fallback"] | None, Query()] = None,
    resolved: Annotated[bool | None, Query()] = None,
    # Alerts are only ever WARN/ERROR, so the Literal rejects anything else with
    # a 422. app_name stays a free str — the set of services can grow, so an
    # unknown value should just match nothing, not be a validation error.
    level: Annotated[Literal["WARN", "ERROR"] | None, Query()] = None,
    app_name: Annotated[str | None, Query()] = None,
    # Router LLM severity (critical/high/medium/low); Literal → 422 on anything
    # else. Null for fallback / not-yet-rated alerts, so a severity filter
    # excludes those — exactly as ``Alert.severity == severity`` does server-side.
    severity: Annotated[
        Literal["critical", "high", "medium", "low"] | None, Query()
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
            department.value if department is not None else None,
            source,
            level=level,
            app_name=app_name,
            severity=severity,
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


async def _context_text(session: AsyncSession, kind: str, record_id: str) -> str | None:
    """The text of the record a question is scoped to, or None if absent.

    Read from THIS service's Postgres (the backend owns the DB) rather than asking
    the AI service — the index holds an embedded copy, but the DB is the source of
    truth and may be newer.
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
        return " ".join(parts)
    if kind == "journey":
        row = (
            await session.execute(select(Journey).where(Journey.journey_id == record_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        return f"Journey {row.outcome or row.status}: {row.summary or ''}".strip()
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
        context_text = await _context_text(session, body.context.kind, body.context.id)

    result = await ask(
        build_scoped_query(body.query, context_text), k=body.k, filters=body.filters
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
    )
