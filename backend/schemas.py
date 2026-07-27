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
from typing import Annotated, Generic, TypeVar

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


# --- pagination envelope -----------------------------------------------------


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """One page of a cursor-paginated listing (see ``backend/pagination.py``).

    ``next_cursor`` is an opaque token to pass back as ``?cursor=`` for the
    following page; ``None`` means this was the last page.
    """

    items: list[T]
    next_cursor: str | None = None


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
    severity: str | None = None
    confidence: float | None = None
    source: str
    # True when the AI service served this from its semantic cache. Modifies
    # source="ai" (the answer is still AI-authored, just reused); never set on a
    # fallback.
    #
    # ``None`` is coerced to False rather than rejected: the column's default is
    # DB-side, so an ``Alert`` serialized before it is flushed (the ``alert.new``
    # WebSocket envelope does exactly this) still reads None. Absent/None both
    # mean "not a cache hit", which is the correct reading for every such row.
    cached: Annotated[bool, BeforeValidator(lambda v: False if v is None else v)] = False
    journey_id: str | None = None
    is_resolved: bool = False
    resolved_at: UtcDatetime | None = None


# --- chat (proxied to the AI service, behind this API's auth) ----------------


class ChatContext(BaseModel):
    """Scope a question to one record — the "ask about this journey/alert" button.

    The backend resolves ``(kind, id)`` against its own DB and prepends that
    record's text to the question, so the answer is anchored to what the agent is
    looking at instead of whatever the query happens to retrieve.
    """

    kind: str                          # "alert" | "journey"
    id: str


class ChatRequest(BaseModel):
    query: str
    k: int = 5
    filters: dict | None = None
    context: ChatContext | None = None


class ChatSource(BaseModel):
    """One cited incident record, with a dashboard link when one can be built."""

    id: str
    kind: str
    score: float
    snippet: str
    # DASHBOARD_URL + journey_id/order_id; None when DASHBOARD_URL is unset or the
    # record carries no id to link to (same rule as the Teams card's link).
    link: str | None = None


class ChatCoverage(BaseModel):
    """How much of the history the answer drew on (computed by the AI service).

    ``truncated`` means retrieval hit its limit, so other matching incidents very
    likely exist beyond the ones cited. The dashboard should surface this on
    counting questions ("2 orders failed…") — an answer built from a capped sample
    otherwise reads as a complete history. See ``ai_service/api.py::ChatCoverage``
    for why this is a field rather than a sentence in the answer.
    """

    shown: int = 0
    limit: int = 0
    truncated: bool = False


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]
    mode: str                          # "ai" | "retrieval-only"
    coverage: ChatCoverage = ChatCoverage()


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


class AlertFacets(BaseModel):
    """``GET /alerts/facets`` — how many alerts each filter value would match.

    One ``{value: count}`` map per multi-select filter. Each map is computed with
    every active filter EXCEPT its own (the exclude-self rule in
    ``build_alert_facet_query``), so the counts show what selecting a value would
    give you rather than collapsing to what is already selected.

    A value with no matches is simply absent — the maps are sparse, and a client
    should read a missing key as 0. Null values are never counted: they mean the
    LLM never rated/routed the alert, and there is no filter option for them.
    """

    severity: dict[str, int]
    department: dict[str, int]
    app_name: dict[str, int]


# --- insights aggregation (GET /stats/insights) -------------------------------
#
# Assembled by backend/stats.py from GROUP BY results. The breakdown dicts are
# open-ended maps rather than fixed fields on purpose: a new department /
# severity / outcome shows up without a schema change, and nullable columns get
# an explicit bucket key ("unassigned"/"unrated"/...) so every dict sums back to
# its ``total``.


class JourneyStats(BaseModel):
    """Journey-side counters. ``success_rate`` is over *finished* journeys only
    (SUCCESS/FAILED/TIMED_OUT), and is 0.0 when none have finished yet.
    ``avg_duration_seconds`` is null when no finished journey has both
    timestamps.
    """

    total: int
    by_status: dict[str, int]
    by_outcome: dict[str, int]
    success_rate: float
    avg_duration_seconds: float | None


class AlertStats(BaseModel):
    """Alert-side counters. ``open`` + ``resolved`` == ``total``."""

    total: int
    open: int
    resolved: int
    by_department: dict[str, int]
    by_severity: dict[str, int]
    by_level: dict[str, int]
    by_source: dict[str, int]


class OverviewStats(BaseModel):
    """The ``GET /stats/insights`` payload — the dashboard insights page."""

    journeys: JourneyStats
    alerts: AlertStats
