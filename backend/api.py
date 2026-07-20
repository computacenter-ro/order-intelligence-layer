"""[5] Core Backend — read-only REST API (CLAUDE.md [5] "API").

Async FastAPI routes over the three tables in ``backend/db.py`` (``Alert``,
``Journey``, ``JourneyEvent``), using the ``get_session`` dependency. The API is
**read-only** — it never writes; journeys/alerts are produced by the consumers.

Responses are Pydantic schemas (``AlertOut`` / ``JourneyOut`` /
``JourneyDetailOut``, defined in ``backend/schemas.py``), never the ORM models,
so the wire contract is explicit and decoupled from the DB layer.

Endpoints:

* ``GET /alerts?since=&department=&source=`` — alerts filtered by
  ``emitted_at >= since`` / ``department`` / ``source``, newest first.
* ``GET /journeys?status=`` — journeys filtered by ``status``.
* ``GET /journeys/{journey_id}`` — one journey + its events (ordered by ``ts``)
  + summary; 404 if the journey does not exist.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import Alert, Journey, JourneyEvent, get_session
from backend.schemas import AlertOut, JourneyDetailOut, JourneyEventOut, JourneyOut


# --- query builders (pure, unit-testable) ------------------------------------


def build_alerts_query(
    since: datetime | None,
    department: str | None,
    source: str | None,
) -> Select:
    """Select alerts filtered by the given criteria, newest ``emitted_at`` first."""
    stmt = select(Alert)
    if since is not None:
        stmt = stmt.where(Alert.emitted_at >= since)
    if department is not None:
        stmt = stmt.where(Alert.department == department)
    if source is not None:
        stmt = stmt.where(Alert.source == source)
    return stmt.order_by(Alert.emitted_at.desc())


def build_journeys_query(status: str | None) -> Select:
    """Select journeys, optionally filtered by ``status``."""
    stmt = select(Journey)
    if status is not None:
        stmt = stmt.where(Journey.status == status)
    return stmt


# --- routes ------------------------------------------------------------------

router = APIRouter()


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    since: Annotated[datetime | None, Query()] = None,
    department: Annotated[str | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> list[Alert]:
    result = await session.execute(build_alerts_query(since, department, source))
    return result.scalars().all()


@router.get("/journeys", response_model=list[JourneyOut])
async def list_journeys(
    status: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> list[Journey]:
    result = await session.execute(build_journeys_query(status))
    return result.scalars().all()


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
