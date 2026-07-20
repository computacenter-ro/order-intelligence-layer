"""Tests for backend/stitching.py — single-pass, incremental journey stitching.

These pin the Correlation Model from CLAUDE.md as executable invariants:

* phase-1 logs carry only ``eventId``; the bridge carries ``eventId`` + >=1
  order id; phase-2 logs carry both order ids and never ``eventId``;
* single-pass, timestamp-ordered stitching produces exactly one journey for all
  three bridge variants (both / order-only / cart-only);
* assembly is incremental — logs split across successive polls (including a
  split right at the bridge) still stitch into one journey;
* pre-creation failures produce complete, valid journeys identified only by
  ``eventId``;
* ``accountNumber`` is NEVER used to correlate.

The pure stitcher owns no DB and no queues — it is exercised here with plain
``LogLine`` objects only.
"""

from datetime import datetime, timedelta, timezone

from shared.models import LogLine
from backend.stitching import Stitcher, StitchedJourney

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
    accountNumber: str | None = None,
    log_id: str | None = None,
) -> LogLine:
    """Build a LogLine at ``BASE + offset_s`` seconds with the given ids."""
    return LogLine(
        log_id=log_id or f"log-{offset_s}-{message[:8]}",
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
        accountNumber=accountNumber,
        message=message,
    )


def _journey_of(logs: list[LogLine]) -> tuple[Stitcher, list[StitchedJourney]]:
    s = Stitcher()
    s.add_batch(logs)
    return s, s.journeys


# --- Bridge variants: one journey each ---------------------------------------


def phase1_bridge_phase2(bridge_ids: str) -> list[LogLine]:
    """A full journey (phase 1 -> bridge -> phase 2) for the given bridge variant."""
    ev, order, cart = "evt-abc", "ORD-6001", "1840927365018240001"
    bridge = {"both": (order, cart), "order": (order, None), "cart": (None, cart)}[
        bridge_ids
    ]
    return [
        mk(0, "Received inbound order event evt-abc", eventId=ev, accountNumber="81036533"),
        mk(1, "Generated order number ORD-6001", eventId=ev),
        mk(2, "Received order creation response for event evt-abc",
           eventId=ev, orderId=bridge[0], cartHeaderId=bridge[1]),
        mk(3, "Get order by Order Number:ORD-6001", orderId=order, cartHeaderId=cart),
        mk(4, "Registered order ORD-6001 for tracking", orderId=order, cartHeaderId=cart),
    ]


def test_bridge_both_stitches_one_journey():
    _s, journeys = _journey_of(phase1_bridge_phase2("both"))
    assert len(journeys) == 1
    assert len(journeys[0].logs) == 5


def test_bridge_order_only_stitches_one_journey():
    # cartHeaderId first appears in phase 2 and must link via the known orderId.
    _s, journeys = _journey_of(phase1_bridge_phase2("order"))
    assert len(journeys) == 1
    assert len(journeys[0].logs) == 5


def test_bridge_cart_only_stitches_one_journey():
    # orderId first appears in phase 2 and must link via the known cartHeaderId.
    _s, journeys = _journey_of(phase1_bridge_phase2("cart"))
    assert len(journeys) == 1
    assert len(journeys[0].logs) == 5


def test_journey_accumulates_all_alias_ids():
    _s, journeys = _journey_of(phase1_bridge_phase2("cart"))
    j = journeys[0]
    assert j.event_id == "evt-abc"
    assert j.order_id == "ORD-6001"
    assert j.cart_header_id == "1840927365018240001"


def test_lookup_by_any_alias_returns_same_journey():
    s, _journeys = _journey_of(phase1_bridge_phase2("both"))
    j = s.journey_for("evt-abc")
    assert j is not None
    assert s.journey_for("ORD-6001") is j
    assert s.journey_for("1840927365018240001") is j


# --- Separation: distinct journeys never merge -------------------------------


