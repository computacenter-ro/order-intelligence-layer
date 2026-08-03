"""Tests for pipeline/injector/inject.py — the pure baton-assembly layer (model B) + CLI.

The publish layer (aio-pika → sim.step.inbound) needs RabbitMQ and is covered by
integration once the runner exists; here we test everything that doesn't do I/O.
"""

import json

import pytest

from pipeline.injector.inject import (
    INBOUND_QUEUE,
    _parse_args,
    build_baton,
)
from shared.models import Baton
from shared.scenarios import SCENARIOS, all_scenarios, compile_steps


@pytest.mark.parametrize("sid", range(1, 16))
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


@pytest.mark.parametrize("sid", range(1, 16))
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


def test_cli_count_defaults_to_one():
    assert _parse_args(["--mode", "continuous"]).count == 1


def test_cli_count_is_parsed():
    assert _parse_args(["--mode", "continuous", "--count", "2"]).count == 2


def test_cli_rejects_count_below_one():
    with pytest.raises(SystemExit):
        _parse_args(["--mode", "continuous", "--count", "0"])


# --- continuous mode: batching + cycling -------------------------------------
# The deployed injector runs `--mode continuous --interval 60 --count 2`: two
# scenarios start together, run to completion on their own, then 60s later the
# next two start. These tests pin that batching/cycling contract without a broker:
# _run_one is replaced by a recorder and asyncio.sleep by a controlled abort.


class _StopLoop(Exception):
    """Breaks out of the infinite continuous loop after N sleeps."""


@pytest.fixture
def continuous_runner(monkeypatch):
    """Run ``_run_continuous`` for a fixed number of ticks, recording firings.

    Returns a callable ``(count, ticks) -> list[list[int]]`` — one inner list of
    scenario ids per tick, in the order they were fired.
    """
    import asyncio as _asyncio

    from pipeline.injector import inject as mod

    def run(count: int, ticks: int) -> list[list[int]]:
        batches: list[list[int]] = []
        current: list[int] = []

        async def fake_run_one(scenario_id, rabbitmq_url):
            current.append(scenario_id)

        async def fake_sleep(_seconds):
            # A tick ends at its sleep: bank the batch, stop after `ticks` of them.
            batches.append(list(current))
            current.clear()
            if len(batches) >= ticks:
                raise _StopLoop

        monkeypatch.setattr(mod, "_run_one", fake_run_one)
        monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
        try:
            _asyncio.run(mod._run_continuous(60.0, None, count))
        except _StopLoop:
            pass
        return batches

    return run


def test_continuous_fires_count_scenarios_per_tick(continuous_runner):
    batches = continuous_runner(2, 3)
    assert [len(b) for b in batches] == [2, 2, 2]


def test_continuous_default_count_fires_one_at_a_time(continuous_runner):
    """The pre-existing behaviour must be unchanged when --count is not given."""
    assert continuous_runner(1, 4) == [[1], [2], [3], [4]]


def test_continuous_pairs_scenarios_in_order(continuous_runner):
    """The requested sequence: (1,2) then (3,4) then (5,6)."""
    assert continuous_runner(2, 3) == [[1, 2], [3, 4], [5, 6]]


def test_continuous_cycle_wraps_without_losing_the_offset(continuous_runner):
    """With an ODD scenario count, pairs must straddle the wrap.

    17 scenarios / count=2 → ... (15,16) (17,1) (2,3) ... A "current batch index"
    implementation would restart at (1,2) after the wrap and fire scenario 1 twice
    as often as the rest; a single stride-advancing counter does not.
    """
    total = len(SCENARIOS)
    ticks = total  # enough to pass the wrap and resume
    batches = continuous_runner(2, ticks)

    expected = [
        [((2 * t + k) % total) + 1 for k in range(2)]
        for t in range(ticks)
    ]
    assert batches == expected

    # Every scenario fired, and no scenario fired twice as often as another.
    fired = [sid for batch in batches for sid in batch]
    assert set(fired) == set(SCENARIOS)
    counts = {sid: fired.count(sid) for sid in set(fired)}
    assert max(counts.values()) - min(counts.values()) <= 1


def test_continuous_batch_is_fired_concurrently(monkeypatch):
    """The batch must start together (gather), not one-after-another.

    Pinned by making the FIRST firing block until the second has started: with
    sequential awaits this deadlocks (and the test times out / fails), so it can
    only pass if both coroutines are in flight at once.
    """
    import asyncio as _asyncio

    from pipeline.injector import inject as mod

    started = _asyncio.Event()

    async def fake_run_one(scenario_id, rabbitmq_url):
        if scenario_id == 1:
            await _asyncio.wait_for(started.wait(), timeout=1.0)  # needs #2 running
        else:
            started.set()

    async def fake_sleep(_seconds):
        raise _StopLoop

    monkeypatch.setattr(mod, "_run_one", fake_run_one)
    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)

    async def go():
        try:
            await mod._run_continuous(60.0, None, 2)
        except _StopLoop:
            pass

    _asyncio.run(go())  # completes only if both ran concurrently
    assert started.is_set()
