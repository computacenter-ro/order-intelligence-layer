"""Tests for shared/scenarios.py — the 10 canonical scenarios (+ clustering
test scenarios 11-17) + step compiler.

These are the ground-truth checks CLAUDE.md requires: outcomes match the
canonical table, chains truncate at fail_at, pre-creation failures stay
eventId-only, the five-hop ping-pong shape holds (two order-engine turns,
Inbound's Settings/JAM/SOLR leg before creation, the order_created close as
the success terminal), and every compiled chain builds a valid Baton.
"""

import json
from pathlib import Path

import pytest

from dataclasses import replace

from shared.models import Baton, BatonContext
from shared.scenarios import (
    AVALARA,
    CHECKER,
    ENRICH_SATELLITES,
    INBOUND,
    INBOUND_SATELLITES,
    JAM,
    ORDER_ENGINE,
    OUTBOUND,
    RSM,
    SCENARIOS,
    SETTINGS,
    SOLR,
    SPT,
    TRACK_TRACE,
    VALIDATOR,
    BLOCKS,
    all_scenarios,
    compile_steps,
    satellite_block,
)

FIXTURE = Path(__file__).resolve().parent.parent / "pipeline" / "data" / "mock-order-flows-v7.json"

# CLAUDE.md canonical table: {id: (outcome, fail_at)}
CANONICAL = {
    1: ("SUCCESS", None),
    2: ("SUCCESS", None),
    3: ("SUCCESS", None),
    4: ("INBOUND_TRANSFORM_FAILED", "transform"),
    5: ("ORDER_CREATION_FAILED", "create"),
    6: ("MARGIN_CHECK_FAILED", "margin"),
    7: ("VALIDATION_FAILED", "udf"),
    8: ("ENRICHMENT_FAILED", "spt"),
    9: ("AUTH_FAILED", "jam"),
    10: ("SAP_SUBMISSION_FAILED", "sap"),
}

# Scenarios that die before the order exists (eventId-only journeys): transform
# (4), creation itself (5), and the Inbound-leg failures — JAM (9) and the
# three Settings anomalies (15/16/17).
PRE_CREATION_FAILURES = [4, 5, 9, 15, 16, 17]

ALL_IDS = sorted(SCENARIOS)


def test_scenario_ids_are_a_contiguous_range_from_one():
    # Derived from SCENARIOS rather than a hardcoded count: scenarios are added
    # over time (10 canonical + the clustering/novel cases 11-17), and a literal
    # made this fail every time one was appended without telling us anything.
    # What actually matters is that the ids stay gapless and 1-based, since
    # tests and the injector address scenarios by number.
    assert sorted(SCENARIOS) == list(range(1, len(SCENARIOS) + 1))


@pytest.mark.parametrize("sid", range(1, 11))
def test_outcome_and_fail_at_match_canonical_table(sid):
    # Deliberately scoped to 1-10 only: CANONICAL mirrors CLAUDE.md's canonical
    # table, not "every scenario that exists" — scenarios 11-17 are clustering
    # test cases (shared/scenarios.py), not part of that documented table.
    outcome, fail_at = CANONICAL[sid]
    s = SCENARIOS[sid]
    assert s.outcome == outcome
    assert s.fail_at == fail_at


@pytest.mark.parametrize("sid", ALL_IDS)
def test_every_scenario_compiles_to_a_valid_baton(sid):
    s = SCENARIOS[sid]
    steps = compile_steps(s)
    ctx = BatonContext(eventId="evt-test", **s.context_seed())
    baton = Baton(flow_id="f-1", scenario=s.id, steps=steps, ctx=ctx)
    assert baton.steps == steps
    # ctx starts with no order ids (Invariant #1)
    assert ctx.orderId is None and ctx.cartHeaderId is None


@pytest.mark.parametrize("sid", ALL_IDS)
def test_terminal_equals_last_compiled_step(sid):
    s = SCENARIOS[sid]
    assert compile_steps(s)[-1] == s.terminal


def test_success_scenarios_end_at_inbound_close():
    """The SUCCESS terminal is Inbound's order_created close — the flow ENDS at
    Inbound, not at Track & Trace (which now registers mid-flow, right after
    creation)."""
    for s in all_scenarios():
        if s.outcome == "SUCCESS":
            assert compile_steps(s)[-1] == (INBOUND, BLOCKS.CLOSE)


