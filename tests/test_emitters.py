"""Tests for the mock service emitters (pipeline/services/*.py).

These drive each scenario's compiled step chain **in-process** (no RabbitMQ, no
collector) by calling the registered block handlers directly, exactly as
``pipeline/services/runner.py`` would, and assert:

  * the correlation-model invariants hold on the emitted LogLines
    (phase-1 = eventId only — including Inbound's whole Settings/JAM/SOLR leg
    and the order_data_ready ack; phase-2 = both order ids, never eventId; and
    NO single line links both id families as structured fields — the
    eventId->order-id join lives only in the order-engine creation logs'
    message text);
  * each scenario ends on its canonical terminal message (the load-bearing text
    the backend's journey assembler matches on) — the SUCCESS terminal being
    Inbound's order_created close, NOT Track & Trace (which registers mid-flow);
  * emitted lines carry the authentic "big project" identity (app_name / logger
    / host) that appears in the reference dataset.

The in-process driver mirrors runner.py's dispatch: emit the current block,
stop if it signals a fatal failure, else advance.
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from pipeline.services.registry import BLOCKS
from shared.models import Baton, BatonContext, LogLine
from shared.scenarios import SCENARIOS, all_scenarios, compile_steps

FIXTURE = Path(__file__).resolve().parent.parent / "pipeline" / "data" / "mock-order-flows-v8.json"

# Importing the service modules registers their blocks (import side-effect).
_SERVICE_MODULES = [
    "inbound", "order_engine", "spt", "rsm", "solr", "settings",
    "jam", "checker", "avalara", "validator", "outbound_osw", "track_trace",
]
for _m in _SERVICE_MODULES:
    importlib.import_module(f"pipeline.services.{_m}")

ALL_IDS = sorted(SCENARIOS)

# Scenarios that die before the order exists — eventId-only journeys.
PRE_CREATION_FAILURES = [4, 5, 9, 15, 16, 17]
# Scenarios whose flows actually create an order (mint the ids).
CREATING = [sid for sid in ALL_IDS if SCENARIOS[sid].reaches_creation]
# Scenarios that reach Inbound's enrichment leg (everything but transform).
REACHES_INBOUND_LEG = [sid for sid in ALL_IDS if sid != 4]


# --- in-process driver -------------------------------------------------------
async def _drive(sid: int) -> tuple[list[LogLine], BatonContext]:
    """Run scenario ``sid``'s chain in-process; return (emitted logs, final ctx)."""
    scenario = SCENARIOS[sid]
    ctx = BatonContext(eventId=f"evt-test-{sid}", **scenario.context_seed())
    baton = Baton(flow_id=f"flow-{sid}", scenario=scenario.id, steps=compile_steps(scenario), ctx=ctx)

    captured: list[LogLine] = []

    async def emit(logs: LogLine | list[LogLine]) -> int:
        items = logs if isinstance(logs, list) else [logs]
        for item in items:
            assert isinstance(item, LogLine)  # emitters must build through the model
            captured.append(item)
        return len(items)

    for cursor, step in enumerate(baton.steps):
        baton.cursor = cursor
        forward = await BLOCKS[step](baton, emit)
        if not forward:
            break  # fatal failure — chain stops (mirrors runner.py)
    return captured, baton.ctx


def _ack_lines(logs: list[LogLine]) -> list[LogLine]:
    """The order_data_ready consumption ack — the phase-1 return-leg line."""
    return [
        l for l in logs
        if l.logger == "c.c.inbound.listener.OrderDataReadyListener"
        and l.message.startswith("Received order_data_ready for event")
    ]


def _creation_lines(logs: list[LogLine]) -> list[LogLine]:
    return [
        l for l in logs
        if l.logger == "c.c.orderengine.service.OrderCreationService"
        and l.eventId is not None
    ]


