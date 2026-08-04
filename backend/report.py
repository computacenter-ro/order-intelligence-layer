"""[5] Core Backend — the twice-daily pipeline report posted to Teams.

Two reports per weekday, both to the ``general`` channel:

* **09:00 local** — "what's waiting for you": state-heavy, led by the backlog.
* **17:00 local** — "what we're leaving behind": delta-heavy, led by the day's
  created/resolved/remaining movement.

**The two windows are different periods on purpose.** 17:00→09:00 is the night
nobody watched; 09:00→17:00 is the worked day. Those two facts read differently and
a single 24h report averages them into neither — which is the whole reason there
are two slots. Do not merge them.

Split like ``journeys.py`` / ``incidents.py``: pure decision functions first, a
thin DB/Redis-touching layer over them at the bottom. Everything above
"--- assembly ---" takes ``now`` as a parameter and never reads a clock, so slot
maths, DST and weekend behaviour are all testable without waiting for a Tuesday.
Importing this module performs no I/O.

**Timezone discipline.** ``REPORT_TIMEZONE`` decides only WHEN a slot fires. Every
timestamp stored, compared or persisted is UTC and timezone-aware, per the project
rule — the local zone exists to answer "has 09:00 in Bucharest happened yet?" and
is never written anywhere.

**Watermark discipline** (``teams:digest:last_sent``, ISO UTC in Redis), three
properties, each load-bearing:

1. **A window ends at the slot BOUNDARY, never at ``now``.** A run that starts at
   09:00:07 still reports up to 09:00:00, so consecutive windows are exactly
   contiguous and a report is reproducible from its own declared bounds. Same
   discipline as the AI-service poller's watermark.
2. **A long outage produces ONE report, not a catch-up burst.** The window always
   ends at the *most recent* passed boundary, so two days down means one report
   covering the whole gap — and because the card states its real window in absolute
   dates, that is visible rather than hidden.
3. **The watermark advances only AFTER a successful send.** A failed POST leaves it
   untouched and the next 60s check retries the same window. Advancing first would
   destroy that window's numbers permanently: a duplicate report is cheap, a lost
   window is irreversible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

# --- configuration -----------------------------------------------------------

REPORT_TIMEZONE = os.getenv("REPORT_TIMEZONE", "Europe/Bucharest")
REPORT_SLOTS = os.getenv("REPORT_SLOTS", "09:00,17:00")

#: Redis key holding the ISO-UTC end of the last successfully sent window.
DIGEST_WATERMARK_KEY = "teams:digest:last_sent"

#: How often the loop asks "has a boundary passed?". Cheap: pure arithmetic plus
#: one Redis read, and only a crossed boundary touches the database.
REPORT_CHECK_INTERVAL = int(os.getenv("REPORT_CHECK_INTERVAL", "60"))

#: The event type carried to ``teams.notify``. ONE type for both slots — the slot
#: travels in ``data["slot"]`` — because routing is per channel, not per variant,
#: and two event types would mean two identical rows in ``channel_for``.
REPORT_EVENT = "report.daily"

#: Bucket for alerts with no department (every ``source="fallback"`` alert).
NO_DEPARTMENT_KEY = "unassigned"

#: Monday=0 … Friday=4. Weekend days get no slots at all: nobody reads a Saturday
#: report, and Friday 17:00 → Monday 09:00 is one honest ~64h handover window.
_WEEKDAYS = frozenset({0, 1, 2, 3, 4})

#: How far back the boundary search will look for a weekday. The longest real gap
#: is a Monday morning reaching back to Friday (3 days); the generous margin covers
#: a public-holiday bridge, and the cap exists only so a misconfigured slot list
#: cannot loop forever.
_MAX_LOOKBACK_DAYS = 8


# --- slots + boundaries (pure) -----------------------------------------------


def parse_slots(raw: str = REPORT_SLOTS) -> list[time]:
    """Parse ``"09:00,17:00"`` into sorted ``time`` objects.

    Sorted because the boundary search walks a day's slots newest-first and would
    otherwise depend on how the operator happened to order the env var. Blanks are
    tolerated (a trailing comma in a compose field); a malformed entry raises
    rather than being skipped — a typo'd slot means a report silently never fires
    at that hour, which is exactly the kind of failure nobody notices.
    """
    slots: list[time] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        hh, _, mm = piece.partition(":")
        slots.append(time(hour=int(hh), minute=int(mm or 0)))
    if not slots:
        raise ValueError(f"REPORT_SLOTS defines no slots: {raw!r}")
    return sorted(set(slots))


def _tz(name: str = REPORT_TIMEZONE) -> ZoneInfo:
    return ZoneInfo(name)


def _boundaries_on(day: datetime, slots: Sequence[time]) -> list[datetime]:
    """The slot boundaries for one local day, ascending, as aware local datetimes.

    Built by attaching the zone to a naive local wall time, which is what makes the
    schedule DST-correct: 09:00 Bucharest is 07:00Z in winter and 06:00Z in summer,
    and a fixed-UTC schedule would be an hour off for half the year.
    """
    return [
        datetime.combine(day.date(), slot, tzinfo=day.tzinfo) for slot in slots
    ]


def latest_boundary(
    now: datetime,
    *,
    slots: Sequence[time] | None = None,
    tz: ZoneInfo | None = None,
) -> tuple[datetime, time] | None:
    """The most recent slot boundary at or before ``now``, as ``(utc, slot)``.

    ``now`` must be timezone-aware. Walks backwards from ``now``'s local day,
    skipping weekends, and returns the first boundary that has already passed —
    so a Saturday check finds Friday 17:00, and a Monday 08:30 check finds Friday
    17:00 too.

    ``None`` only if no weekday boundary exists within the lookback window, which
    a real calendar cannot produce.
    """
    zone = tz or _tz()
    slots = list(slots or parse_slots())
    local = now.astimezone(zone)
    for offset in range(_MAX_LOOKBACK_DAYS):
        day = local - timedelta(days=offset)
        if day.weekday() not in _WEEKDAYS:
            continue
        for boundary in reversed(_boundaries_on(day, slots)):
            if boundary <= local:
                # Compare in local time, return in UTC: everything downstream —
                # the watermark, the SQL bounds — is UTC-only.
                return boundary.astimezone(timezone.utc), boundary.timetz().replace(
                    tzinfo=None
                )
    return None


def previous_boundary(
    boundary_utc: datetime,
    *,
    slots: Sequence[time] | None = None,
    tz: ZoneInfo | None = None,
) -> datetime:
    """The weekday slot boundary immediately BEFORE ``boundary_utc`` (UTC).

    Used only on a cold start (no watermark), so the first report covers one slot
    period instead of all history. Naturally yields Friday 17:00 for a Monday 09:00
    boundary — the same ~64h weekend window a running process would have produced.
    """
    zone = tz or _tz()
    slots = list(slots or parse_slots())
    local = boundary_utc.astimezone(zone)
    for offset in range(_MAX_LOOKBACK_DAYS):
        day = local - timedelta(days=offset)
        if day.weekday() not in _WEEKDAYS:
            continue
        for candidate in reversed(_boundaries_on(day, slots)):
            if candidate < local:
                return candidate.astimezone(timezone.utc)
    # Unreachable on a real calendar; a full slot period back is the safe answer.
    return boundary_utc - timedelta(days=1)


def next_boundary(
    after: datetime,
    *,
    slots: Sequence[time] | None = None,
    tz: ZoneInfo | None = None,
) -> datetime | None:
    """The first weekday slot boundary strictly AFTER ``after`` (UTC).

    Used only for wording: it is what lets the 17:00 card know whether the next
    report is tomorrow morning or Monday. Derived, never hardcoded to "Friday" —
    a public-holiday change to the slot config, or a fifth slot, moves it
    automatically.
    """
    zone = tz or _tz()
    slots = list(slots or parse_slots())
    local = after.astimezone(zone)
    for offset in range(_MAX_LOOKBACK_DAYS):
        day = local + timedelta(days=offset)
        if day.weekday() not in _WEEKDAYS:
            continue
        for candidate in _boundaries_on(day, slots):
            if candidate > local:
                return candidate.astimezone(timezone.utc)
    return None


@dataclass(frozen=True)
class ReportWindow:
    """A due report: the exact half-open window ``[start, end)`` and its slot.

    ``start``/``end`` are UTC and aware. ``slot`` is the local wall-clock time of
    the ending boundary and selects the card variant — a window that ends at the
    09:00 boundary is a morning report even if it started two days earlier.
    """

    start: datetime
    end: datetime
    slot: time

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def due_window(
    now: datetime,
    last_sent: datetime | None,
    *,
    slots: Sequence[time] | None = None,
    tz: ZoneInfo | None = None,
) -> ReportWindow | None:
    """The window to report now, or ``None`` if nothing is due.

    ``None`` in three cases, all normal: no boundary has passed yet, the most
    recent boundary was already reported (``last_sent >= boundary``), or the clock
    is inside a weekend with Friday's 17:00 already sent.

    A missed boundary is NOT skipped: if the process was down over Friday 17:00,
    the Saturday check still sends that report, late, for its true window. That is
    the "one report, not a burst" rule — one window ending at the most recent
    passed boundary, whatever happened in between.
    """
    latest = latest_boundary(now, slots=slots, tz=tz)
    if latest is None:
        return None
    boundary, slot = latest
    if last_sent is not None and last_sent >= boundary:
        return None
    start = (
        last_sent
        if last_sent is not None
        else previous_boundary(boundary, slots=slots, tz=tz)
    )
    if start >= boundary:  # defensive: a watermark from the future reports nothing
        return None
    return ReportWindow(start=start, end=boundary, slot=slot)


# --- window wording (pure) ---------------------------------------------------
#
# Every phrase below is DERIVED from the window, never hardcoded to a slot.
# Friday's 17:00 card is a weekend handover, not an overnight one; Monday's 09:00
# window is ~64h, not "overnight"; and after an outage a window can be any length.
# A card that says "yesterday" about a 64h window is simply wrong, and wrong on the
# one surface that cannot be edited after the fact.

_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def format_window(window: ReportWindow, *, tz: ZoneInfo | None = None) -> str:
    """The window as **absolute local dates**, e.g.
    ``"Mon 3 Aug 17:00 → Tue 4 Aug 09:00 (16.0h)"``.

    Always present on the card, and always the authoritative description: a
    relative phrase alone ("overnight") becomes a lie after a weekend, a holiday or
    an outage, and Teams cards cannot be corrected once posted.
    """
    zone = tz or _tz()
    start = window.start.astimezone(zone)
    end = window.end.astimezone(zone)
    fmt = "%a %-d %b %H:%M"
    return f"{start.strftime(fmt)} → {end.strftime(fmt)} ({window.hours:.0f}h)"


def describe_span(window: ReportWindow, *, tz: ZoneInfo | None = None) -> str:
    """A short relative phrase for the window, derived from its actual shape.

    Chosen from the span and the local calendar, so it degrades honestly:
    ``"overnight"`` only for a genuine single night, ``"over the weekend"`` when the
    window really does cross one, and a plain hour count when it is neither (an
    outage catch-up). Never the only description of the window — it sits beside
    :func:`format_window`.
    """
    zone = tz or _tz()
    start = window.start.astimezone(zone)
    end = window.end.astimezone(zone)
    days = (end.date() - start.date()).days
    if days == 0:
        # The only same-day case with the default slots is the worked day itself
        # (09:00→17:00). Guarded on length so a shorter window — extra slots, or a
        # cold start mid-morning — degrades to a plain hour count instead of
        # claiming a whole working day.
        return "during the working day" if window.hours >= 6 else f"over the past {window.hours:.0f} hours"
    if days == 1 and window.hours <= 20:
        return "overnight"
    # Crossing Saturday or Sunday is what makes it a weekend handover, regardless
    # of length — a Friday-evening-to-Monday-morning window is the common case.
    crosses_weekend = any(
        (start.date() + timedelta(days=offset)).weekday() not in _WEEKDAYS
        for offset in range(days + 1)
    )
    if crosses_weekend:
        return "over the weekend"
    return f"over the past {window.hours:.0f} hours"


def handover_note(
    window: ReportWindow,
    *,
    slots: Sequence[time] | None = None,
    tz: ZoneInfo | None = None,
) -> str | None:
    """"Next report <when>" when the following one is not within a day, else ``None``.

    This is what makes Friday's 17:00 card an honest weekend handover: "what we're
    leaving behind" means something different when nobody looks again for 64 hours.
    Computed from :func:`next_boundary`, so it appears for a holiday bridge too and
    never says "Friday" on the strength of the weekday alone.
    """
    zone = tz or _tz()
    nxt = next_boundary(window.end, slots=slots, tz=tz)
    if nxt is None:
        return None
    gap_hours = (nxt - window.end).total_seconds() / 3600.0
    if gap_hours <= 24:
        return None
    local = nxt.astimezone(zone)
    return f"Next report {_DAY_NAMES[local.weekday()]} {local.strftime('%H:%M')}."


def delta_period(window: ReportWindow, *, tz: ZoneInfo | None = None) -> str:
    """Label for the created/resolved counts: ``"today"`` only when true.

    The 17:00 card wants to say "created today"; that is right for a normal
    09:00→17:00 window and wrong for anything longer, where it would attribute two
    days of alerts to today.
    """
    zone = tz or _tz()
    start = window.start.astimezone(zone)
    end = window.end.astimezone(zone)
    if start.date() == end.date():
        return "today"
    if (end.date() - start.date()).days == 1 and window.hours <= 20:
        return "since yesterday evening"
    return "in this window"


# --- payload fold (pure) -----------------------------------------------------


@dataclass(frozen=True)
class DepartmentRow:
    department: str
    unresolved: int
    urgent: int


@dataclass(frozen=True)
class ReportPayload:
    """Everything the card needs, with no SQL and no clock left in it.

    Deliberately a domain object rather than an Adaptive Card:
    ``teams.build_report_card`` renders it, so what the report *says* is testable
    without asserting on JSON shaped for one transport.
    """

    slot: time
    window_start: datetime
    window_end: datetime
    window_label: str
    span_label: str
    delta_label: str
    as_of_label: str
    #: "Next report Monday 09:00." when the following report is >24h away, else None.
    handover: str | None = None
    departments: tuple[DepartmentRow, ...] = ()
    total_unresolved: int = 0
    total_urgent: int = 0
    created: int = 0
    resolved: int = 0
    #: Slot identity as a plain string, so the card builder and the tests don't
    #: reimplement "is this the morning one?".
    is_morning: bool = True
    extras: dict = field(default_factory=dict)


def fold_departments(rows: Iterable[tuple]) -> tuple[tuple[DepartmentRow, ...], int, int]:
    """Fold ``(department, unresolved, urgent)`` rows into sorted rows + totals.

    NULL becomes :data:`NO_DEPARTMENT_KEY` rather than being dropped, so the table
    always sums back to the printed total — and specifically so that the
    ``source="fallback"`` alerts (no department, no severity) still appear. Those
    are the alerts nobody triaged because the LLM was down; omitting them would
    make the report quietest exactly when the pipeline was least healthy.

    Sorted by unresolved count descending, then name, so the busiest department
    leads and the order is stable between reports.
    """
    folded: dict[str, list[int]] = {}
    for department, unresolved, urgent in rows:
        key = NO_DEPARTMENT_KEY if department is None else str(department)
        bucket = folded.setdefault(key, [0, 0])
        bucket[0] += int(unresolved or 0)
        bucket[1] += int(urgent or 0)
    departments = tuple(
        DepartmentRow(department=name, unresolved=counts[0], urgent=counts[1])
        for name, counts in sorted(folded.items(), key=lambda kv: (-kv[1][0], kv[0]))
    )
    return (
        departments,
        sum(row.unresolved for row in departments),
        sum(row.urgent for row in departments),
    )


def build_payload(
    window: ReportWindow,
    *,
    department_rows: Iterable[tuple],
    created: int,
    resolved: int,
    tz: ZoneInfo | None = None,
    morning_slot: time | None = None,
    slots: Sequence[time] | None = None,
) -> ReportPayload:
    """Fold the executed query rows into a :class:`ReportPayload`.

    ``morning_slot`` defaults to the FIRST configured slot, so which variant a slot
    renders follows ``REPORT_SLOTS`` instead of a hardcoded ``09:00`` — reconfigure
    the slots and the earlier one stays the state-heavy morning report.
    """
    zone = tz or _tz()
    if morning_slot is None:
        morning_slot = parse_slots()[0]
    departments, total_unresolved, total_urgent = fold_departments(department_rows)
    return ReportPayload(
        slot=window.slot,
        window_start=window.start,
        window_end=window.end,
        window_label=format_window(window, tz=zone),
        span_label=describe_span(window, tz=zone),
        delta_label=delta_period(window, tz=zone),
        handover=handover_note(window, slots=slots, tz=zone),
        # The boundary, not "now": the card's numbers are as of the window's end,
        # and a run that started seven seconds late must not claim 09:00:07.
        as_of_label=window.end.astimezone(zone).strftime("%a %-d %b %H:%M %Z"),
        departments=departments,
        total_unresolved=total_unresolved,
        total_urgent=total_urgent,
        created=created,
        resolved=resolved,
        is_morning=window.slot == morning_slot,
    )


def report_event(payload: ReportPayload) -> dict:
    """The ``{"type","data"}`` envelope handed to ``teams.notify``.

    Same envelope shape as every other event so ``channel_for`` / ``notify`` treat
    it uniformly, but note this one is NOT produced by the consumers and never
    reaches ``backend/main.py``'s fan-out: the dashboard has no use for a report,
    so it is not a WebSocket event and ``report.daily`` is deliberately absent from
    ``backend/ws.py``'s constants.
    """
    return {"type": REPORT_EVENT, "data": {"payload": payload}}


# --- assembly (touches the DB + Redis) ---------------------------------------


async def read_watermark(redis) -> datetime | None:
    """The last successfully reported window end (aware UTC), or ``None``.

    A corrupt/unparseable value is treated as absent — a cold start reports one
    slot period, which is strictly better than crashing the loop forever on a
    value nobody can fix without redis-cli.
    """
    raw = await redis.get(DIGEST_WATERMARK_KEY)
    if raw is None:
        return None
    text = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        print(
            f"[report] ignoring unparseable {DIGEST_WATERMARK_KEY}={text!r} "
            "— treating as a cold start",
            flush=True,
        )
        return None
    # Tolerate a naive value (hand-set, or written by an older build): the key has
    # always held UTC, so attaching UTC is the correct reading rather than assuming
    # the server's local zone.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def write_watermark(redis, when: datetime) -> None:
    """Record a window end. Called ONLY after a send succeeded."""
    await redis.set(DIGEST_WATERMARK_KEY, when.astimezone(timezone.utc).isoformat())


async def collect_payload(session, window: ReportWindow, **kwargs) -> ReportPayload:
    """Run the three report queries and fold them into a payload."""
    from backend.stats import (
        alerts_created_in_window,
        alerts_resolved_in_window,
        unresolved_alerts_by_department,
    )

    department_rows = (await session.execute(unresolved_alerts_by_department())).all()
    created = (
        await session.execute(alerts_created_in_window(window.start, window.end))
    ).scalar() or 0
    resolved = (
        await session.execute(alerts_resolved_in_window(window.start, window.end))
    ).scalar() or 0
    return build_payload(
        window,
        department_rows=department_rows,
        created=int(created),
        resolved=int(resolved),
        **kwargs,
    )


async def send_due_report(
    session,
    redis,
    *,
    now: datetime | None = None,
    notify=None,
    slots: Sequence[time] | None = None,
    tz: ZoneInfo | None = None,
) -> ReportWindow | None:
    """Send a report if a boundary has passed. Returns the window sent, or ``None``.

    The ordering here is the whole point: **notify first, watermark second.**
    ``notify`` swallows its own transport errors (it prints to stdout when a webhook
    is unset), so a raise reaching here means something structural — and in that
    case the watermark stays put and the next check retries the identical window.
    Advancing it first would lose those numbers for good.

    ``now`` is injectable so the behaviour is testable without waiting for 09:00.
    """
    if notify is None:
        # Late import: keeps this module importable (and unit-testable) without
        # pulling in httpx, and lets a test inject a fake sink instead.
        from backend import teams

        notify = teams.notify
    now = now or datetime.now(timezone.utc)
    window = due_window(now, await read_watermark(redis), slots=slots, tz=tz)
    if window is None:
        return None

    payload = await collect_payload(session, window, tz=tz)
    # Always sent, even when every number is zero: "0 new, 0 unresolved" proves the
    # pipeline AND this reporter are alive, while silence is ambiguous — quiet
    # night, or dead scheduler? Same reasoning as the staleness signal on
    # /ai-performance.
    await notify(report_event(payload))
    await write_watermark(redis, window.end)
    print(
        f"[report] sent {window.slot.strftime('%H:%M')} report for "
        f"{window.start.isoformat()} → {window.end.isoformat()}",
        flush=True,
    )
    return window


async def report_loop(
    *,
    interval: int = REPORT_CHECK_INTERVAL,
    session_factory=None,
    redis=None,
) -> None:
    """Check every ``interval`` seconds whether a slot boundary has passed.

    Runs as its own lifespan task rather than inside ``run_consumers``' gather,
    deliberately: that coroutine owns the RabbitMQ connection and is restarted
    whenever the broker blips, which would take the reporter down with it. A broker
    outage is precisely when a report still matters — and the report reads Postgres
    and Redis, never the broker.

    A failure in one cycle is logged and the loop continues, matching
    ``_sweep_stalled_loop``: the watermark is untouched on failure, so the next
    cycle retries the same window rather than skipping it.
    """
    import asyncio

    if session_factory is None:
        # Late imports so importing this module needs neither a DB driver nor a
        # Redis client — the pure layer above is unit-tested with neither.
        from backend.db import SessionLocal

        session_factory = SessionLocal
    owns_redis = redis is None
    if owns_redis:
        from redis.asyncio import from_url as redis_from_url

        redis = redis_from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

    print(
        f"[report] loop started — slots={REPORT_SLOTS} tz={REPORT_TIMEZONE} "
        f"every {interval}s",
        flush=True,
    )
    try:
        while True:
            try:
                async with session_factory() as session:
                    await send_due_report(session, redis)
            except Exception as exc:  # noqa: BLE001 — a blip must not kill the loop
                print(f"[report] ERROR (watermark kept, will retry): {exc}", flush=True)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        print("[report] cancelled — stopping", flush=True)
        raise
    finally:
        if owns_redis:
            try:
                await redis.aclose()
            except Exception:  # noqa: BLE001 — shutdown best effort
                pass
