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

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
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

    # Set only for a FAILED journey whose outcome is UNRECOGNIZED_FAILURE
    # (backend/journeys.py) — an LLM-suggested short phrase for the incident
    # title, persisted by backend/journeys.py's _finalize_journey and read by
    # backend/incidents.py's process_completion. Null for every other outcome,
    # and null when the AI service had nothing to suggest (LLM down, etc.).
    suggested_failure_label: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Set by backend/incidents.py on journey completion; null until then, and
    # for a journey that never forms/joins an incident (SUCCESS journeys, or a
    # TIMED_OUT journey with no linked ERROR alert).
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.incident_id"), nullable=True, index=True
    )

    events: Mapped[list["JourneyEvent"]] = relationship(
        back_populates="journey",
        cascade="all, delete-orphan",
    )
    alerts: Mapped[list["Alert"]] = relationship(back_populates="journey")


class Alert(Base):
    """A processed WARN/ERROR alert (``processed.alerts`` payload persisted).

    ``source`` is ``"ai"`` or ``"fallback"``; for fallback pass-throughs
    ``explanation`` / ``department`` / ``severity`` are null.
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
    severity: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)

    # Semantic-cache provenance: True when the AI service reused a stored answer
    # instead of calling the LLM. A MODIFIER on source="ai" (a hit is still an AI
    # answer), never an alternative to it — Teams routing keys off source alone.
    # Non-null with a false default, mirroring ProcessedAlert.cached.
    # ``default`` (Python-side) as well as ``server_default`` (DDL): the server
    # default only applies on INSERT, so an Alert built in memory and serialized
    # before any flush — exactly what the ``alert.new`` WebSocket envelope does —
    # would otherwise read None and fail AlertOut's non-optional bool.
    cached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.false()
    )

    # The masked-message vector (ai_service/semcache.py's embed(), shipped on
    # ProcessedAlert.embedding). None when no encoder was configured.
    embedding: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Nullable FK: the alert may precede its journey's incident-clustering
    # decision (which only happens at journey completion).
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.incident_id"), nullable=True, index=True
    )

    # Nullable FK: the alert may precede its assembled journey.
    journey_id: Mapped[str | None] = mapped_column(
        ForeignKey("journeys.journey_id"), nullable=True, index=True
    )

    # Manual triage — set by IT-support agents working the dashboard.
    is_resolved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.false()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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


class Incident(Base):
    """A cause-cluster of alerts (see repo-root incident-clustering-implementation-plan.md).

    INFRA-classed incidents may span many journeys/orders (``journey_count``
    grows as more orders hit the same failure); order-specific incidents
    always have ``journey_count == 1`` and stay permanently scoped to the one
    journey that created them this iteration — cross-order matching is never
    attempted for them (see ``backend/incidents.py``).
    """

    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String, primary_key=True)

    # Exact-match key for the recognized/deterministic path; null for a novel
    # (unrecognized) cause, which is matched by embedding cosine instead.
    signature: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    failure_subtype: Mapped[str | None] = mapped_column(String, nullable=True)
    failing_service: Mapped[str | None] = mapped_column(String, nullable=True)
    # Mined for display; NOT part of the signature hash (YAGNI — see this
    # plan's Global Constraints).
    error_token: Mapped[str | None] = mapped_column(String, nullable=True)

    title: Mapped[str] = mapped_column(Text, nullable=False)
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # "open" | "resolved"

    first_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set once at creation from the founding causal alert; NEVER reassigned —
    # it's the novel path's cosine-comparison target (backend/incidents.py),
    # not just a UI click-through.
    primary_alert_id: Mapped[str | None] = mapped_column(
        ForeignKey("alerts.alert_id"), nullable=True
    )

    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    journey_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ChatFeedback(Base):
    """One thumbs up/down on one assistant answer.

    Feedback is USER DATA, so unlike the retrieval index (in-memory + Redis,
    rebuildable by ``backfill_rag``) it lives here, durably.

    Keyed by ``answer_id`` — one row per answer, so voting again REPLACES rather
    than stacks. Stored per ANSWER, never per cited record: a vote rates the reply
    the agent read, and which sources deserve the credit is a derivation
    (rank-weighted, see ``backend/feedback.py``). Storing it per record would bake
    one attribution rule into the data and make it unchangeable later.

    ``record_ids`` is ORDERED — position is the signal, since attribution weights
    by citation rank.
    """

    __tablename__ = "chat_feedback"

    answer_id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    # +1 like / -1 dislike; a CHECK constraint keeps a stray 0 out of the ranking
    # math, where it would silently skew every boost.
    vote: Mapped[int] = mapped_column(SmallInteger, nullable=False)

    query: Mapped[str] = mapped_column(Text, nullable=False)
    record_ids: Mapped[list] = mapped_column(JSONB, nullable=False)

    # Context for later analysis; NOT used by the ranking math.
    answer_mode: Mapped[str | None] = mapped_column(String, nullable=True)
    scoped_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    scoped_id: Mapped[str | None] = mapped_column(String, nullable=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        CheckConstraint("vote IN (-1, 1)", name="ck_chat_feedback_vote"),
    )
