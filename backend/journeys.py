"""Incremental ("lazy") journey assembly + journey-over detection.

Two cleanly separated layers:

1. **Pure detection** — module-level functions that decide a journey's outcome
   from data alone, with no DB and no queues:
   * ``classify_failure(message)`` — map one log message to a FAILED subtype.
   * ``detect_terminal(logs)`` — given a journey's logs *in order*, return its
     terminal outcome (``SUCCESS`` or a FAILED subtype) or ``None`` if it is
     still in progress. This is the message-driven half of the "journey over"
     rules in CLAUDE.md and is fully unit-testable.
   * ``is_stalled(last_ts, now, timeout)`` — the time-driven ``TIMED_OUT`` rule.

2. **Assembly + persistence** — ``JourneyAssembler`` wraps the pure
   ``backend.stitching.Stitcher``, reports each journey's completion exactly
   once (``evaluate``), and persists journeys + their events to Postgres
   (``ingest`` / ``sweep_stalled``). The DB layer is a thin wrapper over the
   pure decisions above, so all interesting logic stays testable without a DB.

Journey-over rules (CLAUDE.md — exactly three; message texts are load-bearing):

* **SUCCESS** — the last event is the track-trace terminal
  ("Registered order ... for tracking").
* **FAILED** — a log carries a dead-letter routing marker
  (``order.inbound.queue_error`` / ``order.outbound.queue_error`` — the mock
  services actually emit the ``.dlq`` spelling, which we also match) or a fatal
  abort ("Order creation failed for event", "Order processing aborted",
  "submission aborted", "blocked by margin check", JAM "not authorized" 403,
  "Max redelivery attempts reached"). The subtype is derived from the message.
* **TIMED_OUT** — no new event for the journey's ids for ``STALLED_TIMEOUT``
  seconds (default 90; env-configurable). All clock arithmetic is UTC and
  timezone-aware — never ``utcnow()``.

Pre-creation failures (transform / creation) never acquire order ids: their
journeys carry only ``event_id``. That is correct and complete, not a data gap.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from shared.models import LogLine
from backend.stitching import StitchedJourney, Stitcher

# An optional async sink for WebSocket-style events (see backend/ws.py). Kept as
# a bare Callable so the assembler stays decoupled from the transport: when None
# (the default) the assembler broadcasts nothing — it is fully usable headless.
OnEvent = Callable[[dict], Awaitable[None]]

# --- Config ------------------------------------------------------------------

STALLED_TIMEOUT: int = int(os.getenv("STALLED_TIMEOUT", "90"))


def _utcnow() -> datetime:
    """UTC, timezone-aware now. Never naive, never ``utcnow()``."""
    return datetime.now(timezone.utc)


# --- Outcome vocabulary ------------------------------------------------------
# The subtype strings match shared/scenarios.py's canonical outcomes exactly.

SUCCESS = "SUCCESS"
TIMED_OUT = "TIMED_OUT"
FAILED = "FAILED"  # generic fallback subtype for an unclassified fatal abort

INBOUND_TRANSFORM_FAILED = "INBOUND_TRANSFORM_FAILED"
ORDER_CREATION_FAILED = "ORDER_CREATION_FAILED"
MARGIN_CHECK_FAILED = "MARGIN_CHECK_FAILED"
VALIDATION_FAILED = "VALIDATION_FAILED"
ENRICHMENT_FAILED = "ENRICHMENT_FAILED"
AUTH_FAILED = "AUTH_FAILED"
SAP_SUBMISSION_FAILED = "SAP_SUBMISSION_FAILED"


class JourneyStatus(str, Enum):
    """High-level journey state persisted on ``journeys.status``."""

    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


def status_for(outcome: str) -> JourneyStatus:
    """Map an outcome subtype to its high-level status."""
    if outcome == SUCCESS:
        return JourneyStatus.SUCCESS
    if outcome == TIMED_OUT:
        return JourneyStatus.TIMED_OUT
    return JourneyStatus.FAILED


# --- Pure detection ----------------------------------------------------------

# Ordered, most-specific first. Each entry: (marker substrings, subtype). A
# message matches if it contains ANY marker (case-insensitive). Order matters:
# the JAM abort line contains both "not authorized" and the generic
# "submission aborted", so AUTH must be tested before the validation rule.
_FAILURE_RULES: list[tuple[tuple[str, ...], str]] = [
    (("not authorized", "403 from jam"), AUTH_FAILED),
    (("blocked by margin check",), MARGIN_CHECK_FAILED),
    (("order processing aborted",), ENRICHMENT_FAILED),
    (("order creation failed for event",), ORDER_CREATION_FAILED),
    (
        ("max redelivery attempts reached",
         "order.inbound.queue_error",
         "order.inbound.dlq"),
        INBOUND_TRANSFORM_FAILED,
    ),
    (
        ("order.outbound.queue_error",
         "order.outbound.dlq",
         "submission failed after"),
        SAP_SUBMISSION_FAILED,
    ),
    (("validation failed", "submission aborted"), VALIDATION_FAILED),
]

_SUCCESS_MARKERS = ("registered order", "for tracking")


def classify_failure(message: str) -> str | None:
    """Return the FAILED subtype a fatal message denotes, or ``None``.

    Benign lines (retries, "Not implemented", "Margin check passed", ...) return
    ``None`` — only terminal/fatal markers classify.
    """
    text = message.lower()
    for markers, outcome in _FAILURE_RULES:
        if any(marker in text for marker in markers):
            return outcome
    return None


def detect_terminal(logs: list[LogLine]) -> str | None:
    """Return a journey's terminal outcome from its ordered logs, or ``None``.

    Message-driven only (SUCCESS + FAILED). ``TIMED_OUT`` is time-driven and
    lives in :func:`is_stalled` / :class:`JourneyAssembler`.

    A fatal marker wins over everything (the baton stops at a fatal failure, so
    a FAILED marker never coexists with a success terminal). We scan every log
    for a fatal marker rather than only the last, because a failure can be
    followed by trailing INFO lines (e.g. the creation-failure publish log).
    """
    if not logs:
        return None
    for log in logs:
        subtype = classify_failure(log.message)
        if subtype is not None:
            return subtype
    last = logs[-1].message.lower()
    if all(marker in last for marker in _SUCCESS_MARKERS):
        return SUCCESS
    return None


def is_stalled(last_ts: datetime, now: datetime, timeout: int = STALLED_TIMEOUT) -> bool:
    """True if ``now`` is more than ``timeout`` seconds after ``last_ts``.

    Both must be timezone-aware UTC (the invariant across the whole system).
    """
    return (now - last_ts) > timedelta(seconds=timeout)


# --- Completion record -------------------------------------------------------


@dataclass
class Completion:
    """A journey that has reached a terminal state."""

    journey_id: str
    journey: StitchedJourney
    status: JourneyStatus
    outcome: str  # subtype (== status value for SUCCESS / TIMED_OUT)


# --- Incremental assembly + persistence --------------------------------------


class JourneyAssembler:
    """Assembles journeys incrementally over the ``raw.events`` stream.

    Wraps a pure :class:`Stitcher`, applies the journey-over rules, and reports
    each completion exactly once. The DB-free decision layer (``add`` /
    ``evaluate``) is directly unit-testable; ``ingest`` / ``sweep_stalled`` add
    persistence on top.
    """

    def __init__(self, stalled_timeout: int | None = None) -> None:
        self._stitcher = Stitcher()
        self._completed: dict[str, Completion] = {}
        self._timeout = STALLED_TIMEOUT if stalled_timeout is None else stalled_timeout

    # --- DB-free decision layer ---------------------------------------------

    def add(self, logs) -> list[tuple[StitchedJourney, LogLine]]:
        """Stitch a batch (timestamp order) into journeys.

        Returns the ``(journey, log)`` pairs for the *newly* attached logs
        (duplicates by ``log_id`` are skipped) — exactly the events persistence
        needs to write.
        """
        new_events: list[tuple[StitchedJourney, LogLine]] = []
        for log in sorted(_as_list(logs), key=lambda l: l.timestamp):
            journey = self._stitcher.add(log)
            if journey is None:  # duplicate log_id
                continue
            new_events.append((journey, log))
        return new_events

    def evaluate(self, now: datetime | None = None) -> list[Completion]:
        """Return journeys that have JUST completed; report each only once.

        A message-driven terminal (SUCCESS / FAILED) always wins over the
        time-driven TIMED_OUT.
        """
        now = now or _utcnow()
        completions: list[Completion] = []
        for journey in self._stitcher.journeys:
            if journey.journey_id in self._completed:
                continue
            status, outcome = self._state(journey, now)
            if status is JourneyStatus.IN_PROGRESS:
                continue
            completion = Completion(
                journey_id=journey.journey_id,
                journey=journey,
                status=status,
                outcome=outcome,
            )
            self._completed[journey.journey_id] = completion
            completions.append(completion)
        return completions

    def _state(self, journey: StitchedJourney, now: datetime) -> tuple[JourneyStatus, str | None]:
        outcome = detect_terminal(journey.logs)
        if outcome is not None:
            return status_for(outcome), outcome
        if journey.last_ts is not None and is_stalled(journey.last_ts, now, self._timeout):
            return JourneyStatus.TIMED_OUT, TIMED_OUT
        return JourneyStatus.IN_PROGRESS, None

    @property
    def stitcher(self) -> Stitcher:
        return self._stitcher

    # --- Persistence --------------------------------------------------------

    async def ingest(
        self,
        session,
        logs,
        now: datetime | None = None,
        on_event: OnEvent | None = None,
    ) -> list[Completion]:
        """Stitch + persist a batch, then persist any journeys that completed.

        Idempotent: events are written with ON CONFLICT DO NOTHING on
        ``log_id`` (the output queues are at-least-once).

        When ``on_event`` is given (else no-op), after a successful commit it
        emits — for every journey this chunk grew but did not finish — a
        ``journey.updated`` event, and a ``journey.completed`` event for each
        journey that reached a terminal state. Emitting after commit means we
        never broadcast state that failed to persist.
        """
        new_events = self.add(logs)
        touched = _distinct_journeys(new_events)
        # Parents before children: the journeys rows must exist before
        # journey_events (FK journey_events.journey_id -> journeys.journey_id),
        # otherwise the event insert raises a ForeignKeyViolationError.
        await self._upsert_journeys(session, touched)
        await self._persist_events(session, new_events)
        completions = self.evaluate(now)
        for completion in completions:
            await self._finalize_journey(session, completion)
        await session.commit()

        if on_event is not None:
            # A journey that completed in this chunk is in ``_completed`` now, so
            # it gets a completed event, not an updated one.
            for journey in touched:
                if journey.journey_id not in self._completed:
                    await on_event(_journey_updated_event(journey))
            for completion in completions:
                await on_event(_journey_completed_event(completion))
        return completions

    async def sweep_stalled(
        self,
        session,
        now: datetime | None = None,
        on_event: OnEvent | None = None,
    ) -> list[Completion]:
        """Finalize journeys that have crossed the stall timeout since last seen.

        Emits a ``journey.completed`` event (TIMED_OUT) per finalized journey
        when ``on_event`` is given.
        """
        completions = self.evaluate(now)
        for completion in completions:
            await self._finalize_journey(session, completion)
        if completions:
            await session.commit()

        if on_event is not None:
            for completion in completions:
                await on_event(_journey_completed_event(completion))
        return completions

    @staticmethod
    async def _persist_events(session, new_events) -> None:
        if not new_events:
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from backend.db import JourneyEvent

        rows = [
            {
                "journey_id": journey.journey_id,
                "log_id": log.log_id,
                "ts": log.timestamp,
                "raw": log.model_dump(mode="json"),
            }
            for journey, log in new_events
        ]
        stmt = pg_insert(JourneyEvent).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["log_id"])
        await session.execute(stmt)

    @staticmethod
    async def _upsert_journeys(session, journeys) -> None:
        """Insert in-progress journey rows; on re-touch, refresh span + aliases.

        Never overwrites ``status`` / ``outcome`` here — those are set at insert
        (IN_PROGRESS) and by :meth:`_finalize_journey`, so a late log arriving
        for an already-finalized journey cannot revert it to in-progress.
        """
        if not journeys:
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from backend.db import Journey

        for journey in journeys:
            stmt = pg_insert(Journey).values(
                journey_id=journey.journey_id,
                status=JourneyStatus.IN_PROGRESS.value,
                first_ts=journey.first_ts,
                last_ts=journey.last_ts,
                event_id=journey.event_id,
                order_id=journey.order_id,
                cart_header_id=journey.cart_header_id,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["journey_id"],
                set_={
                    "first_ts": journey.first_ts,
                    "last_ts": journey.last_ts,
                    "event_id": journey.event_id,
                    "order_id": journey.order_id,
                    "cart_header_id": journey.cart_header_id,
                },
            )
            await session.execute(stmt)

    @staticmethod
    async def _finalize_journey(session, completion: Completion) -> None:
        from sqlalchemy import update
        from backend.db import Journey

        journey = completion.journey
        stmt = (
            update(Journey)
            .where(Journey.journey_id == completion.journey_id)
            .values(
                status=completion.status.value,
                outcome=completion.outcome,
                first_ts=journey.first_ts,
                last_ts=journey.last_ts,
                event_id=journey.event_id,
                order_id=journey.order_id,
                cart_header_id=journey.cart_header_id,
            )
        )
        await session.execute(stmt)


def _as_list(logs) -> list[LogLine]:
    return list(logs)


def _distinct_journeys(new_events) -> list[StitchedJourney]:
    seen: set[str] = set()
    out: list[StitchedJourney] = []
    for journey, _log in new_events:
        if journey.journey_id not in seen:
            seen.add(journey.journey_id)
            out.append(journey)
    return out


# --- WebSocket event builders ------------------------------------------------
# The "data" payloads use the API response schemas (backend/schemas.py) so the
# dashboard sees exactly the REST shape. Imports are lazy to keep this module's
# import path free of the web layer.


def _journey_updated_event(journey: StitchedJourney) -> dict:
    """A ``journey.updated`` envelope for an in-progress journey (a grown chunk)."""
    from backend.schemas import JourneyOut
    from backend.ws import EVENT_JOURNEY_UPDATED, make_event

    data = JourneyOut(
        journey_id=journey.journey_id,
        status=JourneyStatus.IN_PROGRESS.value,
        outcome=None,
        first_ts=journey.first_ts,
        last_ts=journey.last_ts,
        event_id=journey.event_id,
        order_id=journey.order_id,
        cart_header_id=journey.cart_header_id,
        summary=None,
    ).model_dump(mode="json")
    return make_event(EVENT_JOURNEY_UPDATED, data)


def _journey_completed_event(completion: Completion) -> dict:
    """A ``journey.completed`` envelope (full detail incl. summary + events)."""
    from backend.schemas import JourneyDetailOut, JourneyEventOut
    from backend.ws import EVENT_JOURNEY_COMPLETED, make_event

    journey = completion.journey
    events = [
        JourneyEventOut(log_id=log.log_id, ts=log.timestamp, raw=log.model_dump(mode="json"))
        for log in journey.logs
    ]
    data = JourneyDetailOut(
        journey_id=journey.journey_id,
        status=completion.status.value,
        outcome=completion.outcome,
        first_ts=journey.first_ts,
        last_ts=journey.last_ts,
        event_id=journey.event_id,
        order_id=journey.order_id,
        cart_header_id=journey.cart_header_id,
        # The LLM summary is fetched separately on completion (AI service); it is
        # null here until that wiring populates it. The field is always present.
        summary=None,
        events=events,
    ).model_dump(mode="json")
    return make_event(EVENT_JOURNEY_COMPLETED, data)
