"""Send a Teams report on demand, without waiting for 09:00 or 17:00.

The scheduler (``backend/report.py::report_loop``) only fires when a slot boundary
has passed and the watermark is behind it, which makes it awkward to look at a real
card while developing. This script builds a window directly and pushes it through
the same ``collect_payload`` → ``report_event`` → ``teams.notify`` path the loop
uses, so what you see is what the loop would send.

**It deliberately does NOT touch ``teams:digest:last_sent``.** Testing must not
consume a production window: if this advanced the watermark, the next real 09:00
report would silently cover a shorter period than it claims. That also means you can
run it as often as you like.

Usage (from the repo root, with the backend's env loaded)::

    # the most recently passed boundary, real window start from the watermark
    python -m backend.scripts.send_test_report

    # force a slot variant — this is how you preview the 17:00 card at 11am
    python -m backend.scripts.send_test_report --slot 17:00

    # force the window start, to check the card states exactly this range
    python -m backend.scripts.send_test_report --since 2026-08-01T14:00:00+00:00

    # render without posting, even with a webhook configured
    python -m backend.scripts.send_test_report --dry-run

With ``TEAMS_WEBHOOK_REPORTS`` unset, ``teams.notify`` prints the card to stdout
anyway — so a plain run is already safe if you have not wired the channel up yet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, time, timezone

from dotenv import load_dotenv

load_dotenv(override=False)  # .env → os.getenv (TEAMS_WEBHOOK_* / DASHBOARD_URL)

from backend import report, teams  # noqa: E402  (after load_dotenv, like main.py)
from backend.db import SessionLocal  # noqa: E402


def _parse_slot(raw: str) -> time:
    hh, mm = raw.split(":", 1)
    return time(int(hh), int(mm))


def _parse_since(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise SystemExit(
            f"--since must carry a timezone offset (got {raw!r}). "
            "Naive datetimes are rejected on purpose: every timestamp in this "
            "system is UTC and aware, and guessing an offset here would silently "
            "shift the window by hours."
        )
    return parsed.astimezone(timezone.utc)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument(
        "--slot",
        help="Slot variant to render, e.g. 09:00 or 17:00. Default: the most "
        "recently passed boundary.",
    )
    ap.add_argument(
        "--since",
        help="ISO-8601 window start WITH offset, e.g. 2026-08-01T14:00:00+00:00. "
        "Default: the real watermark, or the previous boundary on a cold start.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the card instead of POSTing it, even if a webhook is set.",
    )
    args = ap.parse_args()

    tz = report._tz()
    slots = report.parse_slots()
    now = datetime.now(timezone.utc)

    # --slot narrows the SEARCH, it does not override the result. Overriding would
    # render the evening variant over a morning boundary — a card that says "End of
    # day" above a window ending at 09:00, which the real loop can never produce
    # because there the slot always comes FROM the boundary. Restricting the slot
    # list instead finds the most recent boundary of that slot, so the preview stays
    # internally consistent.
    search_slots = [_parse_slot(args.slot)] if args.slot else slots
    latest = report.latest_boundary(now, slots=search_slots, tz=tz)
    if latest is None:
        raise SystemExit(
            "No slot boundary has passed within the lookback window — check "
            f"REPORT_SLOTS ({report.REPORT_SLOTS!r}) and REPORT_TIMEZONE "
            f"({report.REPORT_TIMEZONE!r})."
        )
    # latest_boundary returns (utc_datetime, local_slot_time).
    end, slot = latest

    if args.since:
        start = _parse_since(args.since)
    else:
        # Read-only: the watermark tells us where a real report would start, but we
        # never write it back. Same client construction as ``report_loop``.
        from redis.asyncio import from_url as redis_from_url

        redis = redis_from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        try:
            start = await report.read_watermark(redis)
        finally:
            await redis.aclose()
        if start is None:
            start = report.previous_boundary(end, slots=slots, tz=tz) or end

    if start >= end:
        raise SystemExit(
            f"Empty window: start ({start.isoformat()}) is not before end "
            f"({end.isoformat()}). Pass an earlier --since."
        )

    window = report.ReportWindow(start=start, end=end, slot=slot)
    print(
        f"[test-report] slot={slot.strftime('%H:%M')} "
        f"window={window.start.isoformat()} → {window.end.isoformat()} "
        f"({window.hours:.1f}h)",
        flush=True,
    )

    async with SessionLocal() as session:
        payload = await report.collect_payload(session, window, tz=tz)

    event = report.report_event(payload)

    if args.dry_run:
        print(json.dumps(teams.build_report_card(payload), indent=2, ensure_ascii=False))
        print(f"[test-report] dry run — nothing posted (channel would be "
              f"{teams.channel_for(event)!r})", flush=True)
        return

    await teams.notify(event)
    print("[test-report] sent — watermark deliberately NOT advanced", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
