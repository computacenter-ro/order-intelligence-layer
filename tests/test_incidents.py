"""Tests for backend/incidents.py — the incident-clustering engine.

Pure logic (causal-line selection, signature building, INFRA/order-specific
classification, the cosine + salient-token veto) is unit-tested with plain
objects, no DB — mirroring backend/journeys.py's own pure-detection tests.
DB-touching orchestration is tested with the project's established
_FakeSession/_FakeResult pattern (see tests/test_api.py, tests/test_linking.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.incidents import CausalCandidate, pick_causal_line


def _candidate(level: str, message: str, *, alert_id: str = "a1") -> CausalCandidate:
    return CausalCandidate(
        alert_id=alert_id, level=level, message=message,
        logger="c.c.test.Logger", app_name="cc-test-service",
        department=None, embedding=None,
    )


# --- pick_causal_line ---------------------------------------------------------
def test_first_error_wins_over_a_later_generic_abort():
    # Mirrors scenario 8 (spt.py): SptClient ERROR (attempt 1), WARN retry,
    # SptClient ERROR (attempt 2), WARN retry, SptClient ERROR (attempt 3),
    # then the generic OrderProcessingService abort ERROR — always last.
    candidates = [
        _candidate("ERROR", "SptClient timeout attempt 1", alert_id="a1"),
        _candidate("WARN", "Retrying... attempt 2/3", alert_id="a2"),
        _candidate("ERROR", "SptClient timeout attempt 2", alert_id="a3"),
        _candidate("WARN", "Retrying... attempt 3/3", alert_id="a4"),
        _candidate("ERROR", "SptClient timeout attempt 3", alert_id="a5"),
        _candidate("ERROR", "Order processing aborted for order ORD-1", alert_id="a6"),
    ]
    causal = pick_causal_line(candidates)
    assert causal.alert_id == "a1"  # the FIRST error, never the generic abort


def test_falls_back_to_first_warn_when_no_error():
    candidates = [
        _candidate("WARN", "Order blocked by margin check", alert_id="w1"),
    ]
    causal = pick_causal_line(candidates)
    assert causal.alert_id == "w1"


def test_none_when_no_warn_or_error_at_all():
    candidates = [
        CausalCandidate(
            alert_id="i1", level="INFO", message="just info",
            logger="l", app_name="a", department=None, embedding=None,
        )
    ]
    # INFO never becomes an alert in practice (the poller only alerts on
    # WARN/ERROR), but the function must still degrade gracefully rather than
    # crash if it's ever handed a non-alert-worthy candidate.
    assert pick_causal_line(candidates) is None


def test_none_for_empty_list():
    assert pick_causal_line([]) is None


from backend.incidents import Signature, build_signature, failing_service_from, mine_error_token


# --- failing_service_from ------------------------------------------------------
@pytest.mark.parametrize(
    "logger,expected",
    [
        ("c.c.orderengine.client.SptClient", "SPT"),
        ("c.c.orderengine.client.JamClient", "JAM"),
        ("c.c.outbound.client.SapRfcClient", "SAP"),
        ("c.c.checker.service.MarginCheckService", "checker"),
        ("c.c.validator.strategy.ValidateOrderLineUdfFields", "validator"),
        ("c.c.inbound.transform.TransformService", "inbound-transform"),
        ("c.c.orderengine.service.OrderCreationService", "BM-DB/creation"),
    ],
)
def test_failing_service_from_known_loggers(logger, expected):
    assert failing_service_from(logger, "cc-some-service") == expected


def test_failing_service_falls_back_to_app_name_for_unknown_logger():
    assert failing_service_from("c.c.some.NewComponent", "cc-new-service") == "cc-new-service"


# --- mine_error_token -----------------------------------------------------------
def test_mines_socket_timeout_exception():
    msg = "java.net.SocketTimeoutException: connect timed out (10014ms)"
    assert mine_error_token(msg) == "SocketTimeoutException"


def test_mines_rfc_communication_failure():
    msg = "[SapRfcClient#submitOrder] RFC_COMMUNICATION_FAILURE: connection refused"
    assert mine_error_token(msg) == "RFC_COMMUNICATION_FAILURE"


def test_mines_sql_timeout_exception():
    msg = "Failed to persist cart header: java.sql.SQLTimeoutException: timeout"
    assert mine_error_token(msg) == "SQLTimeoutException"


def test_no_token_returns_none():
    assert mine_error_token("Margin check FAILED for order X: overall margin 5% below threshold 15%") is None


# --- build_signature ------------------------------------------------------------
# subtype is now PASSED IN (from the journey's completion outcome), not derived.
def test_recognized_subtype_gets_a_digest():
    sig = build_signature(
        "ENRICHMENT_FAILED",
        "c.c.orderengine.client.SptClient",
        "cc-order-engine",
        "[SptClient#getSptPriceListCode] <--- ERROR SocketTimeoutException (10014ms)",
    )
    assert sig.failure_subtype == "ENRICHMENT_FAILED"
    assert sig.failing_service == "SPT"
    assert sig.digest is not None


def test_same_subtype_and_service_produce_the_same_digest_regardless_of_message():
    sig_a = build_signature(
        "ENRICHMENT_FAILED",
        "c.c.orderengine.client.SptClient", "cc-order-engine", "...attempt 1...",
    )
    sig_b = build_signature(
        "ENRICHMENT_FAILED",
        "c.c.orderengine.client.SptClient", "cc-order-engine", "...attempt 3...",
    )
    assert sig_a.digest == sig_b.digest


def test_none_subtype_has_no_digest():
    # subtype None = TIMED_OUT / unrecognized -> novel/embedding path.
    sig = build_signature(
        None,
        "c.c.some.NewComponent",
        "cc-new-service",
        "Something entirely new went wrong",
    )
    assert sig.failure_subtype is None
    assert sig.digest is None


from backend.incidents import classify_infra_or_order_specific


@pytest.mark.parametrize(
    "subtype",
    ["ENRICHMENT_FAILED", "ORDER_CREATION_FAILED", "SAP_SUBMISSION_FAILED"],
)
def test_infra_subtypes_classify_as_infra(subtype):
    assert classify_infra_or_order_specific(subtype, "any message") == "infra"


@pytest.mark.parametrize(
    "subtype",
    ["MARGIN_CHECK_FAILED", "VALIDATION_FAILED", "AUTH_FAILED", "INBOUND_TRANSFORM_FAILED"],
)
def test_order_specific_subtypes_classify_as_order_specific(subtype):
    # AUTH_FAILED is the important case: its causal line is JamClient (a
    # dependency-CLIENT logger), but the failure is one user's disabled account,
    # so it is order-specific. Classifying on the SUBTYPE gets this right; a
    # naive "client logger => infra" rule would get it wrong.
    assert classify_infra_or_order_specific(subtype, "any message") == "order_specific"


def test_novel_infra_shaped_message_classifies_as_infra():
    # subtype None (TIMED_OUT / unrecognized) -> fall back to message shape.
    assert classify_infra_or_order_specific(None, "Connection refused while calling downstream") == "infra"
    assert classify_infra_or_order_specific(None, "java.net.SocketTimeoutException: connect timed out") == "infra"


def test_novel_unknown_shape_defaults_to_order_specific():
    """The safe default (source spec §4 step 4b): at worst a genuine cross-order
    outage with an unrecognized shape fragments into several per-order incidents
    (noisy, not misleading) rather than a false merge of unrelated orders."""
    assert classify_infra_or_order_specific(None, "Something entirely unprecedented happened") == "order_specific"


from backend.incidents import _cosine, diverges


def test_cosine_identical_vectors_is_one():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_zero_vector_is_zero():
    assert _cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_diverges_false_for_identical_meaning():
    assert diverges(
        "SAP submission failed for order ORD-1",
        "SAP submission failed for order ORD-2",
    ) is False


def test_diverges_true_on_meaning_flip():
    # A near-identical pair a general embedding could score as very similar,
    # but "succeeded" vs "failed" must never be treated as the same cause.
    assert diverges(
        "SAP submission succeeded for order ORD-1",
        "SAP submission failed for order ORD-1",
    ) is True


def test_diverges_true_on_differing_retry_counter():
    assert diverges(
        "Retrying SPT price list call (attempt 2/3)",
        "Retrying SPT price list call (attempt 3/3)",
    ) is True


from backend.db import Alert, Incident
from backend.incidents import assign_incident, build_title


# --- fakes (project convention — see tests/test_api.py, tests/test_linking.py) -
class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _FakeResult:
    def __init__(self, *, items=None, rows=None):
        self._items = items or []
        self._rows = rows or []

    def scalars(self):
        return _FakeScalars(self._items)

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Returns pre-seeded results per execute() call, in order. Records adds."""

    def __init__(self, results):
        self._results = list(results)
        self.added = []

    async def execute(self, stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "incident_id", None) is None:
                obj.incident_id = "generated-id"

    async def commit(self):
        pass


