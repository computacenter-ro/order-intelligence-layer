"""[5] Core Backend — incident clustering (repo-root
incident-clustering-implementation-plan.md is the design source; read it for
the "why" behind every rule here).

Collapses one order's many alerts into a single incident, and — for
infrastructure-class failures only — groups the same failure across different
orders into one systemic incident. Two layers, mirroring backend/journeys.py's
own split:

* **Pure decision functions** — causal-line selection, signature building,
  INFRA-vs-order-specific classification, the cosine + salient-token veto.
  No DB, fully unit-testable with plain objects.
* **DB-touching orchestration** (:func:`assign_incident`,
  :func:`process_completion`, :func:`sweep_stale_incidents`) — a thin layer
  over the pure decisions above.

``backend/journeys.py`` is NOT re-run for classification here. The failure
``subtype`` comes from the journey's OWN completion outcome
(``Completion.status``/``outcome``), passed in by ``backend/consumers.py`` —
never re-derived. ``_FAILURE_RULES`` stays exactly as-is; an unrecognized
failure (a ``TIMED_OUT`` journey) just routes to this module's novel/embedding
path instead of being excluded. (``JourneyStatus`` is still imported where the
status enum is needed.)
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# --- config -------------------------------------------------------------------

# Deliberately generous — a safety net, not the primary closing mechanism
# (manual dashboard resolve is primary; see backend/incidents.py's docstring
# and the source spec §4 step 7).
INCIDENT_QUIET_TIMEOUT = int(os.getenv("INCIDENT_QUIET_TIMEOUT", "1800"))

# Cosine-similarity floor for the novel (unrecognized) path's incident match.
INCIDENT_COSINE_THRESHOLD = float(os.getenv("INCIDENT_COSINE_THRESHOLD", "0.95"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- causal-line selection (pure) ---------------------------------------------


@dataclass(frozen=True)
class CausalCandidate:
    """The minimal shape :func:`pick_causal_line` needs from an alert.

    Decouples the pure selection logic from the ORM ``Alert`` class so it's
    unit-testable without a database (mirrors backend/journeys.py's
    ``LogLine``-based pure functions).
    """

    alert_id: str
    level: str
    message: str
    logger: str
    app_name: str
    department: str | None
    embedding: list[float] | None


def pick_causal_line(candidates: list[CausalCandidate]) -> CausalCandidate | None:
    """The journey's causal line: first ERROR among its alerts, else first WARN.

    ``candidates`` must already be ordered (by ``emitted_at`` — see
    :func:`_fetch_causal_candidates`) and already suppression-filtered (a
    suppressed WARN/ERROR never became an alert in the first place, so there's
    no separate suppression check to do here). Every one of the 10 canonical
    scenarios has a real ERROR from the specific failing component logged
    BEFORE the shared generic orchestrator abort line — so "first ERROR"
    naturally lands on the real cause, never the generic wrapper, even though
    that wrapper is also ERROR-level in several scenarios.
    """
    for candidate in candidates:
        if candidate.level == "ERROR":
            return candidate
    for candidate in candidates:
        if candidate.level == "WARN":
            return candidate
    return None


# --- signature building (pure) -------------------------------------------------

# Maps a substring of the causal line's logger to the failing component name
# (source spec §5's causal-line table, verified against the actual mock
# service code — pipeline/services/{spt,jam,outbound_osw,checker,validator}.py).
_SERVICE_LOGGER_MARKERS: tuple[tuple[str, str], ...] = (
    ("SptClient", "SPT"),
    ("JamClient", "JAM"),
    ("SapRfcClient", "SAP"),
    ("MarginCheckService", "checker"),
    ("ValidateOrderLineUdfFields", "validator"),
    ("TransformService", "inbound-transform"),
    ("OrderCreationService", "BM-DB/creation"),
)


def failing_service_from(logger: str, app_name: str) -> str:
    """The failing component, from the causal alert's logger.

    Falls back to ``app_name`` for a logger this table doesn't recognize —
    the novel-path case, where the specific component is unknown but the
    service that emitted the log is still a useful label.
    """
    for marker, service in _SERVICE_LOGGER_MARKERS:
        if marker in logger:
            return service
    return app_name


# Specific error tokens mined from the causal message (source spec §4 step 3).
# Order matters only in that the first match wins; these are mutually
# exclusive substrings in practice.
_ERROR_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"SocketTimeoutException"),
    re.compile(r"RFC_COMMUNICATION_FAILURE"),
    re.compile(r"SQLTimeoutException"),
)


def mine_error_token(message: str) -> str | None:
    """The specific error token in ``message``, or ``None``.

    Mined for DISPLAY on the incident card only — NOT part of the signature
    hash, and NOT used for classification (that is subtype-based now; see
    :func:`classify_infra_or_order_specific`). See this plan's Global
    Constraints and the source spec's open question #5.
    """
    for pattern in _ERROR_TOKEN_PATTERNS:
        match = pattern.search(message)
        if match:
            return match.group(0)
    return None


@dataclass(frozen=True)
class Signature:
    """A cause fingerprint for a journey's failure.

    ``failure_subtype`` is the journey's OWN completion outcome (passed in),
    never re-derived here. ``digest`` is ``None`` when ``failure_subtype`` is
    ``None`` — a genuinely novel FAILED cause or any TIMED_OUT journey (the
    journey never resolved to a recognized subtype). ``digest is None`` is
    exactly the novel/embedding-path trigger.
    """

    failure_subtype: str | None
    failing_service: str
    error_token: str | None
    digest: str | None


def build_signature(
    failure_subtype: str | None, logger: str, app_name: str, message: str
) -> Signature:
    """Build a :class:`Signature`.

    ``failure_subtype`` is the journey's OWN classification result, passed in by
    the caller (:func:`process_completion`) — ``completion.outcome`` when
    ``status is FAILED``, else ``None`` (TIMED_OUT / unrecognized). We do NOT
    re-run ``classify_failure``: the journey mechanism already matched the
    terminal line, and re-classifying the *upstream* causal line would just
    return ``None`` and misroute recognized failures to the novel path.
    ``failing_service`` is read from the causal line's ``logger``; ``message``
    is used only to mine the display ``error_token``.
    """
    failing_service = failing_service_from(logger, app_name)
    error_token = mine_error_token(message)
    digest = None
    if failure_subtype is not None:
        raw = f"{failure_subtype}:{failing_service}"
        digest = hashlib.sha256(raw.encode()).hexdigest()
    return Signature(
        failure_subtype=failure_subtype,
        failing_service=failing_service,
        error_token=error_token,
        digest=digest,
    )


# --- INFRA vs order-specific classification (pure) -----------------------------

# The failure's OWN subtype decides its class — semantic and correct even when
# the causal line's LOGGER is misleading. AUTH_FAILED is order-specific (one
# user's account is disabled) even though its causal line is JamClient, a
# dependency-CLIENT logger that a naive "client logger => infra" rule would
# wrongly call infrastructure. So we classify on the subtype, not the raw logger.
_INFRA_SUBTYPES = frozenset({
    "ENRICHMENT_FAILED",        # a dependency (SPT/RSM/...) never responded
    "ORDER_CREATION_FAILED",    # BM-DB connection timeout
    "SAP_SUBMISSION_FAILED",    # SAP RFC not reached
})
_ORDER_SPECIFIC_SUBTYPES = frozenset({
    "MARGIN_CHECK_FAILED",      # this order's margin
    "VALIDATION_FAILED",        # this order's UDF/data
    "AUTH_FAILED",              # this user's account (dependency responded 403)
    "INBOUND_TRANSFORM_FAILED", # this order's unknown product/SKU
})

# Fallback ONLY for the novel path (subtype is None): the message SHAPE.
_INFRA_MESSAGE_MARKERS = (
    "sockettimeoutexception", "rfc_communication_failure", "sqltimeoutexception",
    "service unavailable", "connection refused", "circuit",
)


def classify_infra_or_order_specific(failure_subtype: str | None, message: str) -> str:
    """INFRA (spans orders) vs order-specific (stays per-order).

    Primary signal is the journey's OWN ``failure_subtype`` — semantic and
    correct even when the causal logger is misleading (``AUTH_FAILED`` is
    order-specific though its logger, ``JamClient``, looks like an infra client).

    For the novel path (``failure_subtype is None``) there is no subtype to map,
    so fall back to the message SHAPE: timeout/connection/5xx-shaped -> INFRA,
    everything else -> order-specific. A message matching neither shape DEFAULTS
    to order-specific, never INFRA: at worst a genuine cross-order outage with an
    unrecognized shape fragments into several per-order incidents (noisy, not
    misleading); the reverse default risks an immediate false merge of unrelated
    orders, implying a shared root cause that doesn't exist.
    """
    if failure_subtype in _INFRA_SUBTYPES:
        return "infra"
    if failure_subtype in _ORDER_SPECIFIC_SUBTYPES:
        return "order_specific"
    text = message.lower()
    if any(m in text for m in _INFRA_MESSAGE_MARKERS):
        return "infra"
    if re.search(r"\btimeout\b|\bconnect(ion)?\b", text) or re.search(r"\b5\d\d\b", text):
        return "infra"
    return "order_specific"


# --- cosine + salient-token veto (pure) -----------------------------------------
# Small, self-contained duplicates of ai_service/semcache.py's cosine/diverges
# logic — see this plan's Global Constraints for why these aren't imported
# from ai_service instead.

_WORD = re.compile(r"[A-Za-z]+|\S*\d\S*")

_SALIENT_WORDS = frozenset({
    "failed", "failure", "succeeded", "success", "passed", "pass", "aborted",
    "abort", "blocked", "denied", "deny", "rejected", "reject", "timeout",
    "timed", "unavailable", "unauthorized", "forbidden", "retry", "retrying",
    "final", "not", "no", "none", "unable", "cannot", "missing", "invalid",
    "disabled", "enabled", "up", "down",
})

# Mirrors ai_service/semcache.py's own id-masking (`_MASKS`/`normalize()`) —
# duplicated for the same cross-process reason as cosine/diverges themselves
# (see this plan's Global Constraints). Without masking first, two causal
# lines that differ ONLY by which order/account/cart they belong to would
# register as "diverged" purely because their order/account digits differ —
# defeating cross-order matching for exactly the case it exists to serve.
_ID_MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<EVT>", re.compile(r"evt-[0-9a-f-]{8,}")),
    ("<ORD>", re.compile(r"\bORD-\d+\b")),
    ("<CART>", re.compile(r"\b\d{19}\b")),
    ("<ACC>", re.compile(r"\b\d{6,}\b")),
)


def _mask_ids(message: str) -> str:
    """Mask volatile order/event/cart/account ids so two causal lines from
    different orders compare on CAUSE, not identity."""
    text = message or ""
    for token, pattern in _ID_MASKS:
        text = pattern.sub(token, text)
    return text


def _salient_tokens(text: str) -> frozenset[str]:
    """Meaning-bearing tokens: any token containing a digit (counters,
    percentages, status codes), plus any configured outcome/polarity word.

    ``text`` must already be id-masked (see :func:`_mask_ids`) — otherwise a
    differing order/account/cart id would itself count as a salient token.
    """
    tokens: set[str] = set()
    for match in _WORD.finditer(text):
        tok = match.group(0).lower()
        if any(ch.isdigit() for ch in tok):
            tokens.add(tok)
        elif tok in _SALIENT_WORDS:
            tokens.add(tok)
    return frozenset(tokens)


def diverges(message_a: str, message_b: str) -> bool:
    """True if the two causal messages disagree on any salient token — the
    veto that rejects a cosine match a general-purpose embedding scored high
    despite a meaning flip (e.g. "succeeded" vs "failed"). Masks ids first
    (:func:`_mask_ids`) so two messages differing only by order/account/cart
    id are never treated as diverged."""
    return _salient_tokens(_mask_ids(message_a)) != _salient_tokens(_mask_ids(message_b))


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0 if either is zero)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# --- incident title (pure) ------------------------------------------------------


def build_title(signature: Signature) -> str:
    """A deterministic, no-LLM incident title — reads correctly even with the
    Azure LLM breaker open."""
    subtype = signature.failure_subtype or "Unrecognized failure"
    return f"{subtype} — {signature.failing_service}"


# --- assign_incident: match-or-create (DB) --------------------------------------


async def _find_open_incident_by_signature(session, digest: str):
    from sqlalchemy import select
    from backend.db import Incident

    result = await session.execute(
        select(Incident).where(Incident.status == "open", Incident.signature == digest)
    )
    return result.scalars().first()


async def _find_open_incident_by_cosine(session, *, failing_service: str, causal: CausalCandidate):
    """The novel path (source spec §4 step 5): among OPEN incidents with no
    exact signature (novel-path incidents only) that share the same
    failing_service, find the closest cosine match above the threshold — and
    veto it if the two causal messages disagree on a salient token."""
    from sqlalchemy import select
    from backend.db import Alert, Incident

    if causal.embedding is None:
        return None
    result = await session.execute(
        select(Incident, Alert)
        .join(Alert, Alert.alert_id == Incident.primary_alert_id)
        .where(
            Incident.status == "open",
            Incident.signature.is_(None),
            Incident.failing_service == failing_service,
        )
    )
    best_incident, best_primary_message, best_sim = None, None, -1.0
    for incident, primary_alert in result.all():
        if primary_alert.embedding is None:
            continue
        sim = _cosine(causal.embedding, primary_alert.embedding)
        if sim > best_sim:
            best_incident, best_primary_message, best_sim = incident, primary_alert.message, sim
    if best_incident is None or best_sim < INCIDENT_COSINE_THRESHOLD:
        return None
    if diverges(causal.message, best_primary_message):
        return None
    return best_incident


async def assign_incident(session, *, signature: Signature, infra_class: str,
                           causal: CausalCandidate, now: datetime):
    """Find an OPEN incident to join, or create a new one.

    Order-specific causal lines NEVER search — they always create their own
    incident, permanently scoped to this one journey this iteration (no
    escalation path yet — see the source spec's §12 Future Improvements).

    Does NOT link alerts/journeys or bump alert_count/journey_count —
    :func:`process_completion` does that once, uniformly, for both the match
    and create cases, so a count is never bumped twice.
    """
    from backend.db import Incident

    incident = None
    if infra_class == "infra":
        if signature.digest is not None:
            incident = await _find_open_incident_by_signature(session, signature.digest)
        else:
            incident = await _find_open_incident_by_cosine(
                session, failing_service=signature.failing_service, causal=causal
            )

    if incident is not None:
        return incident

    incident = Incident(
        incident_id=str(uuid.uuid4()),
        signature=signature.digest,
        failure_subtype=signature.failure_subtype,
        failing_service=signature.failing_service,
        error_token=signature.error_token,
        title=build_title(signature),
        department=causal.department,
        status="open",
        first_ts=now,
        last_ts=now,
        primary_alert_id=causal.alert_id,
        alert_count=0,
        journey_count=0,
    )
    session.add(incident)
    await session.flush()  # so incident.incident_id is set before the caller FKs to it
    return incident


# --- process_completion: the main entry point (DB) ------------------------------


async def _fetch_causal_candidates(session, journey_id: str) -> list[CausalCandidate]:
    """This journey's alerts, ordered by ``emitted_at`` — the only per-alert
    timestamp the ``alerts`` table stores (the original log's own timestamp
    isn't persisted on ``alerts``, only on the ``journey_events`` it produced).
    """
    from sqlalchemy import select
    from backend.db import Alert

    result = await session.execute(
        select(Alert).where(Alert.journey_id == journey_id).order_by(Alert.emitted_at.asc())
    )
    return [
        CausalCandidate(
            alert_id=a.alert_id, level=a.level, message=a.message,
            logger=a.logger, app_name=a.app_name, department=a.department,
            embedding=a.embedding,
        )
        for a in result.scalars().all()
    ]


async def process_completion(session, completion, *, now: datetime | None = None):
    """Cluster one journey's completion into an incident, or return ``None``
    if ineligible (source spec §4 Eligibility):

    * only ``FAILED`` and ``TIMED_OUT`` journeys are eligible at all;
    * a ``TIMED_OUT`` journey additionally needs a real linked ERROR alert
      (its causal line can't be a WARN-only fallback — there's no recognized
      terminal marker to anchor a "this timed out because X" story on).

    Idempotent on ``journey_id``: a journey whose ``incident_id`` is already
    set is a no-op (handles at-least-once redelivery / a re-evaluated
    completion safely).
    """
    from sqlalchemy import select, update
    from backend.db import Alert, Journey
    from backend.journeys import JourneyStatus

    now = now or _utcnow()

    if completion.status not in (JourneyStatus.FAILED, JourneyStatus.TIMED_OUT):
        return None

    result = await session.execute(
        select(Journey.incident_id).where(Journey.journey_id == completion.journey_id)
    )
    row = result.first()
    if row is None or row[0] is not None:
        return None  # journey row missing, or already clustered — no-op

    candidates = await _fetch_causal_candidates(session, completion.journey_id)
    causal = pick_causal_line(candidates)
    if causal is None:
        return None  # no WARN/ERROR alert at all — nothing to anchor on
    if completion.status is JourneyStatus.TIMED_OUT and causal.level != "ERROR":
        return None  # TIMED_OUT needs a real ERROR (Eligibility, above)

    # subtype from the journey's OWN classification — recognized only when
    # FAILED; TIMED_OUT / unrecognized -> None -> the novel/embedding path.
    failure_subtype = (
        completion.outcome if completion.status is JourneyStatus.FAILED else None
    )
    signature = build_signature(
        failure_subtype, causal.logger, causal.app_name, causal.message
    )
    infra_class = classify_infra_or_order_specific(failure_subtype, causal.message)

    incident = await assign_incident(
        session, signature=signature, infra_class=infra_class, causal=causal, now=now
    )

    result = await session.execute(
        select(Alert).where(Alert.journey_id == completion.journey_id)
    )
    journey_alerts = result.scalars().all()
    for alert in journey_alerts:
        alert.incident_id = incident.incident_id
    incident.alert_count += len(journey_alerts)
    incident.journey_count += 1
    incident.last_ts = now

    await session.execute(
        update(Journey)
        .where(Journey.journey_id == completion.journey_id)
        .values(incident_id=incident.incident_id)
    )
    await session.commit()
    return incident


# --- lifecycle: quiet-timeout sweep (DB) -----------------------------------------


async def sweep_stale_incidents(session, now: datetime | None = None) -> list:
    """Close OPEN incidents that have had no new member for
    ``INCIDENT_QUIET_TIMEOUT`` seconds.

    This is the safety-net fallback closing mechanism (source spec §4 step 7)
    — manual dashboard resolve is the PRIMARY one and is a REST write
    endpoint, not implemented here (Plan 2). No automatic "recovery" signal of
    any kind closes an incident; this sweep is the only automatic closer.
    """
    from sqlalchemy import select
    from backend.db import Incident

    now = now or _utcnow()
    cutoff = now - timedelta(seconds=INCIDENT_QUIET_TIMEOUT)
    result = await session.execute(
        select(Incident).where(Incident.status == "open", Incident.last_ts < cutoff)
    )
    stale = result.scalars().all()
    for incident in stale:
        incident.status = "resolved"
    if stale:
        await session.commit()
    return stale
