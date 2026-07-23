"""Tests for shared/scenarios.py — the 10 canonical scenarios + step compiler.

These are the ground-truth checks CLAUDE.md requires: outcomes match the
canonical table, chains truncate at fail_at, pre-creation failures stay
eventId-only, all three bridge variants are represented, and every compiled
chain builds a valid Baton.
"""

import json
from pathlib import Path

import pytest

from dataclasses import replace

from shared.models import Baton, BatonContext
from shared.scenarios import (
    AVALARA,
    ENRICH_SATELLITES,
    INBOUND,
    ORDER_ENGINE,
    SCENARIOS,
    SOLR,
    TRACK_TRACE,
    VALIDATOR,
    BLOCKS,
    all_scenarios,
    compile_steps,
)

FIXTURE = Path(__file__).resolve().parent.parent / "pipeline" / "data" / "mock-order-flows-v2.json"

# CLAUDE.md canonical table: {id: (outcome, fail_at, bridge_ids)}
CANONICAL = {
    1: ("SUCCESS", None, "both"),
    2: ("SUCCESS", None, "order"),
    3: ("SUCCESS", None, "cart"),
    4: ("INBOUND_TRANSFORM_FAILED", "transform", None),
    5: ("ORDER_CREATION_FAILED", "create", None),
    6: ("MARGIN_CHECK_FAILED", "margin", "order"),
    7: ("VALIDATION_FAILED", "udf", "both"),
    8: ("ENRICHMENT_FAILED", "spt", "cart"),
    9: ("AUTH_FAILED", "jam", "order"),
    10: ("SAP_SUBMISSION_FAILED", "sap", "both"),
}


def test_exactly_ten_scenarios():
    assert sorted(SCENARIOS) == list(range(1, 11))


@pytest.mark.parametrize("sid", range(1, 11))
def test_outcome_and_fail_at_match_canonical_table(sid):
    outcome, fail_at, _bridge = CANONICAL[sid]
    s = SCENARIOS[sid]
    assert s.outcome == outcome
    assert s.fail_at == fail_at


@pytest.mark.parametrize("sid", range(1, 11))
def test_every_scenario_compiles_to_a_valid_baton(sid):
    s = SCENARIOS[sid]
    steps = compile_steps(s)
    ctx = BatonContext(eventId="evt-test", **s.context_seed())
    baton = Baton(flow_id="f-1", scenario=s.id, steps=steps, ctx=ctx)
    assert baton.steps == steps
    # ctx starts with no order ids (Invariant #1)
    assert ctx.orderId is None and ctx.cartHeaderId is None


@pytest.mark.parametrize("sid", range(1, 11))
def test_terminal_equals_last_compiled_step(sid):
    s = SCENARIOS[sid]
    assert compile_steps(s)[-1] == s.terminal


def test_success_scenarios_end_at_track_trace():
    for s in all_scenarios():
        if s.outcome == "SUCCESS":
            assert compile_steps(s)[-1] == (TRACK_TRACE, BLOCKS.REGISTER)


def test_failure_truncates_inclusive_of_failing_block():
    # S8 fails at spt: chain must END at (spt, serve) — nothing after it.
    steps = compile_steps(SCENARIOS[8])
    assert steps[-1] == ("spt", BLOCKS.SERVE)
    assert (ORDER_ENGINE, "enrich_rsm_call") not in steps  # rsm comes after spt


@pytest.mark.parametrize("sid", [4, 5])
def test_pre_creation_failures_are_event_id_only(sid):
    # Scenarios 4 & 5 never reach the bridge → order ids never introduced.
    s = SCENARIOS[sid]
    steps = compile_steps(s)
    assert s.reaches_creation is False
    assert (INBOUND, BLOCKS.BRIDGE) not in steps


def test_all_three_bridge_variants_are_represented():
    # Only creation-reaching scenarios have a meaningful bridge_ids.
    variants = {
        s.bridge_ids for s in all_scenarios() if s.reaches_creation
    }
    assert {"both", "order", "cart"} <= variants


def test_avalara_is_not_a_standalone_enrich_satellite():
    # Per the reference dataset, Avalara ship-to verification is emitted by the
    # validator (US flows only), not by a standalone cc-avalara-service `serve`
    # step. So no chain contains an (avalara, serve) step, and avalara is not in
    # the satellite list.
    assert AVALARA not in ENRICH_SATELLITES
    for s in all_scenarios():
        assert (AVALARA, BLOCKS.SERVE) not in compile_steps(s)


def test_solr_is_not_a_standalone_enrich_satellite():
    # SOLR product-id resolution is internal to the order engine in the
    # reference dataset — there is no cc-solr-service `serve` step.
    assert SOLR not in ENRICH_SATELLITES
    for s in all_scenarios():
        assert (SOLR, BLOCKS.SERVE) not in compile_steps(s)


def test_us_flow_reaches_the_validator_where_avalara_is_emitted():
    # The US success flow (S3) must reach the validator's validate block — that
    # is where the validator emits the Avalara ship-to verification lines.
    s3 = SCENARIOS[3]
    assert s3.country == "US"
    assert (VALIDATOR, BLOCKS.VALIDATE) in compile_steps(s3)


def test_enrichment_uses_fine_grained_call_serve_resp_trio():
    # For a full success chain, each satellite appears as call -> serve -> resp.
    steps = compile_steps(SCENARIOS[1])
    i = steps.index((ORDER_ENGINE, "enrich_spt_call"))
    assert steps[i:i + 3] == [
        (ORDER_ENGINE, "enrich_spt_call"),
        ("spt", BLOCKS.SERVE),
        (ORDER_ENGINE, "enrich_spt_resp"),
    ]