NOW = datetime(2026, 7, 26, 8, 0, 0, tzinfo=timezone.utc)


def _causal(message: str, *, embedding=None) -> CausalCandidate:
    return CausalCandidate(
        alert_id="causal-1", level="ERROR", message=message,
        logger="c.c.orderengine.client.SptClient", app_name="cc-order-engine",
        department="devops", embedding=embedding,
    )


# --- build_title ----------------------------------------------------------------
def test_build_title_uses_subtype_and_service():
    sig = Signature(
        failure_subtype="ENRICHMENT_FAILED", failing_service="SPT",
        error_token="SocketTimeoutException", digest="d1",
    )
    assert build_title(sig) == "ENRICHMENT_FAILED — SPT"


def test_build_title_for_unrecognized_cause():
    sig = Signature(failure_subtype=None, failing_service="cc-new-service", error_token=None, digest=None)
    assert build_title(sig) == "Unrecognized failure — cc-new-service"


def test_build_title_uses_suggested_label_when_subtype_unrecognized():
    sig = Signature(failure_subtype=None, failing_service="cc-settings-service", error_token=None, digest=None)
    title = build_title(sig, suggested_label="Settings cache connection pool exhausted")
    assert title == "Settings cache connection pool exhausted — cc-settings-service"


def test_build_title_falls_back_when_suggested_label_is_none():
    sig = Signature(failure_subtype=None, failing_service="cc-settings-service", error_token=None, digest=None)
    assert build_title(sig, suggested_label=None) == "Unrecognized failure — cc-settings-service"