def test_track_trace_registers_mid_flow_right_after_create():
    """Track & Trace moved to the middle of the flow: its registration step
    immediately follows creation and is never the last step of any chain."""
    for s in all_scenarios():
        steps = compile_steps(s)
        if (TRACK_TRACE, BLOCKS.REGISTER) not in steps:
            continue
        i = steps.index((TRACK_TRACE, BLOCKS.REGISTER))
        assert steps[i - 1] == (ORDER_ENGINE, BLOCKS.CREATE), (
            f"S{s.id}: register must immediately follow create"
        )
        assert i < len(steps) - 1, f"S{s.id}: register must not be terminal"


def test_the_five_hop_ping_pong_shape():
    """The success chain's coarse shape: receive → OE init → Inbound leg →
    request_create → OE create → ... → dispatch → submit → Inbound close."""
    steps = compile_steps(SCENARIOS[1])
    assert steps[0] == (INBOUND, BLOCKS.RECEIVE)
    assert steps[1] == (ORDER_ENGINE, BLOCKS.INIT)
    rc = steps.index((INBOUND, BLOCKS.REQUEST_CREATE))
    assert steps[rc + 1] == (ORDER_ENGINE, BLOCKS.CREATE)
    assert steps[-3:] == [
        (ORDER_ENGINE, BLOCKS.DISPATCH),
        (OUTBOUND, BLOCKS.SUBMIT),
        (INBOUND, BLOCKS.CLOSE),
    ]
    # Inbound's whole enrichment leg sits between init and request_create.
    for sat in INBOUND_SATELLITES:
        assert 1 < steps.index((sat, BLOCKS.SERVE)) < rc


def test_failure_truncates_inclusive_of_failing_block():
    # S8 fails at spt: chain must END at (spt, serve) — nothing after it.
    steps = compile_steps(SCENARIOS[8])
    assert steps[-1] == ("spt", BLOCKS.SERVE)
    assert (ORDER_ENGINE, "enrich_rsm_call") not in steps  # rsm comes after spt


@pytest.mark.parametrize("sid", PRE_CREATION_FAILURES)
def test_pre_creation_failures_are_event_id_only(sid):
    """Chains that die before (or at) creation contain nothing past create —
    no Track & Trace, no OE enrichment, no dispatch. The journey lives and dies
    with eventId only (CLAUDE.md invariant #3)."""
    s = SCENARIOS[sid]
    steps = compile_steps(s)
    assert s.reaches_creation is False
    if (ORDER_ENGINE, BLOCKS.CREATE) in steps:
        # S5: creation is ATTEMPTED and fails — it must be the last step.
        assert steps[-1] == (ORDER_ENGINE, BLOCKS.CREATE)
    assert (TRACK_TRACE, BLOCKS.REGISTER) not in steps
    assert (ORDER_ENGINE, BLOCKS.DISPATCH) not in steps
    assert not any(
        svc == ORDER_ENGINE and b.startswith("enrich_") for svc, b in steps
    ), f"S{sid}: OE enrichment appears in a pre-creation failure chain"


@pytest.mark.parametrize("sid", [sid for sid in ALL_IDS if SCENARIOS[sid].reaches_creation])
def test_post_creation_scenarios_reach_track_trace(sid):
    """Every flow that survives creation registers with Track & Trace — even
    ones that fail later (SPT-down, margin, validation, SAP). That looks odd
    against the old chain but is the documented step-11 position."""
    assert (TRACK_TRACE, BLOCKS.REGISTER) in compile_steps(SCENARIOS[sid])


def test_bridge_block_is_gone():
    """The creation-response bridge ack no longer exists — the only return hop
    after creation is the terminal order_created close. ``bridge_ids`` stays on
    the scenario/baton schema but is fully inert."""
    for s in all_scenarios():
        assert not any(b == "bridge" for _svc, b in compile_steps(s))
    # The knob's historical values are preserved (schema stability), unused.
    variants = {s.bridge_ids for s in all_scenarios()}
    assert {"both", "order", "cart"} <= variants


