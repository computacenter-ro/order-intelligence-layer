"""Tests for backend/journeys.py — journey-over detection + lazy assembly.

Two layers, tested independently:

* **Pure detection** (``detect_terminal`` / ``classify_failure`` /
  ``is_stalled``) — given an ordered list of ``LogLine`` (and, for timeouts, a
  clock), return the outcome. No DB, no queues. The message texts asserted here
  are the *actual* strings the mock services emit (see ``services/*.py``) —
  they are load-bearing per CLAUDE.md.
* **Incremental assembly** (``JourneyAssembler``) — wraps the pure
  ``Stitcher``, reports each journey's completion exactly once, and derives
  TIMED_OUT from the clock. The DB-free decision layer is what we exercise
  here; persistence is a thin wrapper over these decisions.
"""

from datetime import datetime, timedelta, timezone

from shared.models import LogLine
from backend.journeys import (
    JourneyAssembler,
    JourneyStatus,
    classify_failure,
    detect_terminal,
    is_stalled,
    status_for,
    SUCCESS,
    TIMED_OUT,
    FAILED,
    INBOUND_TRANSFORM_FAILED,
    ORDER_CREATION_FAILED,
    MARGIN_CHECK_FAILED,
    VALIDATION_FAILED,
    ENRICHMENT_FAILED,
    AUTH_FAILED,
    SAP_SUBMISSION_FAILED,
)

import pytest

BASE = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)


def mk(
    offset_s: float,
    message: str,
    *,
    level: str = "INFO",
    app_name: str = "cc-order-engine",
    eventId: str | None = None,
    orderId: str | None = None,
    cartHeaderId: str | None = None,
    log_id: str | None = None,
) -> LogLine:
    return LogLine(
        log_id=log_id or f"log-{offset_s}-{message[:10]}",
        timestamp=BASE + timedelta(seconds=offset_s),
        app_name=app_name,
        level=level,
        logger="c.c.test.Logger",
        host="CCECMEWEBT001",
        process_id="1234",
        thread="rabbit-listener-1",
        eventId=eventId,
        orderId=orderId,
        cartHeaderId=cartHeaderId,
        message=message,
    )


# --- Actual terminal messages emitted by the mock services -------------------
# (copied from services/*.py — these strings are the detection contract)
SUCCESS_MSG = "Registered order ORD-6001 for tracking, SAP ref: 0080012345"
TRANSFORM_MSG = (
    "Max redelivery attempts reached for event evt-1; "
    "routing message to order.inbound.dlq"
)
CREATE_MSG = "Order creation failed for event evt-1 after 3 attempt(s)"
MARGIN_MSG = "Order ORD-6001 blocked by margin check; submission halted"
VALIDATION_MSG = "Order ORD-6001 validation failed with 1 error(s); submission aborted"
ENRICHMENT_MSG = (
    "Order processing aborted for order ORD-6001: SPT price "
    "list service unavailable after 3 attempt(s)"
)
AUTH_MSG = (
    "Cannot process order ORD-6001: user XDISABLED not "
    "authorized (403 from JAM); submission aborted"
)
SAP_MSG = (
    "Order ORD-6001 submission failed after 3 attempt(s); "
    "message moved to order.outbound.dlq for manual intervention"
)


# --- classify_failure: message -> FAILED subtype -----------------------------


@pytest.mark.parametrize(
    "message,expected",
    [
        (TRANSFORM_MSG, INBOUND_TRANSFORM_FAILED),
        (CREATE_MSG, ORDER_CREATION_FAILED),
        (MARGIN_MSG, MARGIN_CHECK_FAILED),
        (VALIDATION_MSG, VALIDATION_FAILED),
        (ENRICHMENT_MSG, ENRICHMENT_FAILED),
        (AUTH_MSG, AUTH_FAILED),
        (SAP_MSG, SAP_SUBMISSION_FAILED),
        # documented (CLAUDE.md) queue_error spelling must also classify
        ("routing message to order.inbound.queue_error", INBOUND_TRANSFORM_FAILED),
        ("message moved to order.outbound.queue_error", SAP_SUBMISSION_FAILED),
    ],
)
def test_classify_failure_maps_each_terminal(message, expected):
    assert classify_failure(message) == expected