def test_build_title_ignores_suggested_label_for_a_recognized_subtype():
    # A named subtype never needs the LLM label — it already has a real name.
    sig = Signature(failure_subtype="ENRICHMENT_FAILED", failing_service="SPT", error_token=None, digest="d1")
    assert build_title(sig, suggested_label="whatever") == "ENRICHMENT_FAILED — SPT"


def test_build_title_sanitizes_a_stray_id_out_of_the_suggested_label():
    sig = Signature(failure_subtype=None, failing_service="cc-settings-service", error_token=None, digest=None)
    title = build_title(sig, suggested_label="Order ORD-6015 hit a settings timeout")
    assert "ORD-6015" not in title
    assert title == "Order hit a settings timeout — cc-settings-service"


def test_build_title_falls_back_when_label_sanitizes_to_nothing():
    sig = Signature(failure_subtype=None, failing_service="cc-settings-service", error_token=None, digest=None)
    assert build_title(sig, suggested_label="ORD-6015") == "Unrecognized failure — cc-settings-service"


# --- assign_incident: recognized + INFRA ----------------------------------------
async def test_recognized_infra_matches_existing_open_incident():
    existing = Incident(
        incident_id="inc-1", signature="d1", failure_subtype="ENRICHMENT_FAILED",
        failing_service="SPT", error_token=None, title="ENRICHMENT_FAILED — SPT",
        department="devops", status="open", first_ts=NOW, last_ts=NOW,
        primary_alert_id="a0", alert_count=4, journey_count=1,
    )
    session = _FakeSession([_FakeResult(items=[existing])])
    sig = Signature(failure_subtype="ENRICHMENT_FAILED", failing_service="SPT",
                     error_token="SocketTimeoutException", digest="d1")
    incident = await assign_incident(
        session, signature=sig, infra_class="infra", causal=_causal("timeout"), now=NOW
    )
    assert incident is existing
    assert session.added == []  # matched, nothing new created


async def test_recognized_infra_creates_new_when_no_open_match():
    session = _FakeSession([_FakeResult(items=[])])  # no open incident with this signature
    sig = Signature(failure_subtype="ENRICHMENT_FAILED", failing_service="SPT",
                     error_token="SocketTimeoutException", digest="d1")
    incident = await assign_incident(
        session, signature=sig, infra_class="infra", causal=_causal("timeout"), now=NOW
    )
    assert incident.signature == "d1"
    assert incident.status == "open"
    assert incident.primary_alert_id == "causal-1"
    assert incident.alert_count == 0 and incident.journey_count == 0  # process_completion bumps these
    assert incident in session.added