def test_inbound_leg_order_settings_then_jam_then_solr():
    """Inbound's pre-creation leg: Settings and JAM are documented as Inbound's
    calls; SOLR's position (last, for catalogue-line matching) is inferred —
    see the provenance note in shared/scenarios.py."""
    assert INBOUND_SATELLITES == [SETTINGS, JAM, SOLR]
    for s in all_scenarios():
        steps = compile_steps(s)
        enrich = [(svc, b) for svc, b in steps if b.startswith("enrich_")]
        if not enrich:
            continue  # transform failures never enrich
        assert enrich[0] == (INBOUND, "enrich_settings_call"), (
            f"S{s.id}: first enrichment call is {enrich[0]}, not Inbound→Settings"
        )


def test_canonical_satellite_order():
    """The full satellite order for a success chain: Inbound's leg first
    (pre-creation), then the order engine's (post-creation)."""
    def sats(sid):
        return [
            svc for svc, b in compile_steps(SCENARIOS[sid])
            if b in (BLOCKS.SERVE, BLOCKS.VALIDATE)
        ]

    assert sats(1) == ["settings", "jam", "solr", "spt", "rsm", "validator", "checker"]
    # The US success chain inserts Avalara between Validator and Checker
    # (auto-approval rules 1 → 1 → 3).
    assert sats(3) == [
        "settings", "jam", "solr", "spt", "rsm", "validator", "avalara", "checker",
    ]


def test_solr_reached_by_every_flow_that_survives_jam():
    """SOLR is the last stop on Inbound's leg, so every flow that gets past JAM
    reaches it — including ones that die later at create (S5) or SPT (8/11/12)."""
    for sid in (1, 2, 3, 5, 6, 7, 8, 10, 11, 12, 13, 14):
        assert (SOLR, BLOCKS.SERVE) in compile_steps(SCENARIOS[sid]), (
            f"S{sid} survives JAM but has no (solr, serve) step"
        )


@pytest.mark.parametrize("sid", [4, 9, 15, 16, 17])
def test_solr_absent_from_flows_that_die_before_it(sid):
    """...and is absent from flows that die earlier on the Inbound leg:
    transform (4), Settings (15/16/17) and JAM (9)."""
    assert (SOLR, BLOCKS.SERVE) not in compile_steps(SCENARIOS[sid])


def test_jam_failure_reaches_settings_but_not_solr():
    """S9's chain shape under the new leg order: Settings served, JAM failed,
    SOLR never reached, and nothing past creation exists."""
    steps = compile_steps(SCENARIOS[9])
    assert (SETTINGS, BLOCKS.SERVE) in steps
    assert steps[-1] == (JAM, BLOCKS.SERVE)
    assert (ORDER_ENGINE, BLOCKS.CREATE) not in steps


def test_avalara_is_us_only_between_validator_and_checker():
    """Avalara runs ONLY for US orders, at its documented position between
    Validator (rule 1) and Checker (rule 3) — hence inserted conditionally
    rather than listed in ENRICH_SATELLITES."""
    assert AVALARA not in ENRICH_SATELLITES
    for s in all_scenarios():
        steps = compile_steps(s)
        has_avalara = (AVALARA, BLOCKS.SERVE) in steps
        # Only US flows that actually REACH that point get it: S12 is US but
        # dies at SPT, so country alone is not sufficient.
        if not has_avalara:
            continue
        assert s.country == "US", f"S{s.id}: non-US flow has an Avalara step"
        sats = [svc for svc, b in steps if b in (BLOCKS.SERVE, BLOCKS.VALIDATE)]
        i = sats.index(AVALARA)
        assert sats[i - 1] == VALIDATOR, f"S{s.id}: Avalara not after Validator: {sats}"
        assert sats[i + 1] == CHECKER, f"S{s.id}: Avalara not before Checker: {sats}"

    # Concretely: S3 (US success) has it; S1/S2 (UK/DE success) do not; S12
    # (US, dies at SPT) does not.
    assert (AVALARA, BLOCKS.SERVE) in compile_steps(SCENARIOS[3])
    assert (AVALARA, BLOCKS.SERVE) not in compile_steps(SCENARIOS[1])
    assert (AVALARA, BLOCKS.SERVE) not in compile_steps(SCENARIOS[2])
    assert (AVALARA, BLOCKS.SERVE) not in compile_steps(SCENARIOS[12])


