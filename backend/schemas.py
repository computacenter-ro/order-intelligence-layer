"""[5] Core Backend — Pydantic response schemas (wire contract).

The API returns these schemas, never the ORM models from ``backend/db.py``, so
the wire format is explicit and decoupled from the DB layer. Every datetime is
normalized to UTC + timezone-aware via :data:`UtcDatetime` (the system-wide
invariant): a ``BeforeValidator`` coerces any naive value to UTC rather than
leaking a naive timestamp.

Importing this module performs no I/O.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict


# --- datetime normalization --------------------------------------------------


def _to_utc(value: object) -> object:
    """Coerce a datetime to UTC + timezone-aware; pass through everything else."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


UtcDatetime = Annotated[datetime, BeforeValidator(_to_utc)]


# --- response schemas --------------------------------------------------------


class AlertOut(BaseModel):
    """One processed alert (``alerts`` row). Enrichment is null for fallback."""

    model_config = ConfigDict(from_attributes=True)

    alert_id: str
    emitted_at: UtcDatetime
    log_id: str
    level: str
    app_name: str
    logger: str
    message: str
    event_id: str | None = None
    order_id: str | None = None
    cart_header_id: str | None = None
    account_number: str | None = None
    explanation: str | None = None
    department: str | None = None
    confidence: float | None = None
    source: str
    journey_id: str | None = None


class JourneyOut(BaseModel):
    """A journey header (``journeys`` row); alias ids may be null."""

    model_config = ConfigDict(from_attributes=True)

    journey_id: str
    status: str
    outcome: str | None = None
    first_ts: UtcDatetime | None = None
    last_ts: UtcDatetime | None = None
    event_id: str | None = None
    order_id: str | None = None
    cart_header_id: str | None = None
    summary: str | None = None


class JourneyEventOut(BaseModel):
    """One raw log line belonging to a journey (``journey_events`` row)."""

    model_config = ConfigDict(from_attributes=True)

    log_id: str
    ts: UtcDatetime
    raw: dict


class JourneyDetailOut(JourneyOut):
    """A journey plus its ordered events (the ``GET /journeys/{id}`` payload)."""

    events: list[JourneyEventOut]
