"""Core backend database layer.

SQLAlchemy (async) models + engine/session factory for the three tables in
CLAUDE.md's "PostgreSQL schema (sketch)": ``alerts``, ``journeys`` and
``journey_events``.

Design notes tied to the Correlation Model (see CLAUDE.md):

* A journey is keyed by an internal ``journey_id``; the business ids
  (``event_id`` / ``order_id`` / ``cart_header_id``) are *aliases* accumulated
  over the journey's lifetime and any of them may be absent (pre-creation
  failures live and die with only ``event_id``). They are therefore nullable
  and NOT unique on ``journeys``.
* ``alerts.journey_id`` is nullable: an alert can arrive (and be shown on the
  dashboard) before its journey has been assembled from ``raw.events``.
* Idempotency for the at-least-once output queues is enforced by the unique
  constraints on ``alerts.log_id`` and ``journey_events.log_id`` (and
  ``alerts.alert_id`` as PK).
"""

from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

# --- Engine / session factory ------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://oil:oil@localhost:5432/oil",
)

engine: AsyncEngine = create_async_engine(DATABASE_URL, future=True)

SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncSession:
    """FastAPI dependency: yield a session, ensuring it is closed."""
    async with SessionLocal() as session:
        yield session


# --- Models ------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class Journey(Base):
    """An assembled per-order journey.

    Identified internally by ``journey_id``; the business ids are aliases that
    fill in over time and may be null (see Correlation Model).
    """

    __tablename__ = "journeys"

    journey_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)
    first_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Alias ids — any may be absent for a given journey.
    event_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    cart_header_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    events: Mapped[list["JourneyEvent"]] = relationship(
        back_populates="journey",
        cascade="all, delete-orphan",
    )
    alerts: Mapped[list["Alert"]] = relationship(back_populates="journey")


class Alert(Base):
    """A processed WARN/ERROR alert (``processed.alerts`` payload persisted).

    ``source`` is ``"ai"`` or ``"fallback"``; for fallback pass-throughs
    ``explanation`` / ``department`` / ``confidence`` are null.
    """

    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String, primary_key=True)
    emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # log_id is unique -> idempotent consumption of the at-least-once queue.
    log_id: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    level: Mapped[str] = mapped_column(String, nullable=False)
    app_name: Mapped[str] = mapped_column(String, nullable=False)
    logger: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Correlation ids carried by the original log line (any may be absent).
    event_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    cart_header_id: Mapped[str | None] = mapped_column(
        String, nullable=True, index=True
    )
    account_number: Mapped[str | None] = mapped_column(String, nullable=True)

    # AI enrichment — null for source="fallback".
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)

    # Nullable FK: the alert may precede its assembled journey.
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.journey_id"), nullable=True, index=True
    )

    journey: Mapped["Journey | None"] = relationship(back_populates="alerts")


class JourneyEvent(Base):
    """One raw log line belonging to a journey (``raw.events`` material)."""

    __tablename__ = "journey_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    journey_id: Mapped[str] = mapped_column(
        ForeignKey("journeys.journey_id"), nullable=False, index=True
    )
    # log_id unique -> idempotent raw.events assembly (constraint named below).
    log_id: Mapped[str] = mapped_column(String, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict] = mapped_column(JSONB, nullable=False)

    journey: Mapped["Journey"] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("log_id", name="uq_journey_events_log_id"),
    )