def test_two_independent_flows_stay_separate():
    a = phase1_bridge_phase2("both")
    b = [
        mk(0.5, "Received inbound order event evt-xyz", eventId="evt-xyz"),
        mk(2.5, "Received order creation response for event evt-xyz",
           eventId="evt-xyz", orderId="ORD-7777", cartHeaderId="9990000000000000000"),
        mk(3.5, "Registered order ORD-7777 for tracking",
           orderId="ORD-7777", cartHeaderId="9990000000000000000"),
    ]
    _s, journeys = _journey_of(a + b)
    assert len(journeys) == 2


def test_account_number_never_correlates():
    # Two unrelated journeys sharing an accountNumber must not be merged.
    shared_acct = "81036533"
    a = [
        mk(0, "Received inbound order event evt-a", eventId="evt-a", accountNumber=shared_acct),
        mk(1, "Order creation failed for event evt-a", level="ERROR",
           eventId="evt-a", accountNumber=shared_acct),
    ]
    b = [
        mk(0.5, "Received inbound order event evt-b", eventId="evt-b", accountNumber=shared_acct),
        mk(1.5, "Order creation failed for event evt-b", level="ERROR",
           eventId="evt-b", accountNumber=shared_acct),
    ]
    _s, journeys = _journey_of(a + b)
    assert len(journeys) == 2


# --- Incremental / cross-poll assembly --------------------------------------


def test_split_across_polls_including_at_the_bridge():
    logs = phase1_bridge_phase2("order")
    s = Stitcher()
    # poll 1: phase-1 only; poll 2: the bridge alone; poll 3: phase-2.
    s.add_batch(logs[:2])
    s.add_batch(logs[2:3])
    s.add_batch(logs[3:])
    assert len(s.journeys) == 1
    assert len(s.journeys[0].logs) == 5


def test_add_batch_returns_only_touched_journeys():
    s = Stitcher()
    s.add_batch(phase1_bridge_phase2("both"))
    touched = s.add_batch([
        mk(5, "Received inbound order event evt-new", eventId="evt-new"),
    ])
    assert len(touched) == 1
    assert touched[0].event_id == "evt-new"


# --- Pre-creation failure: eventId-only journey ------------------------------


def test_pre_creation_failure_is_eventid_only_journey():
    logs = [
        mk(0, "Received inbound order event evt-fail", eventId="evt-fail"),
        mk(1, "routing message to order.inbound.queue_error", level="ERROR",
           eventId="evt-fail"),
    ]
    _s, journeys = _journey_of(logs)
    assert len(journeys) == 1
    j = journeys[0]
    assert j.event_id == "evt-fail"
    assert j.order_id is None
    assert j.cart_header_id is None


# --- Ordering & idempotency --------------------------------------------------


def test_batch_processed_in_timestamp_order():
    # Deliberately shuffle: the bridge (t=2) arrives before phase 1 (t=0,1).
    logs = phase1_bridge_phase2("cart")
    shuffled = [logs[2], logs[0], logs[4], logs[1], logs[3]]
    _s, journeys = _journey_of(shuffled)
    assert len(journeys) == 1
    # logs stored in chronological order regardless of arrival order
    ts = [l.timestamp for l in journeys[0].logs]
    assert ts == sorted(ts)


def test_duplicate_log_id_is_ignored():
    # At-least-once queues re-deliver: same log_id must attach only once.
    log = mk(0, "Received inbound order event evt-dup", eventId="evt-dup", log_id="dup-1")
    s = Stitcher()
    s.add(log)
    s.add(log)
    assert len(s.journeys) == 1
    assert len(s.journeys[0].logs) == 1


def test_first_and_last_ts_track_the_span():
    _s, journeys = _journey_of(phase1_bridge_phase2("both"))
    j = journeys[0]
    assert j.first_ts == BASE
    assert j.last_ts == BASE + timedelta(seconds=4)
