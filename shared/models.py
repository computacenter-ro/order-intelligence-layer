"""Wire-format contracts shared across every subsystem.

See docs/superpowers/specs/2026-07-16-shared-models-design.md for the design
rationale, and CLAUDE.md for the authoritative log schema / correlation model.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

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


class Baton(BaseModel):
    """Control message that hands off "your turn to emit" between mock services."""

    model_config = ConfigDict(extra="forbid")

    flow_id: str
    scenario: int
    steps: list[tuple[str, str]]
    cursor: int = 0
    ctx: BatonContext


class Department(str, Enum):
    networking = "networking"
    devops = "devops"
    backend = "backend"
    database = "database"
    general = "general"


class ProcessedAlert(BaseModel):
    """Contract on the `processed.alerts` queue."""

    model_config = ConfigDict(extra="forbid")

    alert_id: str
    emitted_at: AwareDatetime
    log: LogLine
    explanation: str | None
    department: Department | None
    confidence: float | None = Field(default=None, ge=0, le=1)
    source: Literal["ai", "fallback"]