# --- terminal messages (load-bearing for the backend) ------------------------
# Substring each scenario's LAST emitted line must contain. SUCCESS flows end
# on Inbound's order_created close; "for tracking" is deliberately NOT a
# terminal of anything anymore.
TERMINAL_CONTAINS = {
    1: "processing complete",
    2: "processing complete",
    3: "processing complete",
    4: "order.init_error",
    5: "creation failed for event",
    6: "blocked by margin check",
    7: "submission aborted",
    8: "processing aborted",
    9: "submission aborted",
    10: "order.create.sap_error",
    # 11-14: clustering test scenarios (shared/scenarios.py) — 11/12 and 6/13
    # reuse the SPT-down/margin-check fail paths verbatim, so their terminal
    # substrings match scenarios 8/6; 14 reuses the SAP-down path (matches 10).
    11: "processing aborted",
    12: "processing aborted",
    13: "blocked by margin check",
    14: "order.create.sap_error",
    # 15-17 are the novel/embedding-path cases: they never reach a recognized
    # terminal at all — their "terminal" is just the last emitted line, the
    # unrecognized Inbound-identity SettingsClient ERROR.
    15: "settings service unavailable",
    16: "settings service unavailable",
    17: "rejected",
    # 18 RECOVERS from its SPT blip, so it runs the full chain and ends exactly
    # where scenarios 1-3 do — on Inbound's close. That it shares a terminal with
    # the clean successes is the point: a transient failure is not a failure.
    18: "processing complete",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ALL_IDS)
