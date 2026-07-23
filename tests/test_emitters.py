"""Tests for the mock service emitters (pipeline/services/*.py).

These drive each scenario's compiled step chain **in-process** (no RabbitMQ, no
collector) by calling the registered block handlers directly, exactly as
``pipeline/services/runner.py`` would, and assert:

  * the correlation-model invariants hold on the emitted LogLines
    (phase-1 = eventId only; the bridge ack = eventId ONLY; phase-2 = both
    order ids, never eventId; and NO single line links both id families as
    structured fields — the eventId->order-id join lives only in the
    order-engine creation logs' message text);
  * each scenario ends on its canonical terminal message (the load-bearing text
    the backend's journey assembler matches on);
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

FIXTURE = Path(__file__).resolve().parent.parent / "pipeline" / "data" / "mock-order-flows-v3.json"

# Importing the service modules registers their blocks (import side-effect).
_SERVICE_MODULES = [
    "inbound", "order_engine", "spt", "rsm", "settings",
    "jam", "checker", "validator", "outbound_osw", "track_trace",
]
for _m in _SERVICE_MODULES:
    importlib.import_module(f"pipeline.services.{_m}")


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


def _bridge_lines(logs: list[LogLine]) -> list[LogLine]:
    """THE bridge line(s): the inbound ResponseListener 'Received ... response'."""
    return [
        l for l in logs
        if l.logger == "c.c.inbound.listener.ResponseListener"
        and l.message.startswith("Received order creation response for event")
    ]


# --- terminal messages (load-bearing for the backend) ------------------------
# Substring each scenario's LAST emitted line must contain.
TERMINAL_CONTAINS = {
    1: "for tracking",
    2: "for tracking",
    3: "for tracking",
    4: "order.inbound.dlq",
    5: "no order was created",
    6: "blocked by margin check",
    7: "submission aborted",
    8: "processing aborted",
    9: "submission aborted",
    10: "order.outbound.dlq",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", range(1, 11))
async def test_scenario_ends_on_canonical_terminal(sid):
    logs, _ctx = await _drive(sid)
    assert logs, f"S{sid}: emitted no logs"
    assert TERMINAL_CONTAINS[sid] in logs[-1].message, (
        f"S{sid}: last line {logs[-1].message!r} lacks {TERMINAL_CONTAINS[sid]!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", range(1, 11))
async def test_no_single_line_links_both_id_families_as_fields(sid):
    """The honest-bridge invariant: NO emitted line carries an eventId FIELD
    together with an order-id FIELD. The bridge is no longer an exception — it
    now carries eventId only. (The eventId->order-id join lives solely in the
    order-engine creation logs' message *text*, asserted separately.)"""
    logs, _ctx = await _drive(sid)
    for l in logs:
        has_evt = l.eventId is not None
        has_order = l.orderId is not None or l.cartHeaderId is not None
        assert not (has_evt and has_order), (
            f"S{sid}: line links both id families as fields: {l.message!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [4, 5])
async def test_pre_creation_failures_are_event_id_only(sid):
    """Scenarios 4 & 5 never create an order → no order ids ever appear."""
    logs, ctx = await _drive(sid)
    assert ctx.orderId is None and ctx.cartHeaderId is None
    for l in logs:
        assert l.orderId is None and l.cartHeaderId is None, (
            f"S{sid}: order id appeared on a pre-creation journey: {l.message!r}"
        )
        # every line must still carry the eventId
        assert l.eventId == ctx.eventId


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [1, 2, 3, 6, 7, 8, 9, 10])
async def test_bridge_carries_only_event_id(sid):
    """The honest bridge: eventId ONLY — no order ids as fields, and none in its
    text (the message ends ``: status=CREATED``). It no longer links the id
    families; ``bridge_ids`` is inert."""
    logs, _ctx = await _drive(sid)
    bridges = _bridge_lines(logs)
    assert len(bridges) == 1, f"S{sid}: expected exactly one bridge line, got {len(bridges)}"
    b = bridges[0]
    assert b.eventId is not None
    assert b.orderId is None and b.cartHeaderId is None, (
        f"S{sid}: bridge still carries an order-id field: {b.message!r}"
    )
    assert b.message.endswith(": status=CREATED"), (
        f"S{sid}: bridge message is not the honest form: {b.message!r}"
    )
    # No order id in the TEXT either (word-boundary — the same shapes the
    # stitcher mines): the bridge must not be a de-facto family link.
    assert not re.search(r"\bORD-\d+\b", b.message)
    assert not re.search(r"\b\d{19}\b", b.message)


@pytest.mark.asyncio
@pytest.mark.parametrize("sid", [1, 2, 3, 6, 7, 8, 9, 10])
async def test_creation_logs_carry_order_ids_in_text_contract(sid):
    """CONTRACT (load-bearing, like the terminal messages): for every flow that
    creates an order, the order-engine creation logs must expose BOTH order ids
    in their message TEXT while carrying eventId as a field. This is the only
    thing that ties the two id families together now that the bridge is honest —
    the stitcher's mining depends on it. Fail loudly if that text ever changes.
    """
    logs, ctx = await _drive(sid)
    creation = [
        l for l in logs
        if l.logger == "c.c.orderengine.service.OrderCreationService"
        and l.eventId is not None
    ]
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
async def test_phase1_lines_never_carry_order_ids():
    """Every line emitted before the bridge carries only eventId (no order ids)."""
    logs, _ctx = await _drive(1)
    bridges = _bridge_lines(logs)
    assert bridges
    bridge_idx = logs.index(bridges[0])
    for l in logs[:bridge_idx]:
        assert l.orderId is None and l.cartHeaderId is None, (
            f"phase-1 line carries an order id: {l.message!r}"
        )
        assert l.eventId is not None


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


@pytest.mark.asyncio
async def test_emitted_identity_matches_fixture():
    """Every emitted (app_name, host) and most loggers exist in the reference dataset.

    This is the 'looks like the big project' check: hosts must match exactly,
    and each emitted logger must be one the real service actually uses (guards
    against typos / drift in logger names).
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
        # Loggers: every emitted logger must be a real one for that service.
        unknown = slot["loggers"] - ident[app_name]["loggers"]
        assert not unknown, f"{app_name}: emitted logger(s) not in reference dataset: {unknown}"