# --- assign_incident: novel + INFRA (cosine + veto) -----------------------------
async def test_novel_infra_matches_via_cosine():
    primary_alert = Alert(
        alert_id="a0", emitted_at=NOW, log_id="l0", level="ERROR",
        app_name="cc-new-service", logger="c.c.new.Thing", message="Connection refused calling X",
        source="fallback", embedding=[1.0, 0.0],
    )
    existing = Incident(
        incident_id="inc-2", signature=None, failure_subtype=None,
        failing_service="cc-new-service", error_token=None, title="Unrecognized — cc-new-service",
        department=None, status="open", first_ts=NOW, last_ts=NOW,
        primary_alert_id="a0", alert_count=1, journey_count=1,
    )
    session = _FakeSession([_FakeResult(rows=[(existing, primary_alert)])])
    sig = Signature(failure_subtype=None, failing_service="cc-new-service", error_token=None, digest=None)
    causal = _causal("Connection refused calling X", embedding=[1.0, 0.0])
    incident = await assign_incident(session, signature=sig, infra_class="infra", causal=causal, now=NOW)
    assert incident is existing


async def test_novel_infra_veto_rejects_meaning_flip_even_above_threshold():
    primary_alert = Alert(
        alert_id="a0", emitted_at=NOW, log_id="l0", level="ERROR",
        app_name="cc-new-service", logger="c.c.new.Thing", message="Connection succeeded to X",
        source="fallback", embedding=[1.0, 0.0],
    )
    existing = Incident(
        incident_id="inc-2", signature=None, failure_subtype=None,
        failing_service="cc-new-service", error_token=None, title="t",
        department=None, status="open", first_ts=NOW, last_ts=NOW,
        primary_alert_id="a0", alert_count=1, journey_count=1,
    )
    session = _FakeSession([_FakeResult(rows=[(existing, primary_alert)])])
    sig = Signature(failure_subtype=None, failing_service="cc-new-service", error_token=None, digest=None)
    # Identical vector (cosine 1.0, clears any threshold) but opposite meaning.
    causal = _causal("Connection failed to X", embedding=[1.0, 0.0])
    incident = await assign_incident(session, signature=sig, infra_class="infra", causal=causal, now=NOW)
    assert incident is not existing  # vetoed -> a NEW incident, not the flipped match
    assert incident in session.added


# --- assign_incident: order-specific never searches -----------------------------
async def test_order_specific_never_searches_always_creates():
    session = _FakeSession([])  # no execute() call expected at all
    sig = Signature(failure_subtype="MARGIN_CHECK_FAILED", failing_service="checker",
                     error_token=None, digest="d-margin")
    incident = await assign_incident(
        session, signature=sig, infra_class="order_specific",
        causal=_causal("below threshold"), now=NOW,
    )
    assert incident.failing_service == "checker"
    assert incident in session.added


from backend.journeys import Completion, JourneyStatus
from backend.incidents import process_completion
from backend.stitching import StitchedJourney


def _completion(status: JourneyStatus, journey_id: str = "j1") -> Completion:
    journey = StitchedJourney(journey_id=journey_id, order_id="ORD-1")
    outcome = "ENRICHMENT_FAILED" if status is JourneyStatus.FAILED else "TIMED_OUT"
    return Completion(journey_id=journey_id, journey=journey, status=status, outcome=outcome)


class _RowResult:
    """A result whose .first() returns a (incident_id, suggested_failure_label)
    row, like `select(Journey.incident_id, Journey.suggested_failure_label)`."""
    def __init__(self, value, suggested_failure_label=None):
        self._value = value
        self._label = suggested_failure_label

    def first(self):
        return None if self._value is _MISSING else (self._value, self._label)


_MISSING = object()


async def test_success_journey_is_ineligible_and_returns_none():
    session = _FakeSession([])  # no query should even run
    result = await process_completion(session, _completion(JourneyStatus.SUCCESS))
    assert result is None


async def test_already_clustered_journey_is_a_noop():
    session = _FakeSession([_RowResult("inc-existing")])
    result = await process_completion(session, _completion(JourneyStatus.FAILED))
    assert result is None


