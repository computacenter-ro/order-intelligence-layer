"""Dev harness: fire scenario(s), then dump the emitted logs to a JSON file.

This is the "see the logs" tool. It is NOT part of the runtime pipeline — it
exists so you can eyeball exactly what the mock services emitted for a scenario.

Flow:
  1. record the collector's current log count (so we only capture NEW logs);
  2. publish the scenario baton(s) via the injector;
  3. poll the collector until the log count stops growing (chain drained);
  4. write the captured logs to a JSON file (grouped by flow when possible).

Prerequisites (all via docker compose): RabbitMQ + the collector + the mock
services must be running. Typical local setup::

    docker compose up -d rabbitmq redis
    uvicorn mock_es.app:app --port 9200          # the collector
    python -m services.run_all                   # the mock services
    python -m scripts.capture_flow --scenario scenario_number --out flowX.json

"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from injector.inject import inject_scenario
from shared.scenarios import SCENARIOS, all_scenarios

ES_URL = os.getenv("ES_URL", "http://localhost:9200").rstrip("/")


async def _all_logs(client: httpx.AsyncClient) -> list[dict]:
    """Every log currently in the collector (ascending by timestamp)."""
    resp = await client.get(f"{ES_URL}/logs")
    resp.raise_for_status()
    return resp.json()


async def _wait_until_settled(
    client: httpx.AsyncClient, *, baseline: int, quiet_for: float, timeout: float
) -> list[dict]:
    """Poll until no new logs arrive for ``quiet_for`` seconds (or ``timeout``).

    Returns the logs added since ``baseline``.
    """
    last_count = baseline
    quiet_elapsed = 0.0
    total_elapsed = 0.0
    step = 0.5

    while total_elapsed < timeout:
        await asyncio.sleep(step)
        total_elapsed += step
        logs = await _all_logs(client)
        if len(logs) > last_count:
            last_count = len(logs)
            quiet_elapsed = 0.0
        else:
            quiet_elapsed += step
            if quiet_elapsed >= quiet_for and last_count > baseline:
                break

    logs = await _all_logs(client)
    return logs[baseline:]


def _group_by_flow(logs: list[dict]) -> dict:
    """Group captured logs by their correlation ids for readable output.

    We can't perfectly stitch here (that's the backend's job), but grouping by
    eventId / orderId / cartHeaderId gives a per-flow-ish view for eyeballing.
    Logs with no id land under "_unmatched".
    """
    def key(log: dict) -> str:
        return log.get("eventId") or log.get("orderId") or log.get("cartHeaderId") or "_unmatched"

    grouped: dict[str, list[dict]] = {}
    for log in logs:
        grouped.setdefault(key(log), []).append(log)
    return grouped


async def _fire(scenario_ids: list[int], stagger: float) -> None:
    for i, sid in enumerate(scenario_ids):
        await inject_scenario(SCENARIOS[sid])
        print(f"[capture] fired scenario {sid} ({SCENARIOS[sid].outcome})", flush=True)
        if i < len(scenario_ids) - 1:
            await asyncio.sleep(stagger)


async def _run(args: argparse.Namespace) -> int:
    scenario_ids = (
        [s.id for s in all_scenarios()] if args.all else [args.scenario]
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            baseline = len(await _all_logs(client))
        except httpx.HTTPError as exc:
            print(f"[capture] ERROR: cannot reach collector at {ES_URL} ({exc})")
            print("[capture] is the collector running? uvicorn mock_es.app:app --port 9200")
            return 1

        print(f"[capture] collector has {baseline} logs; firing {len(scenario_ids)} scenario(s)")
        await _fire(scenario_ids, args.stagger)

        print(f"[capture] waiting for logs to settle (quiet_for={args.quiet}s, timeout={args.timeout}s)...")
        captured = await _wait_until_settled(
            client, baseline=baseline, quiet_for=args.quiet, timeout=args.timeout
        )

    print(f"[capture] captured {len(captured)} new log line(s)")

    out = Path(args.out)
    payload = {
        "scenarios": scenario_ids,
        "count": len(captured),
        "logs": captured,
        "by_flow": _group_by_flow(captured) if args.group else None,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[capture] wrote {out.resolve()}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fire scenario(s) and dump emitted logs to JSON.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--scenario", type=int, metavar="N", help="fire one scenario (1-10)")
    group.add_argument("--all", action="store_true", help="fire all 10 scenarios")
    p.add_argument("--out", default="captured_logs.json", help="output JSON path")
    p.add_argument("--stagger", type=float, default=1.0, help="seconds between flows in --all")
    p.add_argument("--quiet", type=float, default=4.0, help="settle when no new logs for this many seconds")
    p.add_argument("--timeout", type=float, default=60.0, help="hard cap on total wait")
    p.add_argument("--group", action="store_true", help="also include a by-flow grouping in the output")
    args = p.parse_args(argv)
    if args.scenario is not None and args.scenario not in SCENARIOS:
        p.error(f"--scenario must be one of {sorted(SCENARIOS)}")
    return args


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
