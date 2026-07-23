"""Per-service identity profiles — the "big project" voice for mock emitters.

Every mock service must stamp its log lines with the *real* identifiers the
corresponding service produces in the actual order pipeline: the ``app_name``,
the ``host`` it runs on, its ``process_id``, its thread-pool naming, and the
Java-style ``logger`` names. Those values are **not** invented here — they are
lifted verbatim from ``data/mock-order-flows-v2.json`` (the reference dataset
captured from the real system), so emitted logs look like they came from the
big project.

This module is the single place that knowledge lives. Emitter modules
(``services/inbound.py`` etc.) build ``LogLine``s through :func:`make_log`,
passing only the *logger* and *thread* they want; the profile supplies
``app_name`` / ``host`` / ``process_id`` and mints ``log_id`` / ``timestamp``.

See CLAUDE.md "[1] Mock Services" service table and the log schema.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.models import Level, LogLine

# --- Service identity ---------------------------------------------------------


@dataclass(frozen=True)
class ServiceProfile:
    """Fixed identity of one mock service (app_name, host, process_id).

    The ``threads`` tuple lists the real thread names seen for this service in
    the reference dataset; ``default_thread`` is the one used unless a block
    asks for a specific one (e.g. order_engine's phase-1 create-listener vs its
    phase-2 worker pool).
    """

    service: str          # the scenarios.py stem (baton step[0]) — e.g. "inbound"
    app_name: str         # the log app_name — e.g. "cc-inbound-service"
    host: str
    process_id: str
    default_thread: str


# Keyed by the scenarios.py service stem (first element of a step tuple).
# app_name / host / process_id / threads all come from mock-order-flows-v2.json.
PROFILES: dict[str, ServiceProfile] = {
    "inbound": ServiceProfile(
        "inbound", "cc-inbound-service", "CCECMETLT001", "7412", "rabbit-listener-1"
    ),
    "order_engine": ServiceProfile(
        "order_engine", "cc-order-engine", "CCECMEWEBT001", "9201", "order-create-listener-1"
    ),
    "spt": ServiceProfile(
        "spt", "cc-spt-service", "CCECMSRVT001", "6340", "http-nio-8080-exec-8"
    ),
    "rsm": ServiceProfile(
        "rsm", "cc-rsm-service", "CCECMSRVT001", "6341", "http-nio-8080-exec-8"
    ),
    "settings": ServiceProfile(
        "settings", "cc-settings-service", "CCECMSRVT002", "5890", "http-nio-8080-exec-2"
    ),
    "jam": ServiceProfile(
        "jam", "cc-jam-service", "CCECMSRVT001", "6342", "http-nio-8080-exec-1"
    ),
    "checker": ServiceProfile(
        "checker", "cc-checker-service", "CCECMSRVT002", "5891", "http-nio-8080-exec-1"
    ),
    # The validator also emits the Avalara satellite logs in the real system
    # (logger c.c.validator.client.AvalaraClient) — Avalara is not its own
    # service. See services/validator.py.
    "validator": ServiceProfile(
        "validator", "cc-validator-service", "CCECMSRVT002", "5892", "http-nio-8080-exec-4"
    ),
    "outbound_osw": ServiceProfile(
        "outbound_osw", "cc-outbound-osw", "CCECMEWEBT002", "9202", "pool-3-thread-2"
    ),
    "track_trace": ServiceProfile(
        "track_trace", "cc-track-trace", "CCECMEWEBT002", "9203", "pool-3-thread-3"
    ),
}


# The order_engine's phase-2 enrichment/orchestration runs on a worker pool
# thread rather than the create-listener. Any of these is realistic; a block
# may pick one so a flow's phase-2 lines share a thread (as in the fixture).
ORDER_ENGINE_WORKER_THREADS: tuple[str, ...] = (
    "pool-3-thread-1",
    "pool-3-thread-2",
    "pool-3-thread-3",
    "pool-3-thread-4",
)


def profile(service: str) -> ServiceProfile:
    """The :class:`ServiceProfile` for a scenarios.py service stem."""
    try:
        return PROFILES[service]
    except KeyError as exc:  # pragma: no cover - guards service/profile drift
        raise KeyError(
            f"no ServiceProfile for service {service!r} - add it to "
            f"pipeline/services/profiles.py PROFILES"
        ) from exc


# --- Log construction ---------------------------------------------------------


def _now_iso() -> datetime:
    """A real, timezone-aware UTC timestamp (CLAUDE.md: never naive/utcnow)."""
    return datetime.now(timezone.utc)


def make_log(
    prof: ServiceProfile,
    *,
    logger: str,
    level: Level,
    message: str,
    thread: str | None = None,
    eventId: str | None = None,
    orderId: str | None = None,
    cartHeaderId: str | None = None,
    accountNumber: str | None = None,
) -> LogLine:
    """Build one :class:`LogLine` stamped with ``prof``'s identity.

    ``log_id`` (unique UUID — the dedup key) and ``timestamp`` (real UTC now)
    are minted here so every emitted line is unique and ordered by real wall
    time. The *id fields* (``eventId``/``orderId``/``cartHeaderId``) are passed
    in by the caller, which is what keeps the correlation model honest: a block
    can only log an id that exists in ``baton.ctx`` at that moment.
    """
    return LogLine(
        log_id=str(uuid.uuid4()),
        timestamp=_now_iso(),
        app_name=prof.app_name,
        level=level,
        logger=logger,
        host=prof.host,
        process_id=prof.process_id,
        thread=thread or prof.default_thread,
        eventId=eventId,
        orderId=orderId,
        cartHeaderId=cartHeaderId,
        accountNumber=accountNumber,
        message=message,
    )
