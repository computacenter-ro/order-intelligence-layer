"""Tests for injector/inject.py — the pure baton-assembly layer (model B) + CLI.

The publish layer (aio-pika → sim.step.inbound) needs RabbitMQ and is covered by
integration once the runner exists; here we test everything that doesn't do I/O.
"""

import json

import pytest

from injector.inject import (
    INBOUND_QUEUE,
    _parse_args,
    build_baton,
)
from shared.models import Baton
from shared.scenarios import SCENARIOS, all_scenarios, compile_steps


@pytest.mark.parametrize("sid", range(1, 11))
def test_build_baton_is_valid_for_every_scenario(sid):
    baton = build_baton(SCENARIOS[sid])
    assert isinstance(baton, Baton)
    assert baton.scenario == sid
    assert baton.cursor == 0
    assert baton.steps == compile_steps(SCENARIOS[sid])


def test_event_id_is_minted_and_prefixed():
    baton = build_baton(SCENARIOS[1])
    assert baton.ctx.eventId.startswith("evt-")


def test_supplied_event_id_is_used():
    baton = build_baton(SCENARIOS[1], event_id="evt-fixed-123")
    assert baton.ctx.eventId == "evt-fixed-123"


@pytest.mark.parametrize("sid", range(1, 11))
def test_model_B_order_ids_absent_at_injection(sid):
    # The whole point of model B: order ids are born at creation, not injection.
    baton = build_baton(SCENARIOS[sid])
    assert baton.ctx.orderId is None
    assert baton.ctx.cartHeaderId is None


def test_ctx_carries_scenario_seed():
    s = SCENARIOS[3]  # US scenario
    baton = build_baton(s)
    assert baton.ctx.country == s.country
    assert baton.ctx.accountNumber == s.accountNumber
    assert baton.ctx.user == s.user
    assert baton.ctx.bridge_ids == s.bridge_ids
    assert baton.ctx.fail_at == s.fail_at


def test_flow_ids_are_unique_across_calls():
    ids = {build_baton(SCENARIOS[1]).flow_id for _ in range(20)}
    assert len(ids) == 20


def test_event_ids_are_unique_across_calls():
    ids = {build_baton(SCENARIOS[1]).ctx.eventId for _ in range(20)}
    assert len(ids) == 20


def test_baton_round_trips_through_json():
    # The publish layer serializes via model_dump_json — ensure it re-parses.
    baton = build_baton(SCENARIOS[6])
    payload = baton.model_dump_json()
    reparsed = Baton.model_validate_json(payload)
    assert reparsed.scenario == baton.scenario
    assert reparsed.steps == baton.steps
    assert reparsed.ctx.eventId == baton.ctx.eventId


def test_inbound_queue_name():
    assert INBOUND_QUEUE == "sim.step.inbound"


# --- CLI arg parsing ---------------------------------------------------------

def test_cli_requires_a_mode():
    with pytest.raises(SystemExit):
        _parse_args([])


def test_cli_scenario_ok():
    args = _parse_args(["--scenario", "6"])
    assert args.scenario == 6


def test_cli_rejects_out_of_range_scenario():
    with pytest.raises(SystemExit):
        _parse_args(["--scenario", "99"])


def test_cli_all_flag():
    args = _parse_args(["--all"])
    assert args.all is True


def test_cli_continuous_requires_positive_interval():
    with pytest.raises(SystemExit):
        _parse_args(["--mode", "continuous", "--interval", "0"])


def test_cli_scenario_and_all_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _parse_args(["--scenario", "1", "--all"])
