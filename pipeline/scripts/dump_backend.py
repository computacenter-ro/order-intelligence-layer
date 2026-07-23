"""Dev harness: dump the backend's outputs (alerts + journeys) to a file.

The "see the results without a frontend" tool. It reads the core backend's
read-only REST API ([5], default :8000) — the same data the dashboard would
show — and writes it to JSON (machine-readable) or TXT (human-readable).

Prerequisites: the full stack running (collector, mock services, AI service,
backend), and some scenarios fired (``python -m pipeline.injector.inject --all``).
The LLM may be OFF — in fallback mode alerts have source="fallback" and journey
summaries are templates; everything else is real.

Usage (from the project root)::

    python -m pipeline.scripts.dump_backend --out results.json
    python -m pipeline.scripts.dump_backend --format txt --out results.txt
    python -m pipeline.scripts.dump_backend --status SUCCESS      # filter journeys




Want to see logs without having a frontend? ;)

python -m uvicorn pipeline.mock_es.app:app --port 9200   # 1. collector
python -m pipeline.services.run_all                       # 2. mock services
python -m ai_service.main                                 # 3. AI service (prints FALLBACK)
python -m uvicorn backend.main:app --port 8000            # 4. backend (consumers + API)
python -m pipeline.injector.inject --all                  # 5. fire the 10 scenarios

Step 3 — wait, then dump the results

Give it ~30–60s after firing so the sliding-window poller (25s window) picks everything up and the backend stitches the journeys. Then:

python -m pipeline.scripts.dump_backend --fo   # human-readable
python -m pipeline.scripts.dump_backend --out results.json                  # machine-readable

What you'll get

The dump pulls from the backend's REST API (/alerts, /journeys, /journeys/{id}) and writes:
- An overview — counts of alerts by source, e.
- Every journey — status, outcome, id aliases, time span, the template summary, and its full ordered event timeline.
- Every alert — source/level/service, departplanation.

This is essentially "what the dashboard woul

What to expect in fallback mode: all alerts on/department null), journey summaries arethe deterministic templates. The outcomes are the real test — you should see the 10 scenarios resolve to their
canonical outcomes (3× SUCCESS, plus the 7 fes the whole stitching/correlation coreend-to-end, LLM-independent.

Two notes

- --status TIMED_OUT: to test the timeout path, fire a scenario then kill the mock services mid-flow — after 90s(STALLED_TIMEOUT) that journey should flip tter lets you check.
- The /summarize-journey binding change we just made (127.0.0.1) doesn't affect this — the backend calls the AI service internally, both on localhost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")


async def _get(client: httpx.AsyncClient, path: str, **params) -> object:
    resp = await client.get(f"{BACKEND_URL}{path}", params={k: v for k, v in params.items() if v is not None})
    resp.raise_for_status()
    return resp.json()


async def _collect(status: str | None, department: str | None, source: str | None) -> dict:
    """Pull alerts + journeys (and each journey's detail) from the backend."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        alerts = await _get(client, "/alerts", department=department, source=source)
        journeys = await _get(client, "/journeys", status=status)
        # Fetch full detail (events + summary) per journey.
        details = []
        for j in journeys:
            jid = j.get("journey_id")
            if jid:
                details.append(await _get(client, f"/journeys/{jid}"))
    return {"alerts": alerts, "journeys": details}


def _summarize(data: dict) -> dict:
    """A small counts overview for the top of the dump."""
    alerts = data["alerts"]
    journeys = data["journeys"]
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    for j in journeys:
        by_status[j.get("status", "?")] = by_status.get(j.get("status", "?"), 0) + 1
        oc = j.get("outcome") or "(none)"
        by_outcome[oc] = by_outcome.get(oc, 0) + 1
    by_source: dict[str, int] = {}
    for a in alerts:
        by_source[a.get("source", "?")] = by_source.get(a.get("source", "?"), 0) + 1
    return {
        "alert_count": len(alerts),
        "journey_count": len(journeys),
        "alerts_by_source": by_source,
        "journeys_by_status": by_status,
        "journeys_by_outcome": by_outcome,
    }


def _render_txt(overview: dict, data: dict) -> str:
    lines: list[str] = []
    bar = "=" * 72
    lines += [bar, "BACKEND OUTPUT DUMP", bar, ""]
    lines.append("OVERVIEW")
    for k, v in overview.items():
        lines.append(f"  {k}: {v}")
    lines += ["", bar, f"JOURNEYS ({len(data['journeys'])})", bar]
    for j in data["journeys"]:
        lines.append("")
        lines.append(f"journey_id : {j.get('journey_id')}")
        lines.append(f"  status   : {j.get('status')}   outcome: {j.get('outcome')}")
        lines.append(f"  ids      : event={j.get('event_id')} order={j.get('order_id')} cart={j.get('cart_header_id')}")
        lines.append(f"  span     : {j.get('first_ts')} -> {j.get('last_ts')}")
        lines.append(f"  summary  : {j.get('summary')}")
        events = j.get("events", [])
        lines.append(f"  events ({len(events)}):")
        for e in events:
            raw = e.get("raw", {})
            lines.append(f"    - {raw.get('level','?'):5} {raw.get('app_name','?'):22} {raw.get('message','')}")
    lines += ["", bar, f"ALERTS ({len(data['alerts'])})", bar]
    for a in data["alerts"]:
        lines.append("")
        lines.append(f"  [{a.get('source')}] {a.get('level')} {a.get('app_name')} — dept={a.get('department')} conf={a.get('confidence')}")
        lines.append(f"    message    : {a.get('message')}")
        lines.append(f"    explanation: {a.get('explanation')}")
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            await client.get(f"{BACKEND_URL}/health")
        except httpx.HTTPError as exc:
            print(f"[dump] ERROR: cannot reach backend at {BACKEND_URL} ({exc})")
            print("[dump] is it running?  python -m uvicorn backend.main:app --port 8000")
            return 1

    data = await _collect(args.status, args.department, args.source)
    overview = _summarize(data)

    print("[dump] overview:", json.dumps(overview))
    out = Path(args.out)
    if args.format == "txt":
        out.write_text(_render_txt(overview, data), encoding="utf-8")
    else:
        out.write_text(
            json.dumps({"overview": overview, **data}, indent=2), encoding="utf-8"
        )
    print(f"[dump] wrote {out.resolve()}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Dump backend alerts + journeys to a file.")
    p.add_argument("--out", default="backend_dump.json", help="output path")
    p.add_argument("--format", choices=["json", "txt"], default="json")
    p.add_argument("--status", help="filter journeys by status (SUCCESS/FAILED/TIMED_OUT/IN_PROGRESS)")
    p.add_argument("--department", help="filter alerts by department")
    p.add_argument("--source", help="filter alerts by source (ai/fallback)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