async def test_timed_out_with_no_error_alert_forms_no_incident():
    candidates_result = _FakeResult(items=[
        Alert(alert_id="w1", emitted_at=NOW, log_id="l1", level="WARN",
              app_name="cc-x", logger="l", message="just a warning", source="fallback"),
    ])
    session = _FakeSession([_RowResult(None), candidates_result])
    result = await process_completion(session, _completion(JourneyStatus.TIMED_OUT))
    assert result is None


async def test_timed_out_with_a_real_error_forms_an_incident_via_novel_path():
    """The whole reason embeddings were chosen over a hardcoded list: a
    journey with no _FAILURE_RULES match times out to TIMED_OUT, but with a
    real linked ERROR it's still incident-eligible via the novel path.

    This causal message matches neither the INFRA nor order-specific token
    tables, so it classifies as order-specific (the safe default — Task 7) —
    which means assign_incident creates a new incident WITHOUT searching at
    all (no execute() call for a match attempt), so only 3 execute() calls
    happen in total: the idempotency check, the causal-candidate fetch, and
    the final alert-linking fetch (the closing UPDATE is a 4th).
    """
    alerts = [
        Alert(alert_id="a1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-new-service", logger="c.c.new.Thing",
              message="Something entirely unprecedented happened",
              source="fallback", department=None, embedding=[1.0, 0.0], journey_id="j1"),
    ]
    session = _FakeSession([
        _RowResult(None),               # not yet clustered
        _FakeResult(items=alerts),        # causal-candidate fetch
        _FakeResult(items=alerts),         # journey's full alert set, for linking
        _FakeResult(items=[]),            # the closing UPDATE journeys — return value unused
    ])
    incident = await process_completion(session, _completion(JourneyStatus.TIMED_OUT))
    assert incident is not None
    assert incident.failure_subtype is None  # unrecognized — novel path, not a hardcoded rule
    assert incident.journey_count == 1
    assert incident.alert_count == 1


async def test_unrecognized_failure_normalizes_to_novel_path_for_matching():
    # A FAILED/UNRECOGNIZED_FAILURE completion must still take the cosine/
    # embedding novel path for matching purposes — not the hash-based exact
    # path — so unrelated unrecognized failures from the same service don't
    # get coarsely merged by service name alone.
    alerts = [
        Alert(alert_id="e1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-settings-service", logger="c.c.settings.CacheClient",
              message="ConnectionPoolExhaustedError: no available connections",
              source="fallback", department=None, embedding=None, journey_id="j-novel"),
    ]
    session = _FakeSession([
        _RowResult(None),                # not yet clustered
        _FakeResult(items=alerts),       # causal-candidate fetch
        _FakeResult(items=alerts),       # journey's full alert set, for linking
        _FakeResult(items=[]),           # the closing UPDATE journeys — return value unused
    ])
    completion = Completion(
        journey_id="j-novel", journey=StitchedJourney(journey_id="j-novel", order_id="ORD-1"),
        status=JourneyStatus.FAILED, outcome="UNRECOGNIZED_FAILURE",
    )
    incident = await process_completion(session, completion)
    assert incident.signature is None  # novel path, not a hash digest
    assert incident.failure_subtype is None


async def test_unrecognized_failure_uses_suggested_label_from_journey_row():
    alerts = [
        Alert(alert_id="e1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-settings-service", logger="c.c.settings.CacheClient",
              message="ConnectionPoolExhaustedError: no available connections",
              source="fallback", department=None, embedding=None, journey_id="j-novel"),
    ]
    session = _FakeSession([
        _RowResult(None, suggested_failure_label="Settings cache connection pool exhausted"),
        _FakeResult(items=alerts),
        _FakeResult(items=alerts),
        _FakeResult(items=[]),
    ])
    completion = Completion(
        journey_id="j-novel", journey=StitchedJourney(journey_id="j-novel", order_id="ORD-1"),
        status=JourneyStatus.FAILED, outcome="UNRECOGNIZED_FAILURE",
    )
    incident = await process_completion(session, completion)
    assert incident.title == "Settings cache connection pool exhausted — cc-settings-service"