async def test_scenario_ends_on_canonical_terminal(sid):
    logs, _ctx = await _drive(sid)
    assert logs, f"S{sid}: emitted no logs"
    assert TERMINAL_CONTAINS[sid] in logs[-1].message, (
        f"S{sid}: last line {logs[-1].message!r} lacks {TERMINAL_CONTAINS[sid]!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [1, 2, 3])
async def test_success_terminal_is_the_inbound_close(sid):
    """The SUCCESS terminal is Inbound's order_created close: inbound identity,
    phase-2 ids, and the load-bearing marker pair backend/journeys.py matches
    ("order_created" + "processing complete"). Track & Trace's registration
    line exists mid-flow but is NOT last."""
    logs, ctx = await _drive(sid)
    last = logs[-1]
    assert last.app_name == "cc-inbound-service"
    assert "order_created" in last.message and "processing complete" in last.message
    assert last.orderId == ctx.orderId and last.cartHeaderId == ctx.cartHeaderId
    assert last.eventId is None
    # Track & Trace registered earlier — mid-flow, before the checks.
    tt = [i for i, l in enumerate(logs) if "for tracking" in l.message]
    assert tt and tt[-1] < len(logs) - 1, f"S{sid}: tracking line missing or last"


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", ALL_IDS)
async def test_no_single_line_links_both_id_families_as_fields(sid):
    """The core invariant: NO emitted line carries an eventId FIELD together
    with an order-id FIELD. (The eventId->order-id join lives solely in the
    order-engine creation logs' message *text*, asserted separately.)"""
    logs, _ctx = await _drive(sid)
    for l in logs:
        has_evt = l.eventId is not None
        has_order = l.orderId is not None or l.cartHeaderId is not None
        assert not (has_evt and has_order), (
            f"S{sid}: line links both id families as fields: {l.message!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", PRE_CREATION_FAILURES)
async def test_pre_creation_failures_are_event_id_only(sid):
    """Journeys that die before creation — transform (4), creation (5), and the
    Inbound-leg failures JAM (9) and Settings (15/16/17) — never see an order
    id anywhere. That is invariant #3, not a data gap."""
    logs, ctx = await _drive(sid)
    assert ctx.orderId is None and ctx.cartHeaderId is None
    for l in logs:
        assert l.orderId is None and l.cartHeaderId is None, (
            f"S{sid}: order id appeared on a pre-creation journey: {l.message!r}"
        )
        # every line must still carry the eventId
        assert l.eventId == ctx.eventId


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", REACHES_INBOUND_LEG)
async def test_order_data_ready_ack_carries_only_event_id(sid):
    """The order_data_ready ack (the phase-1 return-leg line, replacing the old
    creation-response bridge): eventId ONLY — no order ids as fields, and none
    in its text. It links nothing, by design."""
    logs, _ctx = await _drive(sid)
    acks = _ack_lines(logs)
    assert len(acks) == 1, f"S{sid}: expected exactly one order_data_ready ack, got {len(acks)}"
    a = acks[0]
    assert a.eventId is not None
    assert a.orderId is None and a.cartHeaderId is None
    # No order id in the TEXT either (word-boundary — the same shapes the
    # stitcher mines): the ack must not be a de-facto family link.
    assert not re.search(r"\bORD-\d+\b", a.message)
    assert not re.search(r"\b\d{19}\b", a.message)


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", CREATING)
async def test_creation_logs_carry_order_ids_in_text_contract(sid):
    """CONTRACT (load-bearing, like the terminal messages): for every flow that
    creates an order, the order-engine creation logs must expose BOTH order ids
    in their message TEXT while carrying eventId as a field. This is the only
    thing that ties the two id families together — the stitcher's mining
    depends on it. Fail loudly if that text ever changes.
    """
    logs, ctx = await _drive(sid)
    creation = _creation_lines(logs)
    # The orderId must appear as ORD-<n> in some creation line's text...
    assert any(re.search(r"\bORD-\d+\b", l.message) for l in creation), (
        f"S{sid}: no creation log exposes an ORD- id in its text"
    )
    # ...and the 19-digit cartHeaderId in some creation line's text.
    assert any(re.search(r"\b\d{19}\b", l.message) for l in creation), (
        f"S{sid}: no creation log exposes a 19-digit cart id in its text"
    )
    # And those mined ids must equal the ctx ids the flow actually minted.
    joined = " ".join(l.message for l in creation)
    assert ctx.orderId in joined and ctx.cartHeaderId in joined, (
        f"S{sid}: creation-log text does not contain the minted ids"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", CREATING)
async def test_phase_boundary_at_creation(sid):
    """Everything up to and including the creation logs is phase 1 (eventId
    only) — Inbound's whole enrichment leg included; everything after carries
    both order ids and never eventId."""
    logs, _ctx = await _drive(sid)
    creation = _creation_lines(logs)
    assert creation, f"S{sid}: no creation logs"
    boundary = logs.index(creation[-1])
    for l in logs[: boundary + 1]:
        assert l.orderId is None and l.cartHeaderId is None, (
            f"S{sid}: phase-1 line carries an order id: {l.message!r}"
        )
        assert l.eventId is not None
    for l in logs[boundary + 1:]:
        assert l.orderId is not None and l.cartHeaderId is not None, (
            f"S{sid}: phase-2 line missing order ids: {l.message!r}"
        )
        assert l.eventId is None, f"S{sid}: phase-2 line carries eventId: {l.message!r}"


# --- authenticity: identity matches the reference dataset --------------------
def _fixture_identity() -> dict[str, dict[str, set[str]]]:
    """Map app_name -> {hosts, loggers} from the reference dataset."""
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ident: dict[str, dict[str, set[str]]] = {}
    for flow in data:
        for e in flow["events"]:
            slot = ident.setdefault(e["app_name"], {"hosts": set(), "loggers": set()})
            slot["hosts"].add(e["host"])
            slot["loggers"].add(e["logger"])
    return ident


# The v7 fixture is captured from these very emitters, so every real logger is
# in it and no allowance is needed. If a NEW logger is ever added ahead of a
# recapture, list it here temporarily — and empty this again once the next
# fixture version lands.
_REALIGNMENT_NEW_LOGGERS: dict[str, set[str]] = {}


@pytest.mark.asyncio
async def test_emitted_identity_matches_fixture():
    """Every emitted (app_name, host) and most loggers exist in the reference dataset.

    This is the 'looks like the big project' check: hosts must match exactly,
    and each emitted logger must be one the real service actually uses (guards
    against typos / drift in logger names). Loggers newly introduced by the
    realignment are allow-listed until the v7 recapture supersedes v6.
    """
    ident = _fixture_identity()
    # Collect emitted identity across ALL scenarios.
    emitted: dict[str, dict[str, set[str]]] = {}
    for s in all_scenarios():
        logs, _ctx = await _drive(s.id)
        for l in logs:
            slot = emitted.setdefault(l.app_name, {"hosts": set(), "loggers": set()})
            slot["hosts"].add(l.host)
            slot["loggers"].add(l.logger)

    for app_name, slot in emitted.items():
        assert app_name in ident, f"emitted unknown app_name {app_name!r}"
        # Host must match the real service's host exactly.
        assert slot["hosts"] <= ident[app_name]["hosts"], (
            f"{app_name}: emitted host(s) {slot['hosts'] - ident[app_name]['hosts']} "
            f"not in reference {ident[app_name]['hosts']}"
        )
        # Loggers: every emitted logger must be a real one for that service,
        # or a declared realignment addition.
        allowed = ident[app_name]["loggers"] | _REALIGNMENT_NEW_LOGGERS.get(app_name, set())
        unknown = slot["loggers"] - allowed
        assert not unknown, f"{app_name}: emitted logger(s) not in reference dataset: {unknown}"


# =============================================================================
# The two enrichment legs on the emitted stream: Inbound's pre-creation leg
# (Settings → JAM → SOLR) and the order engine's post-creation leg
# (SPT → RSM → Validator → [Avalara] → Checker).
#
# These assert on the EMITTED lines (what live journeys actually contain), not
# just on the compiled chain — that is the difference between "the chain says so"
# and "the dashboard will show it".
# =============================================================================
_SAT_APP = {
    "cc-settings-service": "settings",
    "cc-spt-service": "spt",
    "cc-rsm-service": "rsm",
    "cc-solr-service": "solr",
    "cc-jam-service": "jam",
    "cc-checker-service": "checker",
    "cc-avalara-service": "avalara",
    "cc-validator-service": "validator",
}

# Flows in which Settings serves happily (i.e. the Inbound leg starts) — every
# flow except transform (4) and the three Settings failures (15/16/17, whose
# failure variant is an Inbound-identity line, not a cc-settings-service one).
_SETTINGS_SERVES = [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
# Flows that reach SOLR: everything past JAM — including ones that die later
# at create (5) or SPT (8/11/12).
_REACHES_SOLR = [1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13, 14]


def _satellite_sequence(logs: list[LogLine]) -> list[str]:
    """The satellite services touched, in order, deduped for adjacency."""
    seq: list[str] = []
    for l in logs:
        sat = _SAT_APP.get(l.app_name)
        if sat and (not seq or seq[-1] != sat):
            seq.append(sat)
    return seq


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", _SETTINGS_SERVES)
async def test_settings_is_the_first_satellite_emitted(sid):
    """Settings opens Inbound's leg — asserted on the emitted stream, for every
    flow that enriches at all."""
    logs, _ctx = await _drive(sid)
    seq = _satellite_sequence(logs)
    assert seq and seq[0] == "settings", f"S{sid}: satellites start with {seq[:2]}"


@pytest.mark.asyncio
async def test_emitted_satellite_order_is_canonical():
    """Settings -> JAM -> SOLR (Inbound's leg) then SPT -> RSM -> Validator ->
    Checker (the engine's leg), with Avalara between Validator and Checker for
    the US flow only."""
    logs, _ = await _drive(1)
    assert _satellite_sequence(logs) == [
        "settings", "jam", "solr", "spt", "rsm", "validator", "checker",
    ]
    us, _ = await _drive(3)
    assert _satellite_sequence(us) == [
        "settings", "jam", "solr", "spt", "rsm", "validator", "avalara", "checker",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", _REACHES_SOLR)
async def test_solr_emits_in_every_flow_that_reaches_it(sid):
    """SOLR appears in every flow that survives JAM — successes AND journeys
    that fail later (create 5, SPT 8/11/12, margin 6/13, validation 7, SAP
    10/14). Its lines are PHASE 1 now: eventId only, no order ids."""
    logs, _ctx = await _drive(sid)
    solr = [l for l in logs if l.app_name == "cc-solr-service"]
    assert solr, f"S{sid}: no cc-solr-service lines emitted"
    for l in solr:
        assert l.eventId is not None, f"S{sid}: SOLR line lost its eventId"
        assert l.orderId is None and l.cartHeaderId is None, (
            f"S{sid}: SOLR is pre-creation now — it must not carry order ids"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [4, 9, 15, 16, 17])
async def test_solr_absent_from_flows_that_die_before_it(sid):
    """Absent from transform (4), the Settings failures (15/16/17) and the JAM
    failure (9) — all of which die earlier on Inbound's leg."""
    logs, _ctx = await _drive(sid)
    assert not [l for l in logs if l.app_name == "cc-solr-service"]


# --- scenario 18: the transient failure that recovers ------------------------

_SPT_CLIENT = "c.c.orderengine.client.SptClient"


@pytest.mark.asyncio
async def test_flaky_spt_emits_a_timeout_then_recovers():
    """The blip: a timeout ERROR and a retry WARN, then the normal success lines.

    The first two are the SAME wording the SPT-down variant uses for its first
    attempt — a blip and an outage are indistinguishable until the retry lands.
    """
    logs, _ctx = await _drive(18)
    spt_client = [l for l in logs if l.logger == _SPT_CLIENT]
    levels = [l.level for l in spt_client]
    assert "ERROR" in levels and "WARN" in levels

    error = next(l for l in spt_client if l.level == "ERROR")
    assert "SocketTimeoutException" in error.message
    retry = next(l for l in spt_client if l.level == "WARN")
    assert "attempt 2/3" in retry.message

    # The recovery marker comes from the SAME logger — that pairing is what
    # backend/journeys.py's unrecovered_errors reads.
    recovery = [l for l in spt_client if l.level == "INFO" and "succeeded" in l.message]
    assert recovery, "no recovery line from SptClient"
    assert logs.index(recovery[0]) > logs.index(error)

    # The satellite's own happy-path lines still ran: the retry is what succeeds.
    assert [l for l in logs if l.app_name == "cc-spt-service"
            and "Resolved price list code" in l.message]


@pytest.mark.asyncio
async def test_flaky_spt_never_emits_the_fatal_abort_line():
    """The one line that separates a blip from an outage.

    "Order processing aborted" is what _FAILURE_RULES maps to ENRICHMENT_FAILED;
    emitting it here would turn the recovery into a FAILED journey.
    """
    logs, _ctx = await _drive(18)
    assert not [l for l in logs if "processing aborted" in l.message.lower()]


@pytest.mark.asyncio
async def test_flaky_flow_runs_the_whole_chain_and_classifies_as_success():
    """End to end: no retry line classifies, and the journey resolves SUCCESS."""
    from backend.journeys import SUCCESS, classify_failure, detect_terminal

    logs, _ctx = await _drive(18)
    for log in logs:
        assert classify_failure(log.message) is None, (
            f"S18: {log.message!r} classifies as a failure"
        )
    assert detect_terminal(logs) == SUCCESS
    # It reached the far end of the chain — SAP submission and Inbound's close.
    assert [l for l in logs if l.app_name == "cc-outbound-osw"]
    assert logs[-1].app_name == "cc-inbound-service"


@pytest.mark.asyncio
async def test_flaky_flow_carries_a_real_error_on_a_success_journey():
    """The point of the scenario: an ERROR alone does not condemn a journey.

    Deliberate and noted in CLAUDE.md — these WARN/ERROR alerts hang off a
    SUCCESS journey and are never clustered (incident eligibility is
    FAILED/TIMED_OUT only).
    """
    from backend.journeys import unrecovered_errors

    logs, _ctx = await _drive(18)
    assert [l for l in logs if l.level == "ERROR"], "S18 should carry a real ERROR"
    assert unrecovered_errors(logs) == [], "S18's ERROR must read as recovered"


@pytest.mark.asyncio
async def test_the_spt_outage_still_fails_terminally():
    """The guard against over-correcting: scenario 8 is unchanged."""
    from backend.journeys import ENRICHMENT_FAILED, detect_terminal, unrecovered_errors

    logs, _ctx = await _drive(8)
    assert detect_terminal(logs) == ENRICHMENT_FAILED
    assert unrecovered_errors(logs), "a real outage must leave unrecovered ERRORs"


@pytest.mark.asyncio
async def test_avalara_emits_only_for_the_us_flow():
    """Avalara is US-only AND must be reached: S3 (US success) has it; S1/S2
    (UK/DE) do not; S12 is US but dies at SPT, so it does not either."""
    us, _ = await _drive(3)
    avalara = [l for l in us if l.app_name == "cc-avalara-service"]
    assert avalara, "S3 (US) emitted no cc-avalara-service lines"
    assert any("Ship-to address verified" in l.message for l in avalara)
    for sid in (1, 2, 6, 7, 9, 10, 12, 13, 14):
        logs, _ = await _drive(sid)
        assert not [l for l in logs if l.app_name == "cc-avalara-service"], (
            f"S{sid}: non-US (or not-reached) flow emitted Avalara lines"
        )


@pytest.mark.asyncio
async def test_avalara_sits_between_validator_and_checker():
    """The documented auto-approval order: Validator (rule 1) → Avalara (still
    rule 1, US only) → Checker (rule 3)."""
    logs, _ = await _drive(3)
    seq = _satellite_sequence(logs)
    assert seq[-3:] == ["validator", "avalara", "checker"]


@pytest.mark.asyncio
async def test_validator_no_longer_emits_avalara_verification_lines():
    """Ownership moved to cc-avalara-service, so the validator must NOT emit the
    verification (or its AvalaraClient request) — otherwise a US journey would
    show the address verified twice."""
    for sid in ALL_IDS:
        logs, _ctx = await _drive(sid)
        validator = [l for l in logs if l.app_name == "cc-validator-service"]
        assert not [l for l in validator if l.logger.endswith("client.AvalaraClient")], (
            f"S{sid}: validator still emits AvalaraClient lines"
        )
        assert not [l for l in validator if "Ship-to address verified" in l.message], (
            f"S{sid}: validator still emits the ship-to verification"
        )
        # The benign "Not implemented" ship-to strategy WARN is still expected
        # (it is in the AI service's suppression list) — it is not a duplicate.
        ship_to = [
            l for l in validator
            if l.logger.endswith("ValidateShipToWithAvalara")
        ]
        assert all(l.message == "Not implemented" for l in ship_to)


@pytest.mark.asyncio
async def test_us_verification_is_emitted_exactly_once():
    """The whole point of a standalone Avalara: exactly one ship-to
    verification line in a US journey."""
    logs, _ = await _drive(3)
    verified = [l for l in logs if "Ship-to address verified" in l.message]
    assert len(verified) == 1, f"expected 1 verification line, got {len(verified)}"
    assert verified[0].app_name == "cc-avalara-service"


# =============================================================================
# Minted-id uniqueness (order_engine._mint_ids)
#
# Uniqueness is load-bearing for CORRELATION, not cosmetic: the backend keys a
# journey on an alias set of ids, so two flows sharing an orderId are merged
# into one journey — the loser then never reaches a terminal marker and is swept
# as TIMED_OUT. The original implementation drew orderId from
# random.randint(1, 999), so a few dozen flows collided by the birthday bound.
# =============================================================================
def _mint(n: int) -> tuple[list[str], list[str]]:
    """Mint ``n`` fresh id pairs through the real _mint_ids."""
    from pipeline.services.order_engine import _mint_ids

    orders: list[str] = []
    carts: list[str] = []
    for i in range(n):
        ctx = BatonContext(eventId=f"evt-uniq-{i}", **SCENARIOS[1].context_seed())
        _mint_ids(ctx)
        orders.append(ctx.orderId)
        carts.append(ctx.cartHeaderId)
    return orders, carts


def test_minted_ids_are_unique():
    """No duplicates across many mints — the regression that caused TIMED_OUT."""
    orders, carts = _mint(2000)
    assert len(set(orders)) == len(orders), "duplicate orderId minted"
    assert len(set(carts)) == len(carts), "duplicate cartHeaderId minted"


def test_minted_cart_header_is_exactly_19_digits():
    """backend/stitching.py mines the cart id with ``\\b\\d{19}\\b`` — anchored at
    BOTH ends, so an 18- or 20-digit value silently stops correlating."""
    _, carts = _mint(500)
    assert {len(c) for c in carts} == {19}, sorted({len(c) for c in carts})
    assert all(c.isdigit() for c in carts)
    # The exact pattern the stitcher/semcache use must fullmatch.
    assert all(re.fullmatch(r"\d{19}", c) for c in carts)


def test_minted_order_id_matches_the_mined_pattern():
    """orderId must stay ``ORD-<digits>`` (stitching.py + semcache.py both use
    ``\\bORD-\\d+\\b``); the digit COUNT is free to grow."""
    orders, _ = _mint(500)
    assert all(re.fullmatch(r"ORD-\d+", o) for o in orders)


def test_mint_ids_keeps_preseeded_ids():
    """Deterministic tests pre-seed ids; _mint_ids must not overwrite them."""
    from pipeline.services.order_engine import _mint_ids

    ctx = BatonContext(eventId="evt-x", **SCENARIOS[1].context_seed())
    ctx.orderId = "ORD-6001"
    ctx.cartHeaderId = "1840927365018240001"
    _mint_ids(ctx)
    assert ctx.orderId == "ORD-6001"
    assert ctx.cartHeaderId == "1840927365018240001"


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [1, 2, 3])
async def test_concurrent_flows_never_share_order_ids(sid):
    """End-to-end guard: driving the same scenario repeatedly must yield a
    DISTINCT order id each run (this is what stops journey merging)."""
    seen_orders, seen_carts = set(), set()
    for _ in range(12):
        _logs, ctx = await _drive(sid)
        assert ctx.orderId not in seen_orders, f"orderId reused: {ctx.orderId}"
        assert ctx.cartHeaderId not in seen_carts, f"cartHeaderId reused: {ctx.cartHeaderId}"
        seen_orders.add(ctx.orderId)
        seen_carts.add(ctx.cartHeaderId)
