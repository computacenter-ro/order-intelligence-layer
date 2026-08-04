"""Wire-format contracts shared across every subsystem.

See docs/superpowers/specs/2026-07-16-shared-models-design.md for the design
rationale, and CLAUDE.md for the authoritative log schema / correlation model.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_serializer

Level = Literal["DEBUG", "INFO", "WARN", "ERROR"]
BridgeIds = Literal["both", "order", "cart", "random"]


class LogLine(BaseModel):
    """One log line as emitted by a mock service and stored by the collector."""

    model_config = ConfigDict(extra="forbid")

    log_id: str
    timestamp: AwareDatetime
    app_name: str
    level: Level
    logger: str
    host: str
    process_id: str
    thread: str
    eventId: str | None = None
    orderId: str | None = None
    cartHeaderId: str | None = None
    accountNumber: str | None = None
    message: str

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime) -> str:
        utc_value = value.astimezone(timezone.utc)
        return utc_value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_value.microsecond // 1000:03d}Z"


class OrderLine(BaseModel):
    """One line item carried in the baton context."""

    model_config = ConfigDict(extra="forbid")

    productId: str
    sku: str | None = None


class BatonContext(BaseModel):
    """The `ctx` block of a Baton — flow state carried between mock services."""

    model_config = ConfigDict(extra="forbid")

    eventId: str
    accountNumber: str
    country: str
    user: str
    lines: list[OrderLine]
    orderId: str | None = None
    cartHeaderId: str | None = None
    bridge_ids: BridgeIds = "random"
    fail_at: str | None = None
    # A block that fails ONCE and then succeeds — the transient-blip knob,
    # deliberately separate from ``fail_at``. ``fail_at`` means "emit the failure
    # variant AND stop the chain" (shared/scenarios.py truncates on it); a flaky
    # block emits its retry lines and forwards the baton, so the flow runs to
    # completion. Keeping them apart is what leaves truncation untouched for
    # every terminal-failure scenario.
    flaky_at: str | None = None


class Baton(BaseModel):
    """Control message that hands off "your turn to emit" between mock services."""

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    scenario: int
    steps: list[tuple[str, str]]
    cursor: int = 0
    ctx: BatonContext


class Department(str, Enum):
    """The team an alert is routed to (``ai_service/nodes.py``'s router node).

    ``business`` was called ``general`` until the Teams channel split gave it its
    own ``business`` channel, at which point the old name was actively misleading:
    the dashboard rendered "General" for an alert whose card went to
    ``#business-logs``. It is also semantically empty, which is why the router
    prompt had to insist "the answer is general, whatever the log level" — a
    department that names what it MEANS makes the model's job easier.

    ``business`` = **not an engineering fault**: the pipeline worked exactly as
    designed and correctly *rejected* an order (margin below threshold, missing
    ``costCenter`` UDF, disabled JAM account, unmapped product). Nobody changes
    code. The load-bearing contrast is with ``backend``, which is an actual defect.

    ⚠ A channel is not a department — see ``_DEPARTMENT_CHANNELS`` in
    ``backend/teams.py``. There was once a ``general`` Teams channel that carried
    journey completions; those notifications were dropped, and the channel is now
    ``reports`` (``TEAMS_WEBHOOK_REPORTS``), carrying the twice-daily digest and
    nothing else. None of that is or ever was this department.
    """

    networking = "networking"
    devops = "devops"
    backend = "backend"
    database = "database"
    business = "business"


class Severity(str, Enum):
    """Per-log technical severity, judged by the router LLM from the log alone.

    A relative ranking of how urgent this single WARN/ERROR is for an IT-support
    engineer — NOT business impact (the log doesn't carry that). ``None`` on a
    ``ProcessedAlert`` means fallback (LLM down), exactly like department.
    """

    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


class ProcessedAlert(BaseModel):
    """Contract on the `processed.alerts` queue."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    emitted_at: AwareDatetime
    log: LogLine
    explanation: str | None
    department: Department | None
    severity: Severity | None = None
    source: Literal["ai", "fallback"]
    # True when this alert's explanation/routing was served from the AI
    # service's semantic cache (a reused AI answer) rather than a fresh LLM
    # call. ``source`` stays "ai" on a cache hit so backend routing is unchanged
    # — this flag is purely informational (dashboard/metrics).
    cached: bool = False
    # The masked-message vector for incident clustering (backend/incidents.py).
    # None when no encoder is configured (ai_service/semcache.py cache disabled)
    # — clustering's novel/embedding path just has nothing to compare, same as
    # any other missing optional signal.
    embedding: list[float] | None = None