async def test_failed_journey_creates_incident_links_all_alerts_and_journey():
    """Mirrors scenario 8's real sequence (spt.py): the SptClient's own
    SocketTimeoutException ERROR comes first (this is the causal line, and
    it's what makes this classify as INFRA), then a WARN retry, then the
    generic OrderProcessingService abort ERROR last. All 3 alerts must end up
    linked to the one incident, even though only the first is "causal".
    """
    alerts = [
        Alert(alert_id="a1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-order-engine", logger="c.c.orderengine.client.SptClient",
              message=(
                  "[SptClient#getSptPriceListCode] <--- ERROR "
                  "java.net.SocketTimeoutException: connect timed out (10014ms)"
              ),
              source="fallback", department=None, embedding=None, journey_id="j1"),
        Alert(alert_id="a2", emitted_at=NOW, log_id="l2", level="WARN",
              app_name="cc-order-engine", logger="c.c.orderengine.client.SptClient",
              message="Retrying SPT price list call (attempt 2/3)",
              source="fallback", department=None, embedding=None, journey_id="j1"),
        Alert(alert_id="a3", emitted_at=NOW, log_id="l3", level="ERROR",
              app_name="cc-order-engine", logger="c.c.orderengine.service.OrderProcessingService",
              message="Order processing aborted for order ORD-1: SPT price list service unavailable after 3 attempt(s)",
              source="fallback", department=None, embedding=None, journey_id="j1"),
    ]
    session = _FakeSession([
        _RowResult(None),                       # not yet clustered
        _FakeResult(items=alerts),               # causal-candidate fetch (ordered by emitted_at)
        _FakeResult(items=[]),                   # no open incident with this signature (INFRA search)
        _FakeResult(items=alerts),               # journey's full alert set, for linking
        _FakeResult(items=[]),                   # the closing UPDATE journeys — return value unused
    ])
    incident = await process_completion(session, _completion(JourneyStatus.FAILED))
    assert incident is not None
    assert incident.alert_count == 3
    assert incident.journey_count == 1
    assert incident.failure_subtype == "ENRICHMENT_FAILED"
    assert incident.failing_service == "SPT"
    assert all(a.incident_id == incident.incident_id for a in alerts)


# --- on_event: incident.new (WebSocket push), mirrors alert.new ------------------


async def test_new_incident_emits_incident_new_via_on_event():
    """Creating a fresh incident pushes exactly one incident.new event, with a
    payload matching the IncidentOut REST shape (same contract as alert.new)."""
    alerts = [
        Alert(alert_id="a1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-order-engine", logger="c.c.orderengine.client.SptClient",
              message=(
                  "[SptClient#getSptPriceListCode] <--- ERROR "
                  "java.net.SocketTimeoutException: connect timed out (10014ms)"
              ),
              source="fallback", department=None, embedding=None, journey_id="j1"),
    ]
    session = _FakeSession([
        _RowResult(None),               # not yet clustered
        _FakeResult(items=alerts),        # causal-candidate fetch
        _FakeResult(items=[]),            # no open incident with this signature -> creates
        _FakeResult(items=alerts),         # journey's full alert set, for linking
        _FakeResult(items=[]),             # closing UPDATE journeys — return value unused
    ])
    events = []

    async def on_event(event):
        events.append(event)

    incident = await process_completion(
        session, _completion(JourneyStatus.FAILED), on_event=on_event
    )
    assert incident is not None
    assert len(events) == 1
    assert events[0]["type"] == "incident.new"
    assert events[0]["data"]["incident_id"] == incident.incident_id
    assert events[0]["data"]["failure_subtype"] == "ENRICHMENT_FAILED"


async def test_joining_existing_incident_emits_incident_updated_not_new():
    """Joining an already-open incident must push incident.updated (counts
    grew), NOT incident.new — that's reserved for the actually-new row."""
    existing = Incident(
        incident_id="inc-1", signature="d1", failure_subtype="ENRICHMENT_FAILED",
        failing_service="SPT", error_token=None, title="ENRICHMENT_FAILED — SPT",
        department="devops", status="open", first_ts=NOW, last_ts=NOW,
        primary_alert_id="a0", alert_count=4, journey_count=1,
    )
    alerts = [
        Alert(alert_id="a1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-order-engine", logger="c.c.orderengine.client.SptClient",
              message=(
                  "[SptClient#getSptPriceListCode] <--- ERROR "
                  "java.net.SocketTimeoutException: connect timed out (10014ms)"
              ),
              source="fallback", department=None, embedding=None, journey_id="j1"),
    ]
    session = _FakeSession([
        _RowResult(None),                       # not yet clustered
        _FakeResult(items=alerts),               # causal-candidate fetch
        _FakeResult(items=[existing]),           # matches the existing open incident
        _FakeResult(items=alerts),               # journey's full alert set, for linking
        _FakeResult(items=[]),                   # closing UPDATE journeys — return value unused
    ])
    events = []

    async def on_event(event):
        events.append(event)

    incident = await process_completion(
        session, _completion(JourneyStatus.FAILED), on_event=on_event
    )
    assert incident is existing
    assert len(events) == 1
    assert events[0]["type"] == "incident.updated"
    assert events[0]["data"]["incident_id"] == existing.incident_id
    # The count bump is visible in the pushed payload, not just in memory.
    assert events[0]["data"]["alert_count"] == 5
    assert events[0]["data"]["journey_count"] == 2


