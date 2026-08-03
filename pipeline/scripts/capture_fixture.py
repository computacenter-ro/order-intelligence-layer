"""Dev harness: regenerate the reference fixture (``pipeline/data/mock-order-flows-vN.json``).

The fixture is the tests' oracle for what the pipeline actually emits, so it is
**captured from the emitters, never hand-written** — that is what keeps it
honest when a service's log text changes. This script exists because the shape
the tests read (one object per flow, with ``_flow`` / ``outcome`` / ``events``)
is not what ``capture_flow.py`` dumps, and regenerating it by hand invites
exactly the drift the fixture is supposed to catch.

It drives each scenario's compiled step chain **in-process** — the same dispatch
``pipeline/services/runner.py`` performs, minus RabbitMQ and the collector — so
it needs no running infrastructure and is deterministic apart from the minted
ids and timestamps.

Usage::

    python -m pipeline.scripts.capture_fixture --out pipeline/data/mock-order-flows-v8.json

Per-flow keys (the contract tests/test_journeys.py and tests/test_scenarios.py
read):

``_flow``      the scenario id (fixture order == ``sorted(SCENARIOS)``)
``eventId``    minted per flow; ``orderId`` / ``cartHeaderId`` are null for the
               pre-creation failures, which never mint them — invariant #3
``scenario``   the scenario's prose name
``outcome``    the scenario's canonical outcome
``events``     every emitted LogLine, in emission order, serialized as JSON
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import uuid
from pathlib import Path

from pipeline.services.registry import BLOCKS
from shared.models import Baton, BatonContext, LogLine
from shared.scenarios import SCENARIOS, compile_steps

# Importing the service modules registers their blocks (import side-effect):
# without this BLOCKS is empty and every dispatch below would KeyError.
_SERVICE_MODULES = [
    "inbound", "order_engine", "spt", "rsm", "solr", "settings",
    "jam", "checker", "avalara", "validator", "outbound_osw", "track_trace",
]
for _module in _SERVICE_MODULES:
    importlib.import_module(f"pipeline.services.{_module}")


async def capture_flow(scenario_id: int) -> dict:
    """Drive one scenario's chain in-process and return its fixture entry."""
    scenario = SCENARIOS[scenario_id]
    ctx = BatonContext(
        eventId=f"evt-{uuid.uuid4()}",
        **scenario.context_seed(),
    )
    baton = Baton(
        flow_id=str(uuid.uuid4()),
        scenario=scenario.id,
        steps=compile_steps(scenario),
        ctx=ctx,
    )

    captured: list[LogLine] = []

    async def emit(logs: LogLine | list[LogLine]) -> int:
        items = logs if isinstance(logs, list) else [logs]
        captured.extend(items)
        return len(items)

    for cursor, step in enumerate(baton.steps):
        baton.cursor = cursor
        # Mirrors runner.py: a block signalling a fatal failure stops the chain
        # (the baton is not forwarded). A flaky block returns True and the flow
        # continues — which is the whole point of scenario 18.
        if not await BLOCKS[step](baton, emit):
            break

    return {
        "_flow": scenario.id,
        "eventId": baton.ctx.eventId,
        "orderId": baton.ctx.orderId,
        "cartHeaderId": baton.ctx.cartHeaderId,
        "scenario": scenario.name,
        "outcome": scenario.outcome,
        "events": [log.model_dump(mode="json") for log in captured],
    }


async def capture_all() -> list[dict]:
    """Every scenario, in id order — the fixture's required ordering."""
    return [await capture_flow(sid) for sid in sorted(SCENARIOS)]


async def _run(args: argparse.Namespace) -> int:
    flows = await capture_all()
    out = Path(args.out)
    out.write_text(json.dumps(flows, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(len(flow["events"]) for flow in flows)
    print(f"[capture-fixture] {len(flows)} flows, {total} log lines -> {out.resolve()}")
    for flow in flows:
        print(
            f"  scenario {flow['_flow']:>2} {flow['outcome']:<24} "
            f"{len(flow['events']):>3} lines"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the reference fixture from the emitters."
    )
    parser.add_argument(
        "--out",
        default="pipeline/data/mock-order-flows-v8.json",
        help="output JSON path (keep the previous version — they are kept for history)",
    )
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