def test_settings_failure_contains_no_other_satellite():
    """A Settings failure fails at the FIRST enrichment step (of Inbound's leg
    now), so its chain holds exactly one satellite serve. Correct, not a gap."""
    for sid in (15, 16, 17):
        sats = [svc for svc, b in compile_steps(SCENARIOS[sid]) if b == BLOCKS.SERVE]
        assert sats == [SETTINGS], f"S{sid}: expected only settings, got {sats}"


def test_enrichment_uses_fine_grained_call_serve_resp_trio():
    # Each satellite appears as call -> serve -> resp, with the correct CALLER:
    # the order engine for its post-creation leg...
    steps = compile_steps(SCENARIOS[1])
    i = steps.index((ORDER_ENGINE, "enrich_spt_call"))
    assert steps[i:i + 3] == [
        (ORDER_ENGINE, "enrich_spt_call"),
        ("spt", BLOCKS.SERVE),
        (ORDER_ENGINE, "enrich_spt_resp"),
    ]
    # ...and INBOUND for the pre-creation leg.
    j = steps.index((INBOUND, "enrich_jam_call"))
    assert steps[j:j + 3] == [
        (INBOUND, "enrich_jam_call"),
        (JAM, BLOCKS.SERVE),
        (INBOUND, "enrich_jam_resp"),
    ]


def test_outcomes_match_reference_fixture_order():
    # v7 covers every scenario, so compare against the full set (length-derived,
    # not a hardcoded count — see test_scenario_ids_are_a_contiguous_range_from_one).
    ref = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert [f["_flow"] for f in ref] == sorted(SCENARIOS)
    assert [f["outcome"] for f in ref] == [
        SCENARIOS[i].outcome for i in sorted(SCENARIOS)
    ]


# --- deeper invariants + edge cases -------------------------------------------

# Phase-1 steps are everything that runs before the order exists: Inbound's
# receive, the OE's defaults-only first turn, the whole Inbound enrichment leg
# (Settings/JAM/SOLR serve included), request_create, and create itself (its
# logs stay eventId-only even while it mints the ids into ctx). Everything
# after create carries the order ids — if any of those precedes `create` in a
# chain, the correlation model is broken.
_PHASE1_STEPS = {
    (INBOUND, BLOCKS.RECEIVE),
    (ORDER_ENGINE, BLOCKS.INIT),
    (INBOUND, BLOCKS.REQUEST_CREATE),
    (ORDER_ENGINE, BLOCKS.CREATE),
    *{(INBOUND, f"enrich_{sat}_call") for sat in INBOUND_SATELLITES},
    *{(sat, BLOCKS.SERVE) for sat in INBOUND_SATELLITES},
    *{(INBOUND, f"enrich_{sat}_resp") for sat in INBOUND_SATELLITES},
}


def _is_phase2_step(step: tuple[str, str]) -> bool:
    return step not in _PHASE1_STEPS


@pytest.mark.parametrize("sid", ALL_IDS)
def test_phase_ordering_no_phase2_step_before_create(sid):
    """No order-id-bearing (phase-2) block may appear before order_engine/create.

    For chains that never reach create, there simply are no phase-2 steps,
    which trivially satisfies this.
    """
    steps = compile_steps(SCENARIOS[sid])
    if (ORDER_ENGINE, BLOCKS.CREATE) in steps:
        create_idx = steps.index((ORDER_ENGINE, BLOCKS.CREATE))
    else:
        create_idx = len(steps)  # no create → every step must be phase 1
    for i, step in enumerate(steps):
        if _is_phase2_step(step):
            assert i > create_idx, f"S{sid}: phase-2 step {step} at {i} precedes create"


def test_transform_failure_never_reaches_create():
    # Edge case: S4 fails at transform (phase 1) → create must be ABSENT.
    steps = compile_steps(SCENARIOS[4])
    assert (ORDER_ENGINE, BLOCKS.CREATE) not in steps


def test_creation_failure_ends_at_create():
    # Edge case: S5 fails AT create → create is PRESENT (attempted), and the
    # chain stops there. This distinguishes S5 from S4 and is the subtle
    # "reaches_creation=False yet create present" case.
    steps = compile_steps(SCENARIOS[5])
    assert steps[-1] == (ORDER_ENGINE, BLOCKS.CREATE)
    assert SCENARIOS[5].reaches_creation is False