from backend.incidents import INCIDENT_QUIET_TIMEOUT, sweep_stale_incidents


async def test_sweep_closes_incidents_past_the_quiet_timeout():
    stale = Incident(
        incident_id="inc-old", signature="d1", failure_subtype="ENRICHMENT_FAILED",
        failing_service="SPT", error_token=None, title="t", department=None,
        status="open", first_ts=NOW - timedelta(seconds=INCIDENT_QUIET_TIMEOUT + 100),
        last_ts=NOW - timedelta(seconds=INCIDENT_QUIET_TIMEOUT + 1),
        primary_alert_id="a0", alert_count=1, journey_count=1,
    )
    session = _FakeSession([_FakeResult(items=[stale])])
    closed = await sweep_stale_incidents(session, now=NOW)
    assert closed == [stale]
    assert stale.status == "resolved"


async def test_sweep_leaves_recently_active_incidents_open():
    session = _FakeSession([_FakeResult(items=[])])  # query already filters by last_ts
    closed = await sweep_stale_incidents(session, now=NOW)
    assert closed == []


from backend.db import Journey
from backend.incidents import retry_unclustered_completions


async def test_retry_clusters_a_journey_whose_alerts_have_since_landed():
    """Mirrors the live-testing discovery: a journey completed (raw.events,
    no LLM wait) before its own alerts existed (processed.alerts, LLM-bound),
    so the first process_completion attempt found nothing. By the time this
    retry runs, the alerts have landed — clustering should now succeed.
    """
    journey = Journey(
        journey_id="j1", status="FAILED", outcome="ENRICHMENT_FAILED",
        first_ts=NOW, last_ts=NOW, incident_id=None,
    )
    alerts = [
        Alert(alert_id="a1", emitted_at=NOW, log_id="l1", level="ERROR",
              app_name="cc-order-engine", logger="c.c.orderengine.client.SptClient",
              message="[SptClient#getSptPriceListCode] <--- ERROR SocketTimeoutException (10014ms)",
              source="fallback", department=None, embedding=None, journey_id="j1"),
    ]
    session = _FakeSession([
        _FakeResult(items=[journey]),           # find unclustered terminal journeys
        _RowResult(None),                       # process_completion: not yet clustered
        _FakeResult(items=alerts),               # causal-candidate fetch (now populated)
        _FakeResult(items=[]),                   # no open incident with this signature
        _FakeResult(items=alerts),               # journey's full alert set, for linking
        _FakeResult(items=[]),                   # closing UPDATE journeys — return value unused
    ])
    incidents = await retry_unclustered_completions(session, now=NOW)
    assert len(incidents) == 1
    assert incidents[0].failure_subtype == "ENRICHMENT_FAILED"
    assert journey.incident_id is None  # this fake session doesn't apply the UPDATE to `journey`


async def test_retry_leaves_a_journey_alone_when_still_no_causal_alert():
    """Safe to call repeatedly: if the alerts still haven't landed, this must
    no-op again (not crash, not create a bogus incident) — a later sweep gets
    another chance."""
    journey = Journey(
        journey_id="j2", status="FAILED", outcome="ENRICHMENT_FAILED",
        first_ts=NOW, last_ts=NOW, incident_id=None,
    )
    session = _FakeSession([
        _FakeResult(items=[journey]),  # find unclustered terminal journeys
        _RowResult(None),              # process_completion: not yet clustered
        _FakeResult(items=[]),         # still zero alerts for this journey
    ])
    incidents = await retry_unclustered_completions(session, now=NOW)
    assert incidents == []


async def test_retry_finds_nothing_when_no_journeys_are_unclustered():
    session = _FakeSession([_FakeResult(items=[])])
    incidents = await retry_unclustered_completions(session, now=NOW)
    assert incidents == []
