"""Tests for backend/stitching.py — single-pass, incremental journey stitching.

These pin the Correlation Model from CLAUDE.md as executable invariants:

* phase-1 logs carry only ``eventId``; the bridge ack carries ONLY ``eventId``
  (no order ids, as fields OR text); phase-2 logs carry both order ids and never
  ``eventId``; NO single log line links the two id families as structured fields;
* the eventId->order-id join is recovered by MINING the order-engine creation
  logs' message text (which carry ``eventId`` as a field and the order ids in
  their text) — mined ids are registered as aliases exactly like field ids;
* single-pass, timestamp-ordered stitching produces exactly one journey even
  though no line links both families as fields;
* assembly is incremental — logs split across successive polls (including a
  split between the creation logs and phase 2) still stitch into one journey;
* pre-creation failures produce complete, valid journeys identified only by
  ``eventId``;
* ``accountNumber`` is NEVER used to correlate, and an unrelated 19-digit number
  in a message must NOT merge journeys.

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


# --- Honest full flow: mining closes the join --------------------------------

ORDER = "ORD-6001"
CART = "1840927365018240001"


def honest_full_flow() -> list[LogLine]:
    """A full journey with the HONEST bridge (eventId only).

    The eventId->order-id join lives ONLY in the creation logs' message text:
    ``eventId`` is a structured field there, and the order ids appear in the
    text (exactly as the order-engine ``create`` block emits them). The bridge
    ack carries eventId only. Phase-2 logs carry the order-id fields, no eventId.
    No single line links both families as structured fields.
    """
    ev = "evt-abc"
    return [
        mk(0, "Received inbound order event evt-abc", eventId=ev, accountNumber="81036533"),
        # Creation logs: eventId FIELD + order ids in TEXT (the mining source).
        mk(1, f"Created cart header {CART}", eventId=ev),
        mk(2, f"Generated order number {ORDER} for cart header {CART}", eventId=ev),
        # Honest bridge: eventId only, no order ids anywhere.
        mk(3, "Received order creation response for event evt-abc: status=CREATED", eventId=ev),
        # Phase 2: order-id fields only.
        mk(4, f"Get order by Order Number:{ORDER}", orderId=ORDER, cartHeaderId=CART),
        mk(5, f"Registered order {ORDER} for tracking", orderId=ORDER, cartHeaderId=CART),
    ]


def test_honest_flow_stitches_one_journey():
    # No line links both id families as fields, yet mining the creation-log text
    # ties eventId to the order ids → exactly one journey.
    _s, journeys = _journey_of(honest_full_flow())
    assert len(journeys) == 1
    assert len(journeys[0].logs) == 6


def test_no_line_links_both_families_as_fields():
    # Guard the honest-corpus premise the mining must survive.
    for l in honest_full_flow():
        has_evt = l.eventId is not None
        has_order = l.orderId is not None or l.cartHeaderId is not None
        assert not (has_evt and has_order), f"line links both families as fields: {l.message!r}"


def test_journey_accumulates_all_alias_ids_via_mining():
    _s, journeys = _journey_of(honest_full_flow())
    j = journeys[0]
    # order_id / cart_header_id were MINED from the creation-log text, yet they
    # populate the journey's alias fields exactly like structured-field ids.
    assert j.event_id == "evt-abc"
    assert j.order_id == ORDER
    assert j.cart_header_id == CART


def test_lookup_by_any_alias_returns_same_journey():
    s, _journeys = _journey_of(honest_full_flow())
    j = s.journey_for("evt-abc")
    assert j is not None
    assert s.journey_for(ORDER) is j          # mined id resolves to the journey
    assert s.journey_for(CART) is j


# --- Separation: distinct journeys never merge -------------------------------


def test_two_independent_flows_stay_separate():
    a = honest_full_flow()
    b = [
        mk(0.5, "Received inbound order event evt-xyz", eventId="evt-xyz"),
        mk(1.5, "Generated order number ORD-7777 for cart header 9990000000000000000",
           eventId="evt-xyz"),  # mining source for flow b
        mk(2.5, "Received order creation response for event evt-xyz: status=CREATED",
           eventId="evt-xyz"),
        mk(3.5, "Registered order ORD-7777 for tracking",
           orderId="ORD-7777", cartHeaderId="9990000000000000000"),
    ]
    _s, journeys = _journey_of(a + b)
    assert len(journeys) == 2


def test_unrelated_19_digit_number_does_not_merge_journeys():
    # A message containing an unrelated 19-digit run must NOT be mined as a
    # cartHeaderId that links two distinct journeys. Here flow b's benign log
    # happens to contain a 19-digit number equal to flow a's cart id would be a
    # real collision; we instead assert a DIFFERENT unrelated 19-digit number in
    # an otherwise-unrelated journey never pulls it into flow a.
    a = honest_full_flow()  # cart id CART = 1840927365018240001
    b = [
        mk(0.5, "Received inbound order event evt-zzz", eventId="evt-zzz"),
        # An unrelated 19-digit number (NOT CART) in a benign phase-1 line.
        mk(1.5, "Reserved buffer size 1234567890123456789 bytes", eventId="evt-zzz"),
        mk(2.5, "routing message to order.inbound.queue_error", level="ERROR",
           eventId="evt-zzz"),
    ]
    _s, journeys = _journey_of(a + b)
    # Two journeys: flow a (success) and flow b (eventId-only failure). The stray
    # 19-digit number becomes an alias of flow b only — it never touches flow a.
    assert len(journeys) == 2
    jb = next(j for j in journeys if j.event_id == "evt-zzz")
    assert jb.order_id is None
    # flow a keeps its own, distinct cart id
    ja = next(j for j in journeys if j.event_id == "evt-abc")
    assert ja.cart_header_id == CART


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


def test_split_across_polls_between_creation_and_phase2():
    # The hardest cross-poll split now: creation logs (which mine the order ids)
    # in one poll, phase-2 (order-id fields) in another, with the eventId-only
    # bridge on the boundary. Mining must persist across polls (alias map lives
    # in the Stitcher) so the join still closes.
    logs = honest_full_flow()  # [event, created, generated, bridge, get, registered]
    s = Stitcher()
    s.add_batch(logs[:3])   # phase-1 + both creation logs (order ids mined here)
    s.add_batch(logs[3:4])  # the eventId-only bridge alone
    s.add_batch(logs[4:])   # phase-2 (order-id fields)
    assert len(s.journeys) == 1
    assert len(s.journeys[0].logs) == 6


def test_add_batch_returns_only_touched_journeys():
    s = Stitcher()
    s.add_batch(honest_full_flow())
    touched = s.add_batch([
        mk(6, "Received inbound order event evt-new", eventId="evt-new"),
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
    # Deliberately shuffle arrival order; stitching sorts by timestamp first.
    logs = honest_full_flow()
    shuffled = [logs[3], logs[0], logs[5], logs[2], logs[1], logs[4]]
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
    _s, journeys = _journey_of(honest_full_flow())
    j = journeys[0]
    assert j.first_ts == BASE
    assert j.last_ts == BASE + timedelta(seconds=5)