@pytest.mark.parametrize("sat", ENRICH_SATELLITES)
def test_every_oe_satellite_trio_is_intact_in_full_success_chain(sat):
    # Parametrized across ALL of the order engine's satellites: each must appear
    # as an uninterrupted call -> serve/validate -> resp trio in S1's chain.
    steps = compile_steps(SCENARIOS[1])
    call = (ORDER_ENGINE, f"enrich_{sat}_call")
    assert call in steps, f"satellite {sat} missing from OE enrichment"
    i = steps.index(call)
    assert steps[i:i + 3] == [
        (ORDER_ENGINE, f"enrich_{sat}_call"),
        (sat, satellite_block(sat)),
        (ORDER_ENGINE, f"enrich_{sat}_resp"),
    ]


@pytest.mark.parametrize("sat", INBOUND_SATELLITES)
def test_every_inbound_satellite_trio_is_intact_in_full_success_chain(sat):
    steps = compile_steps(SCENARIOS[1])
    call = (INBOUND, f"enrich_{sat}_call")
    assert call in steps, f"satellite {sat} missing from Inbound enrichment"
    i = steps.index(call)
    assert steps[i:i + 3] == [
        (INBOUND, f"enrich_{sat}_call"),
        (sat, BLOCKS.SERVE),
        (INBOUND, f"enrich_{sat}_resp"),
    ]


@pytest.mark.parametrize("sid", ALL_IDS)
def test_no_orphan_enrichment_calls_or_responses(sid):
    # Every enrich_X_call has a matching enrich_X_resp AND vice versa — unless
    # the chain was truncated mid-trio by a satellite failure, in which case a
    # dangling call (and the satellite block) is expected but the resp must be
    # absent.
    steps = compile_steps(SCENARIOS[sid])
    calls = {b[len("enrich_"):-len("_call")] for _s, b in steps if b.startswith("enrich_") and b.endswith("_call")}
    resps = {b[len("enrich_"):-len("_resp")] for _s, b in steps if b.startswith("enrich_") and b.endswith("_resp")}
    # responses can never exist without their call
    assert resps <= calls, f"S{sid}: enrich responses without a call: {resps - calls}"
    # A call without a response is only allowed for the satellite the chain
    # truncated on — i.e. the LAST step is that satellite's server block (its
    # `<---` response never got emitted). NOTE: this satellite is derived from
    # the truncation point, not from `fail_at` — `fail_at` uses
    # scenario-vocabulary words ("margin", "udf", "sap") that differ from the
    # satellite name ("checker", "validator"/n-a, "outbound"/n-a).
    dangling = calls - resps
    if dangling:
        last_service, last_block = steps[-1]
        assert last_block in (BLOCKS.SERVE, BLOCKS.VALIDATE), (
            f"S{sid}: dangling enrich call(s) {dangling} but chain does not end "
            f"on a satellite block (ends on {steps[-1]})"
        )
        assert dangling == {last_service}, (
            f"S{sid}: dangling enrich call(s) {dangling} but chain truncated on "
            f"satellite {last_service!r}"
        )


def test_no_duplicate_steps_in_any_chain():
    for s in all_scenarios():
        steps = compile_steps(s)
        assert len(steps) == len(set(steps)), f"S{s.id}: duplicate steps in chain"


def test_unknown_fail_at_raises():
    # Edge case: a typo'd fail_at must NOT silently compile to a success chain.
    bad = replace(SCENARIOS[6], fail_at="margins")  # note the typo
    with pytest.raises(ValueError):
        compile_steps(bad)


def test_fail_at_none_produces_untruncated_chain():
    # A success scenario forced through the failing-step path shouldn't lose steps.
    s = SCENARIOS[1]
    assert compile_steps(s) == compile_steps(replace(s, fail_at=None))


def test_all_fail_at_values_resolve_to_a_real_step():
    # Every scenario's non-None fail_at must map to a step actually in its chain
    # (guards against a scenario whose fail_at can never fire).
    for s in all_scenarios():
        if s.fail_at is None:
            continue
        steps = compile_steps(s)  # must not raise
        assert steps, f"S{s.id}: empty chain"
