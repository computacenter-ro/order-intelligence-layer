"""[injector] Starts flows — stands in for "Orders B2B / Salesforce".

The injector is the *ignition* of the simulation. Nothing physically arrives
from B2B/Salesforce here; instead the injector fakes that arrival by minting a
fresh flow identity, compiling the scenario's step chain into a Baton, and
publishing it to the first service's control queue (``sim.step.inbound``). The
runner + services take it from there.

Id model (model "B" — ids are born at creation, CLAUDE.md "THE CORRELATION
MODEL"): the injector mints **only** ``eventId``. ``orderId`` / ``cartHeaderId``
do not exist yet — they are created by the order_engine ``create`` block at run
time and first surface in logs at the bridge. So the starting ctx carries
``eventId`` with ``orderId``/``cartHeaderId`` left ``None``.

Design: split into a pure **baton-assembly** layer (no I/O, unit-testable) and a
thin **publish** layer (aio-pika). Only the publish layer knows about RabbitMQ,
so the queue/transport convention (shared with runner.py) is isolated to one
place.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from shared.models import Baton, BatonContext
from shared.scenarios import SCENARIOS, all_scenarios, compile_steps, Scenario

# First control queue — where every flow starts (CLAUDE.md [1]).
INBOUND_QUEUE = "sim.step.inbound"
DEFAULT_RABBITMQ_URL = "amqp://guest:guest@localhost:5672/"


def _rabbitmq_url() -> str:
    return os.environ.get("RABBITMQ_URL", DEFAULT_RABBITMQ_URL)


def _new_event_id() -> str:
    """A fresh pre-creation identifier: ``evt-<uuid>``."""
    return f"evt-{uuid.uuid4()}"


# --- Layer 1: pure baton assembly (no I/O) -----------------------------------
def build_baton(scenario: Scenario, *, event_id: str | None = None) -> Baton:
    """Assemble a ready-to-publish Baton for ``scenario`` (model B).

    Mints ``eventId`` (unless one is supplied, for deterministic tests) and
    seeds the ctx with it. ``orderId``/``cartHeaderId`` are deliberately left
    ``None`` — they are born later, in the order_engine ``create`` block.
    """
    ctx = BatonContext(
        eventId=event_id or _new_event_id(),
        **scenario.context_seed(),  # account, country, user, lines, bridge_ids, fail_at
    )
    # Invariant #1: order ids do not exist pre-creation.
    assert ctx.orderId is None and ctx.cartHeaderId is None

    return Baton(
        flow_id=str(uuid.uuid4()),
        scenario=scenario.id,
        steps=compile_steps(scenario),
        cursor=0,
        ctx=ctx,
    )


# --- Layer 2: publish (aio-pika) ---------------------------------------------
async def publish_baton(baton: Baton, *, rabbitmq_url: str | None = None) -> None:
    """Publish a single baton to the inbound control queue.

    Imports aio-pika lazily so the pure assembly layer (and its tests) don't
    require RabbitMQ to be installed/running.
    """
    import aio_pika

    connection = await aio_pika.connect_robust(rabbitmq_url or _rabbitmq_url())
    async with connection:
        channel = await connection.channel()
        await channel.declare_queue(INBOUND_QUEUE, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=baton.model_dump_json().encode(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=INBOUND_QUEUE,
        )


async def inject_scenario(scenario: Scenario, *, rabbitmq_url: str | None = None) -> Baton:
    """Build + publish one scenario's baton. Returns the baton (for logging/tests)."""
    baton = build_baton(scenario)
    await publish_baton(baton, rabbitmq_url=rabbitmq_url)
    return baton


# --- CLI ---------------------------------------------------------------------
async def _run_one(scenario_id: int, rabbitmq_url: str | None) -> None:
    scenario = SCENARIOS[scenario_id]
    baton = await inject_scenario(scenario, rabbitmq_url=rabbitmq_url)
    print(f"[injector] fired scenario {scenario.id} ({scenario.outcome}) "
          f"flow_id={baton.flow_id} eventId={baton.ctx.eventId}")


async def _run_all(stagger: float, rabbitmq_url: str | None) -> None:
    for scenario in all_scenarios():
        await _run_one(scenario.id, rabbitmq_url)
        await asyncio.sleep(stagger)  # stagger so flows interleave realistically


async def _run_continuous(interval: float, rabbitmq_url: str | None) -> None:
    scenarios = all_scenarios()
    i = 0
    while True:
        scenario = scenarios[i % len(scenarios)]
        await _run_one(scenario.id, rabbitmq_url)
        i += 1
        await asyncio.sleep(interval)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start simulated order flows.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", type=int, metavar="N",
                       help="fire a single scenario (1-10)")
    group.add_argument("--all", action="store_true",
                       help="fire all 10 scenarios, staggered")
    group.add_argument("--mode", choices=["continuous"],
                       help="continuous injection (use with --interval)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between flows in continuous mode (default 5)")
    parser.add_argument("--stagger", type=float, default=1.0,
                        help="seconds between flows in --all mode (default 1)")
    args = parser.parse_args(argv)

    if args.scenario is not None and args.scenario not in SCENARIOS:
        parser.error(f"--scenario must be one of {sorted(SCENARIOS)}")
    if args.mode == "continuous" and args.interval <= 0:
        parser.error("--interval must be > 0")
    return args


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    url = _rabbitmq_url()
    if args.scenario is not None:
        asyncio.run(_run_one(args.scenario, url))
    elif args.all:
        asyncio.run(_run_all(args.stagger, url))
    elif args.mode == "continuous":
        asyncio.run(_run_continuous(args.interval, url))


if __name__ == "__main__":
    main()