def test_classify_failure_none_for_benign():
    assert classify_failure("Margin check passed for order ORD-6001") is None
    assert classify_failure("Not implemented: strategy skipped") is None
    assert classify_failure("Retrying SAP submission for order ORD-6001") is None
    assert classify_failure("Requeueing event evt-1 for redelivery (attempt 1/3)") is None


def test_auth_precedence_over_generic_submission_aborted():
    # The JAM abort line contains BOTH "not authorized" and "submission aborted";
    # it must classify as AUTH_FAILED, not VALIDATION_FAILED.
    assert classify_failure(AUTH_MSG) == AUTH_FAILED


# --- detect_terminal: ordered logs -> outcome or None ------------------------


def test_detect_success_on_track_trace_terminal():
    logs = [
        mk(0, "Received inbound order event evt-1", eventId="evt-1"),
        mk(5, SUCCESS_MSG, app_name="cc-track-trace", orderId="ORD-6001"),
    ]
    assert detect_terminal(logs) == SUCCESS


def test_detect_none_for_in_progress():
    logs = [
        mk(0, "Received inbound order event evt-1", eventId="evt-1"),
        mk(1, "Generated order number ORD-6001", eventId="evt-1"),
    ]
    assert detect_terminal(logs) is None


def test_detect_empty_is_none():
    assert detect_terminal([]) is None


def test_detect_failed_finds_marker_even_with_trailing_info():
    # Order-creation failure emits the fatal ERROR then a trailing INFO publish
    # line; detection must still find the fatal marker.
    logs = [
        mk(0, "Received inbound order event evt-1", eventId="evt-1"),
        mk(1, CREATE_MSG, level="ERROR", eventId="evt-1"),
        mk(2, "Published order creation failure for event evt-1 to queue order.response.queue",
           eventId="evt-1"),
    ]
    assert detect_terminal(logs) == ORDER_CREATION_FAILED


# --- is_stalled: time-based -------------------------------------------------


def test_is_stalled_boundary():
    assert is_stalled(BASE, BASE + timedelta(seconds=91), 90) is True
    assert is_stalled(BASE, BASE + timedelta(seconds=90), 90) is False
    assert is_stalled(BASE, BASE + timedelta(seconds=89), 90) is False


# --- status_for: outcome subtype -> high-level status ------------------------


def test_status_for():
    assert status_for(SUCCESS) is JourneyStatus.SUCCESS
    assert status_for(TIMED_OUT) is JourneyStatus.TIMED_OUT
    assert status_for(MARGIN_CHECK_FAILED) is JourneyStatus.FAILED
    assert status_for(INBOUND_TRANSFORM_FAILED) is JourneyStatus.FAILED
    assert status_for(FAILED) is JourneyStatus.FAILED


# --- JourneyAssembler: incremental decisions --------------------------------


def _success_flow():
    return [
        mk(0, "Received inbound order event evt-1", eventId="evt-1"),
        mk(2, "Received order creation response for event evt-1",
           eventId="evt-1", orderId="ORD-6001", cartHeaderId="1840927365018240001"),
        mk(5, SUCCESS_MSG, app_name="cc-track-trace",
           orderId="ORD-6001", cartHeaderId="1840927365018240001"),
    ]


def test_assembler_reports_success_once():
    a = JourneyAssembler()
    a.add(_success_flow())
    first = a.evaluate(now=BASE + timedelta(seconds=6))
    assert len(first) == 1
    c = first[0]
    assert c.status is JourneyStatus.SUCCESS
    assert c.outcome == SUCCESS
    # idempotent: already-completed journeys are not reported again
    assert a.evaluate(now=BASE + timedelta(seconds=7)) == []


def test_assembler_failed_subtype_and_aliases():
    a = JourneyAssembler()
    a.add([
        mk(0, "Received inbound order event evt-1", eventId="evt-1"),
        mk(2, "Received order creation response for event evt-1",
           eventId="evt-1", orderId="ORD-6001"),
        mk(3, MARGIN_MSG, level="WARN", orderId="ORD-6001", cartHeaderId="18409"),
    ])
    [c] = a.evaluate(now=BASE + timedelta(seconds=4))
    assert c.status is JourneyStatus.FAILED
    assert c.outcome == MARGIN_CHECK_FAILED
    assert c.journey.order_id == "ORD-6001"