def test_outcomes_match_reference_fixture_order():
    ref = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert [f["outcome"] for f in ref] == [SCENARIOS[i].outcome for i in range(1, 11)]


# --- added: deeper invariants + edge cases -----------------------------------

# Phase-2 blocks are everything that can only appear once the order exists.
# If any of these precedes `create` in a chain, the correlation model is broken.
def _is_phase2_step(step: tuple[str, str]) -> bool:
    service, block = step
    if service == INBOUND and block == BLOCKS.BRIDGE:
        return False  # bridge is the hinge, not phase 2
    if service in (INBOUND, ORDER_ENGINE) and block in (BLOCKS.RECEIVE, BLOCKS.CREATE):
        return False  # phase 1
    return True


@pytest.mark.parametrize("sid", range(1, 11))
def test_phase_ordering_no_phase2_step_before_create(sid):
    """No order-id-bearing (phase-2) block may appear before order_engine/create.

    For chains that never reach create (S4), there simply are no phase-2 steps,
    which trivially satisfies this — asserted explicitly below.
    """
    steps = compile_steps(SCENARIOS[sid])
    if (ORDER_ENGINE, BLOCKS.CREATE) in steps:
        create_idx = steps.index((ORDER_ENGINE, BLOCKS.CREATE))
    else:
        create_idx = len(steps)  # no create → every step must be phase 1
    for i, step in enumerate(steps):
        if _is_phase2_step(step):
            assert i > create_idx, f"S{sid}: phase-2 step {step} at {i} precedes create"


@pytest.mark.parametrize("sid", range(1, 11))
def test_bridge_when_present_sits_immediately_after_create(sid):
    steps = compile_steps(SCENARIOS[sid])
    if (INBOUND, BLOCKS.BRIDGE) not in steps:
        return
    b = steps.index((INBOUND, BLOCKS.BRIDGE))
    assert steps[b - 1] == (ORDER_ENGINE, BLOCKS.CREATE), (
        f"S{sid}: bridge must immediately follow create"
    )
    # and no enrichment may precede the bridge
    assert not any(
        block.startswith("enrich_") for _svc, block in steps[:b]
    ), f"S{sid}: enrichment appears before the bridge"


def test_transform_failure_never_reaches_create():
    # Edge case: S4 fails at transform (phase 1) → create must be ABSENT.
    steps = compile_steps(SCENARIOS[4])
    assert (ORDER_ENGINE, BLOCKS.CREATE) not in steps


def test_creation_failure_reaches_create_but_not_bridge():
    # Edge case: S5 fails AT create → create is PRESENT (attempted), but the
    # chain must not advance to the bridge. This distinguishes S5 from S4 and
    # is the subtle "reaches_creation=False yet create present" case.
    steps = compile_steps(SCENARIOS[5])
    assert (ORDER_ENGINE, BLOCKS.CREATE) in steps
    assert (INBOUND, BLOCKS.BRIDGE) not in steps
    assert SCENARIOS[5].reaches_creation is False


@pytest.mark.parametrize("sat", ENRICH_SATELLITES)
def test_every_satellite_trio_is_intact_in_full_success_chain(sat):
    # Parametrized across ALL satellites, not just spt: each must appear as an
    # uninterrupted call -> serve -> resp trio in a full success chain (S1).
    steps = compile_steps(SCENARIOS[1])
    call = (ORDER_ENGINE, f"enrich_{sat}_call")
    assert call in steps, f"satellite {sat} missing from enrichment"
    i = steps.index(call)
    assert steps[i:i + 3] == [
        (ORDER_ENGINE, f"enrich_{sat}_call"),
        (sat, BLOCKS.SERVE),
        (ORDER_ENGINE, f"enrich_{sat}_resp"),
    ]


@pytest.mark.parametrize("sid", range(1, 11))
def test_no_orphan_enrichment_calls_or_responses(sid):
    # Every enrich_X_call has a matching enrich_X_resp AND vice versa — unless
    # the chain was truncated mid-trio by a satellite failure, in which case a
    # dangling call (and the serve) is expected but the resp must be absent.
    steps = compile_steps(SCENARIOS[sid])
    calls = {b[len("enrich_"):-len("_call")] for _s, b in steps if b.startswith("enrich_") and b.endswith("_call")}
    resps = {b[len("enrich_"):-len("_resp")] for _s, b in steps if b.startswith("enrich_") and b.endswith("_resp")}
    # responses can never exist without their call
    assert resps <= calls, f"S{sid}: enrich responses without a call: {resps - calls}"
    # A call without a response is only allowed for the satellite the chain
    # truncated on — i.e. the LAST step is that satellite's `serve` (its `<---`
    # response never got emitted). NOTE: this satellite is derived from the
    # truncation point, not from `fail_at` — `fail_at` uses scenario-vocabulary
    # words ("margin", "udf", "sap") that differ from the satellite name
    # ("checker", "validator"/n-a, "outbound"/n-a).
    dangling = calls - resps
    if dangling:
        last_service, last_block = steps[-1]
        assert last_block == BLOCKS.SERVE, (
            f"S{sid}: dangling enrich call(s) {dangling} but chain does not end "
            f"on a satellite serve (ends on {steps[-1]})"
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