def test_pre_creation_failure_is_eventid_only():
    a = JourneyAssembler()
    a.add([
        mk(0, "Received inbound order event evt-1", eventId="evt-1"),
        mk(1, TRANSFORM_MSG, level="ERROR", app_name="cc-inbound-service", eventId="evt-1"),
    ])
    [c] = a.evaluate(now=BASE + timedelta(seconds=2))
    assert c.status is JourneyStatus.FAILED
    assert c.outcome == INBOUND_TRANSFORM_FAILED
    assert c.journey.event_id == "evt-1"
    assert c.journey.order_id is None
    assert c.journey.cart_header_id is None


def test_assembler_timeout_only_after_threshold():
    a = JourneyAssembler(stalled_timeout=90)
    a.add([mk(0, "Received inbound order event evt-1", eventId="evt-1")])
    # within the window: still in progress, no completion
    assert a.evaluate(now=BASE + timedelta(seconds=90)) == []
    # past the window: TIMED_OUT
    [c] = a.evaluate(now=BASE + timedelta(seconds=91))
    assert c.status is JourneyStatus.TIMED_OUT
    assert c.outcome == TIMED_OUT


def test_message_terminal_wins_over_timeout():
    a = JourneyAssembler(stalled_timeout=90)
    a.add(_success_flow())
    # even though wall-clock is far past the stall window, a real terminal wins
    [c] = a.evaluate(now=BASE + timedelta(seconds=1000))
    assert c.status is JourneyStatus.SUCCESS


def test_assembler_lazy_across_batches():
    a = JourneyAssembler()
    flow = _success_flow()
    a.add(flow[:1])
    assert a.evaluate(now=BASE + timedelta(seconds=1)) == []
    a.add(flow[1:2])
    assert a.evaluate(now=BASE + timedelta(seconds=3)) == []
    a.add(flow[2:])
    [c] = a.evaluate(now=BASE + timedelta(seconds=6))
    assert c.status is JourneyStatus.SUCCESS
    assert len(c.journey.logs) == 3


# =============================================================================
# Fixture-driven end-to-end: the 10 canonical flows
# (pipeline/data/mock-order-flows-v2.json — the reference-system log samples).
#
# Each flow is a real captured log stream ("events") plus its expected
# "outcome". We push every flow's events through the *pure* pipeline —
# stitching (backend.stitching.Stitcher via JourneyAssembler) + outcome
# detection (backend.journeys) — with no DB and no broker, and assert the
# Correlation Model / journey-over guarantees from CLAUDE.md.
# =============================================================================

import json
from pathlib import Path

from backend.stitching import Stitcher  # noqa: F401 — used via JourneyAssembler

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "pipeline" / "data" / "mock-order-flows-v2.json"
)

# The one log where eventId coexists with the new order id(s): inbound's
# ResponseListener logging the creation response (CLAUDE.md Correlation Model).
_BRIDGE_LOGGER = "c.c.inbound.listener.ResponseListener"


def _bridge_index(logs: list[LogLine]) -> int | None:
    return next(
        (
            i
            for i, log in enumerate(logs)
            if log.logger == _BRIDGE_LOGGER
            and "creation response" in log.message.lower()
        ),
        None,
    )


def _load_flows() -> list[dict]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    flows = []
    for flow in raw:
        logs = [LogLine.model_validate(e) for e in flow["events"]]
        flows.append(
            {
                # "_flow" is the scenario number (1..10); "scenario" is its prose
                # description in this fixture.
                "scenario": flow["_flow"],
                "name": flow["scenario"],
                "outcome": flow["outcome"],
                "logs": logs,
                "bridge_idx": _bridge_index(logs),
            }
        )
    return flows


FLOWS = _load_flows()
FLOW_IDS = [f"scenario-{f['scenario']}-{f['outcome']}" for f in FLOWS]
# Pre-creation failures (transform / creation) never reach the bridge — they are
# eventId-only journeys, so the bridge-cut test only applies to the rest.
BRIDGE_FLOWS = [f for f in FLOWS if f["bridge_idx"] is not None]
BRIDGE_FLOW_IDS = [f"scenario-{f['scenario']}-{f['outcome']}" for f in BRIDGE_FLOWS]


def _assemble(batches: list[list[LogLine]]) -> tuple[JourneyAssembler, list]:
    """Feed batches ("polls") through stitching + detection; no DB/broker.

    Returns the assembler and the completions from a single final evaluate().
    A real message terminal always wins over the stall clock, so ``now`` is
    irrelevant here; we pin it to the last timestamp for determinism.
    """
    a = JourneyAssembler()
    for batch in batches:
        a.add(batch)
    all_ts = [log.timestamp for batch in batches for log in batch]
    completions = a.evaluate(now=max(all_ts) if all_ts else None)
    return a, completions


def _split(logs: list[LogLine], n: int) -> list[list[LogLine]]:
    """Split ``logs`` into up to ``n`` contiguous, non-empty batches (in order)."""
    size = max(1, (len(logs) + n - 1) // n)
    return [logs[i : i + size] for i in range(0, len(logs), size)]


def test_fixture_loads_ten_flows():
    assert len(FLOWS) == 10
    assert {f["scenario"] for f in FLOWS} == set(range(1, 11))


@pytest.mark.parametrize("flow", FLOWS, ids=FLOW_IDS)
def test_flow_produces_exactly_one_journey(flow):
    a, _ = _assemble([flow["logs"]])
    assert len(a.stitcher.journeys) == 1
    # every event lands in that single journey
    assert len(a.stitcher.journeys[0].logs) == len(flow["logs"])


@pytest.mark.parametrize("flow", FLOWS, ids=FLOW_IDS)
def test_flow_outcome_matches_fixture(flow):
    a, completions = _assemble([flow["logs"]])
    assert len(completions) == 1
    assert completions[0].outcome == flow["outcome"]
    # and pure detection agrees directly on the stitched, ordered logs
    assert detect_terminal(a.stitcher.journeys[0].logs) == flow["outcome"]


@pytest.mark.parametrize("flow", FLOWS, ids=FLOW_IDS)
def test_flow_stitches_across_multiple_polls(flow):
    # The same events, delivered in several separate polls, still assemble into
    # exactly one journey with the same outcome (incremental / "lazy" assembly).
    a, completions = _assemble(_split(flow["logs"], 4))
    assert len(a.stitcher.journeys) == 1
    assert len(a.stitcher.journeys[0].logs) == len(flow["logs"])
    assert len(completions) == 1
    assert completions[0].outcome == flow["outcome"]


@pytest.mark.parametrize("flow", BRIDGE_FLOWS, ids=BRIDGE_FLOW_IDS)
def test_flow_stitches_with_poll_boundary_at_the_bridge(flow):
    # A poll boundary lands EXACTLY at the bridge line — the hardest split for
    # the Correlation Model: phase 1 (eventId only) | bridge | phase 2 (order
    # ids only). Single-pass stitching must still bridge the id change.
    logs, b = flow["logs"], flow["bridge_idx"]
    batches = [logs[:b], logs[b : b + 1], logs[b + 1 :]]
    batches = [batch for batch in batches if batch]
    a, completions = _assemble(batches)
    assert len(a.stitcher.journeys) == 1
    assert len(a.stitcher.journeys[0].logs) == len(logs)
    assert completions[0].outcome == flow["outcome"]


@pytest.mark.parametrize("flow", FLOWS, ids=FLOW_IDS)
def test_full_redelivery_is_idempotent(flow):
    # At-least-once delivery: re-delivering the identical logs (same log_ids)
    # must change nothing — no duplicate events, same single journey/outcome.
    logs = flow["logs"]
    a = JourneyAssembler()
    first = a.add(logs)
    assert len(first) == len(logs)  # all new on first delivery
    assert a.add(logs) == []  # every re-delivered log_id is a duplicate → no-op
    assert len(a.stitcher.journeys) == 1
    assert len(a.stitcher.journeys[0].logs) == len(logs)
    [completion] = a.evaluate(now=max(log.timestamp for log in logs))
    assert completion.outcome == flow["outcome"]


@pytest.mark.parametrize("flow", FLOWS, ids=FLOW_IDS)
def test_overlapping_polls_do_not_duplicate(flow):
    # The AI poller uses overlapping sliding windows, so consecutive polls
    # re-deliver their overlap. Dedup on log_id must absorb it.
    logs = flow["logs"]
    mid = len(logs) // 2
    overlap = min(len(logs), mid + 3)
    batches = [logs[:overlap], logs[mid:]]  # logs[mid:overlap] delivered twice
    a, completions = _assemble(batches)
    assert len(a.stitcher.journeys) == 1
    assert len(a.stitcher.journeys[0].logs) == len(logs)
    assert completions[0].outcome == flow["outcome"]
