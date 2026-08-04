"""Tests for the twice-daily Teams report (backend/report.py + the card builder).

No database, no Redis, no clock: every pure function takes ``now`` as a parameter,
so slot maths, DST, weekends and outages are all asserted directly instead of
being waited for. The DB/Redis layer is driven with a fake session and a fake Redis.

The scheduling rules under test are the ones that are cheap to get subtly wrong and
expensive to notice:

* a window ends at the slot BOUNDARY, not at ``now`` — so consecutive windows tile
  the timeline exactly once and a report is reproducible from its own bounds;
* a long outage yields ONE report, not a catch-up burst;
* the watermark advances only after a successful send, so a failed POST retries the
  same window rather than losing its numbers forever.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend import report, teams

TZ = ZoneInfo("Europe/Bucharest")
SLOTS = [time(9, 0), time(17, 0)]

# 2026: Mon 3 Aug … Fri 7 Aug, Sat 8 / Sun 9 Aug. Fri 31 Jul precedes Mon 3 Aug.
MON, TUE, FRI = 3, 4, 7


def _utc(year, month, day, hour, minute=0, second=0) -> datetime:
    """A LOCAL wall time, returned as the aware UTC instant it denotes."""
    return datetime(
        year, month, day, hour, minute, second, tzinfo=TZ
    ).astimezone(timezone.utc)


def _due(now: datetime, last_sent: datetime | None):
    return report.due_window(now, last_sent, slots=SLOTS, tz=TZ)


# --- slot parsing -------------------------------------------------------------


def test_parse_slots_sorts_and_dedups():
    # Sorted so the newest-first boundary search cannot depend on how the operator
    # happened to order the env var.
    assert report.parse_slots("17:00,09:00") == [time(9, 0), time(17, 0)]
    assert report.parse_slots("09:00, 09:00 ,17:00,") == [time(9, 0), time(17, 0)]


def test_parse_slots_rejects_a_malformed_entry():
    """A typo must fail loudly. Skipping it would mean a report that silently never
    fires at that hour — the failure mode nobody notices."""
    with pytest.raises(ValueError):
        report.parse_slots("09:00,notatime")
    with pytest.raises(ValueError):
        report.parse_slots(",")


def test_the_default_slots_are_the_two_documented_ones():
    assert report.parse_slots(report.REPORT_SLOTS) == SLOTS


# --- boundaries + the due window ---------------------------------------------


def test_each_slot_fires_exactly_once():
    """Both slots produce a report, and only one each."""
    # 09:00 fires; its window runs back to the previous evening.
    morning = _due(_utc(2026, 8, TUE, 9, 0, 7), _utc(2026, 8, MON, 17, 0))
    assert morning is not None
    assert morning.slot == time(9, 0)
    assert morning.end == _utc(2026, 8, TUE, 9, 0)

    # Having sent it, a later check in the same period fires nothing...
    assert _due(_utc(2026, 8, TUE, 9, 30), morning.end) is None
    assert _due(_utc(2026, 8, TUE, 16, 59), morning.end) is None

    # ...until 17:00, which fires once and covers the worked day.
    evening = _due(_utc(2026, 8, TUE, 17, 0, 3), morning.end)
    assert evening is not None
    assert evening.slot == time(17, 0)
    assert evening.start == morning.end
    assert evening.end == _utc(2026, 8, TUE, 17, 0)
    assert _due(_utc(2026, 8, TUE, 17, 45), evening.end) is None


def test_the_window_ends_at_the_boundary_not_at_now():
    """A run that starts late still reports up to the boundary.

    This is what makes consecutive windows exactly contiguous: the next window
    starts where this one ended, so no second of the timeline is double-counted or
    missed. Same discipline as the AI-service poller's watermark.
    """
    for late_by in (1, 7, 59, 600):
        window = _due(
            _utc(2026, 8, TUE, 9, 0, 0) + timedelta(seconds=late_by),
            _utc(2026, 8, MON, 17, 0),
        )
        assert window.end == _utc(2026, 8, TUE, 9, 0), late_by


def test_consecutive_windows_are_contiguous_and_do_not_overlap():
    """Walk a week minute-by-boundary and assert the windows tile the timeline."""
    last = _utc(2026, 8, MON, 9, 0)
    ends = []
    for day in range(MON, FRI + 1):
        for hour in (9, 17):
            window = _due(_utc(2026, 8, day, hour, 0, 5), last)
            if window is None:
                continue
            assert window.start == last, "a gap or an overlap between windows"
            ends.append(window.end)
            last = window.end
    # Mon 17:00 through Fri 17:00 = 9 boundaries after the seeded Mon 09:00.
    assert len(ends) == 9
    assert ends[0] == _utc(2026, 8, MON, 17, 0)
    assert ends[-1] == _utc(2026, 8, FRI, 17, 0)


def test_nothing_is_due_before_the_first_boundary_of_the_day():
    """08:30 has not crossed 09:00, so the last boundary is yesterday's 17:00 —
    already reported."""
    assert _due(_utc(2026, 8, TUE, 8, 30), _utc(2026, 8, MON, 17, 0)) is None


# --- DST ----------------------------------------------------------------------
#
# Europe/Bucharest is EET (+2) in winter and EEST (+3) in summer, switching on the
# last Sunday of March and October. A fixed-UTC schedule would fire an hour off for
# half the year, so the boundary is built from local wall time and converted.


@pytest.mark.parametrize(
    "date_parts,expected_utc_hour,expected_offset",
    [
        ((2026, 3, 27), 7, 2),   # Friday before the spring change: EET, 09:00 = 07:00Z
        ((2026, 3, 30), 6, 3),   # Monday after it: EEST, 09:00 = 06:00Z
        ((2026, 10, 23), 6, 3),  # Friday before the autumn change: still EEST
        ((2026, 10, 26), 7, 2),  # Monday after it: back to EET
    ],
)
def test_the_nine_am_boundary_tracks_dst(date_parts, expected_utc_hour, expected_offset):
    year, month, day = date_parts
    now = _utc(year, month, day, 9, 30)
    boundary, slot = report.latest_boundary(now, slots=SLOTS, tz=TZ)
    assert slot == time(9, 0)
    assert boundary.hour == expected_utc_hour
    assert boundary.tzinfo == timezone.utc
    # And the instant really is 09:00 local on that date.
    local = boundary.astimezone(TZ)
    assert (local.hour, local.minute) == (9, 0)
    assert local.utcoffset() == timedelta(hours=expected_offset)


def test_the_window_across_the_spring_transition_is_an_hour_short():
    """Sun 29 Mar 2026 skips 03:00→04:00 local. The Friday-to-Monday window is
    therefore 63 wall-clock hours, not 64 — and the card prints the real number
    because it is computed from the UTC instants, not from the calendar."""
    window = _due(_utc(2026, 3, 30, 9, 0, 1), _utc(2026, 3, 27, 17, 0))
    assert window.hours == pytest.approx(63.0)


# --- weekends -----------------------------------------------------------------


@pytest.mark.parametrize("day", [8, 9])  # Sat 8, Sun 9 Aug 2026
def test_no_report_fires_on_a_weekend(day):
    """Friday 17:00 already sent ⇒ the weekend is silent at every hour."""
    friday_evening = _utc(2026, 8, FRI, 17, 0)
    for hour in (0, 9, 12, 17, 23):
        assert _due(_utc(2026, 8, day, hour, 0), friday_evening) is None, hour


def test_friday_evening_to_monday_morning_is_one_long_window():
    """One ~64h handover window, not three days of missed reports."""
    window = _due(_utc(2026, 8, MON, 9, 0, 2), _utc(2026, 7, 31, 17, 0))
    assert window.start == _utc(2026, 7, 31, 17, 0)
    assert window.end == _utc(2026, 8, MON, 9, 0)
    assert window.hours == pytest.approx(64.0)
    assert window.slot == time(9, 0)


def test_a_report_missed_over_the_weekend_is_still_sent_late():
    """The process was down at Friday 17:00. Saturday's check sends THAT report,
    for its true window — a late send is not a Saturday report, and skipping it
    would silently drop Friday afternoon's numbers."""
    window = _due(_utc(2026, 8, 8, 10, 0), _utc(2026, 8, FRI, 9, 0))
    assert window.end == _utc(2026, 8, FRI, 17, 0)
    assert window.slot == time(17, 0)


# --- outages ------------------------------------------------------------------


def test_a_two_day_outage_produces_exactly_one_report():
    """Down from Tuesday evening to Friday morning: one report covering the gap,
    not one per missed boundary. The card states its real window, so the length is
    visible rather than hidden."""
    last_sent = _utc(2026, 8, TUE, 17, 0)
    now = _utc(2026, 8, FRI, 9, 0, 30)
    window = _due(now, last_sent)
    assert window.start == last_sent
    assert window.end == _utc(2026, 8, FRI, 9, 0)
    assert window.hours == pytest.approx(64.0)
    # And having sent it, the burst does not follow: nothing more is due until the
    # next boundary.
    assert _due(now, window.end) is None
    assert _due(_utc(2026, 8, FRI, 16, 0), window.end) is None


def test_cold_start_reports_one_slot_period_not_all_history():
    """No watermark ⇒ the window starts at the PREVIOUS boundary."""
    window = _due(_utc(2026, 8, TUE, 9, 5), None)
    assert window.start == _utc(2026, 8, MON, 17, 0)
    assert window.hours == pytest.approx(16.0)


def test_cold_start_on_a_monday_reaches_back_to_friday():
    window = _due(_utc(2026, 8, MON, 9, 5), None)
    assert window.start == _utc(2026, 7, 31, 17, 0)
    assert window.hours == pytest.approx(64.0)


def test_a_watermark_from_the_future_reports_nothing():
    """Defensive: a hand-edited or clock-skewed watermark must not produce a
    backwards window (which would make every SQL bound nonsense)."""
    assert _due(_utc(2026, 8, TUE, 9, 5), _utc(2026, 8, FRI, 17, 0)) is None


# --- wording (derived from the window, never from the slot) -------------------


def test_the_window_is_stated_in_absolute_local_dates():
    window = _due(_utc(2026, 8, TUE, 9, 0, 7), _utc(2026, 8, MON, 17, 0))
    label = report.format_window(window, tz=TZ)
    assert label == "Mon 3 Aug 17:00 → Tue 4 Aug 09:00 (16h)"


def test_span_wording_is_derived_not_hardcoded_per_slot():
    """The same 09:00 slot describes itself differently depending on its window —
    "overnight" on a Tuesday, "over the weekend" on a Monday. Hardcoding
    "overnight" for the morning report would make every Monday card a lie."""
    tuesday = _due(_utc(2026, 8, TUE, 9, 0, 1), _utc(2026, 8, MON, 17, 0))
    monday = _due(_utc(2026, 8, MON, 9, 0, 1), _utc(2026, 7, 31, 17, 0))
    assert report.describe_span(tuesday, tz=TZ) == "overnight"
    assert report.describe_span(monday, tz=TZ) == "over the weekend"


def test_a_long_outage_window_is_not_described_as_overnight():
    """A 64h mid-week gap is neither "overnight" nor "over the weekend" — it gets a
    plain hour count, which is the only honest description of an outage window."""
    # Tue 17:00 → Fri 09:00: 64h, but Wed and Thu are weekdays, so no weekend.
    midweek = _due(_utc(2026, 8, FRI, 9, 0, 1), _utc(2026, 8, TUE, 17, 0))
    assert midweek.hours == pytest.approx(64.0)
    assert report.describe_span(midweek, tz=TZ) == "over the past 64 hours"

    # An outage that really does span a weekend says so, regardless of length:
    # Thu 6 Aug 17:00 → Mon 10 Aug 09:00 crosses Sat 8 / Sun 9.
    over_weekend = _due(_utc(2026, 8, 10, 9, 0, 1), _utc(2026, 8, 6, 17, 0))
    assert over_weekend.hours == pytest.approx(88.0)
    assert report.describe_span(over_weekend, tz=TZ) == "over the weekend"


def test_delta_label_says_today_only_when_the_window_is_one_day():
    same_day = _due(_utc(2026, 8, TUE, 17, 0, 1), _utc(2026, 8, TUE, 9, 0))
    assert report.delta_period(same_day, tz=TZ) == "today"
    spanning = _due(_utc(2026, 8, FRI, 17, 0, 1), _utc(2026, 8, TUE, 17, 0))
    assert report.delta_period(spanning, tz=TZ) == "in this window"


def test_the_handover_note_appears_only_when_the_next_report_is_far_off():
    """Friday evening is a weekend handover; Tuesday evening is not. Derived from
    the next boundary, so a holiday bridge gets it too and no weekday is named in
    the code."""
    friday = _due(_utc(2026, 7, 31, 17, 0, 1), _utc(2026, 7, 31, 9, 0))
    tuesday = _due(_utc(2026, 8, TUE, 17, 0, 1), _utc(2026, 8, TUE, 9, 0))
    assert report.handover_note(friday, slots=SLOTS, tz=TZ) == "Next report Monday 09:00."
    assert report.handover_note(tuesday, slots=SLOTS, tz=TZ) is None


# --- the department fold ------------------------------------------------------


def test_a_fallback_alert_lands_in_an_explicit_null_bucket():
    """A ``source="fallback"`` alert has NO department and NO severity, so a
    silently dropped NULL group would hide exactly the alerts nobody triaged —
    because the LLM was down. The breakdown must still sum to the printed total."""
    rows = [("backend", 4, 1), (None, 3, 0), ("networking", 2, 0)]
    departments, total, urgent = report.fold_departments(rows)
    keys = [row.department for row in departments]
    assert report.NO_DEPARTMENT_KEY in keys
    assert total == 9 == sum(row.unresolved for row in departments)
    assert urgent == 1


def test_the_department_table_sums_to_the_total_on_the_payload_too():
    window = _due(_utc(2026, 8, TUE, 9, 0, 1), _utc(2026, 8, MON, 17, 0))
    payload = report.build_payload(
        window,
        department_rows=[("backend", 4, 1), (None, 3, 0)],
        created=5,
        resolved=2,
        tz=TZ,
        slots=SLOTS,
        morning_slot=SLOTS[0],
    )
    assert payload.total_unresolved == sum(r.unresolved for r in payload.departments) == 7
    assert payload.total_urgent == 1


def test_departments_are_ordered_busiest_first():
    rows = [("a", 1, 0), ("b", 9, 0), ("c", 5, 0)]
    departments, _, _ = report.fold_departments(rows)
    assert [row.department for row in departments] == ["b", "c", "a"]


def test_the_fold_handles_an_empty_backlog():
    departments, total, urgent = report.fold_departments([])
    assert departments == () and total == 0 and urgent == 0


# --- the arithmetic identity --------------------------------------------------


def test_resolved_in_window_ignores_when_the_alert_was_created():
    """The definition the card's arithmetic depends on.

    ``alerts_resolved_in_window`` must filter on ``resolved_at`` and NOTHING else.
    The tempting misreading — "resolved among those created in the window" — drops
    the commonest case (yesterday's alert triaged this morning) and the printed
    numbers then visibly fail to add up.
    """
    from sqlalchemy.dialects import postgresql

    from backend import stats

    since = _utc(2026, 8, MON, 17, 0)
    until = _utc(2026, 8, TUE, 9, 0)
    sql = str(
        stats.alerts_resolved_in_window(since, until).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "alerts.resolved_at" in sql
    assert "alerts.emitted_at" not in sql, "must not scope by creation time"

    created_sql = str(
        stats.alerts_created_in_window(since, until).compile(
            dialect=postgresql.dialect()
        )
    )
    assert "alerts.emitted_at" in created_sql
    assert "alerts.resolved_at" not in created_sql


def test_the_open_alert_arithmetic_ties():
    """open_at_end == open_at_start + created_in_window - resolved_in_window.

    Simulated over a hand-built alert set using the same half-open predicates the
    SQL uses, including the two cases that break under the wrong definition of
    "resolved in window": an alert created BEFORE the window and resolved inside it,
    and one created inside and resolved after.
    """
    start = _utc(2026, 8, MON, 17, 0)
    end = _utc(2026, 8, TUE, 9, 0)
    before, inside, after = start - timedelta(hours=5), start + timedelta(hours=2), end + timedelta(hours=1)

    # (emitted_at, resolved_at | None)
    alerts = [
        (before, inside),   # old alert, triaged during the window
        (before, None),     # old alert, still open
        (inside, inside),   # created and resolved inside
        (inside, None),     # created inside, still open
        (inside, after),    # created inside, resolved after the window closed
        (before, before),   # created and resolved before the window
        (after, None),      # created after the window
    ]

    def open_at(instant):
        return sum(
            1
            for emitted, resolved in alerts
            if emitted < instant and (resolved is None or resolved >= instant)
        )

    created = sum(1 for emitted, _ in alerts if start <= emitted < end)
    resolved = sum(
        1 for _, resolved in alerts if resolved is not None and start <= resolved < end
    )

    assert created == 3
    assert resolved == 2  # BOTH the old alert and the same-window one
    assert open_at(end) == open_at(start) + created - resolved


# --- routing ------------------------------------------------------------------


def test_report_routes_to_the_reports_channel():
    event = {"type": "report.daily", "data": {"payload": None}}
    assert teams.channel_for(event) == "reports"
    assert teams.REPORTS == "reports"


def test_one_event_type_serves_both_slots():
    """The slot travels in ``data``, so routing has one row rather than two."""
    assert report.REPORT_EVENT == "report.daily"
    for slot in SLOTS:
        window = report.ReportWindow(
            start=_utc(2026, 8, MON, 17, 0), end=_utc(2026, 8, TUE, 9, 0), slot=slot
        )
        payload = report.build_payload(
            window, department_rows=[], created=0, resolved=0, tz=TZ,
            slots=SLOTS, morning_slot=SLOTS[0],
        )
        event = report.report_event(payload)
        assert event["type"] == "report.daily"
        assert teams.channel_for(event) == teams.REPORTS


def test_alert_routing_is_unchanged():
    alert = {
        "type": "alert.new",
        "data": {"alert_id": "al-1", "source": "ai", "department": "backend"},
    }
    assert teams.channel_for(alert) == "backend"
    assert teams.channel_for({"type": "alert.new", "data": {"source": "fallback"}}) == "fallback"
    assert teams.channel_for({"type": "journey.completed", "data": {}}) is None


def test_report_daily_is_not_a_websocket_event():
    """The report reaches Teams directly from the scheduler, never through
    ``main._fan_out`` — that also feeds the WS hub, and the dashboard has no use for
    a report. So it must not appear among ws.py's event constants."""
    from backend import ws

    emitted = {
        value for name, value in vars(ws).items()
        if name.startswith("EVENT_") and isinstance(value, str)
    }
    assert report.REPORT_EVENT not in emitted


# --- the card -----------------------------------------------------------------


def _payload(slot, *, rows=(), created=0, resolved=0, now=None, last=None):
    window = _due(now, last)
    assert window is not None and window.slot == slot, "fixture picked the wrong slot"
    return report.build_payload(
        window, department_rows=list(rows), created=created, resolved=resolved,
        tz=TZ, slots=SLOTS, morning_slot=SLOTS[0],
    )


def _blocks(card):
    return card["attachments"][0]["content"]["body"]


def _texts(card):
    return [b["text"] for b in _blocks(card) if b["type"] == "TextBlock"]


def _factsets(card):
    return [
        {f["title"]: f["value"] for f in b["facts"]}
        for b in _blocks(card)
        if b["type"] == "FactSet"
    ]


def test_the_morning_card_leads_with_the_backlog(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.example.com")
    payload = _payload(
        time(9, 0), rows=[("backend", 7, 2), (None, 3, 0)], created=9,
        now=_utc(2026, 8, TUE, 9, 0, 7), last=_utc(2026, 8, MON, 17, 0),
    )
    card = teams.build_report_card(payload)
    texts = _texts(card)
    assert texts[0] == "Waiting for you — 10 unresolved"          # title names the backlog
    assert "as of Tue 4 Aug 09:00 EEST" in texts[1]                # as-of + totals
    assert "10 unresolved · 2 urgent" in texts[1]
    assert any("9 new alerts overnight." == t for t in texts)      # created in window
    assert _factsets(card)[-1] == {"backend": "7 · 2 urgent", "unassigned": "3"}


def test_the_evening_card_leads_with_the_delta(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.example.com")
    payload = _payload(
        time(17, 0), rows=[("backend", 7, 2)], created=14, resolved=11,
        now=_utc(2026, 8, TUE, 17, 0, 3), last=_utc(2026, 8, TUE, 9, 0),
    )
    card = teams.build_report_card(payload)
    texts = _texts(card)
    assert texts[0] == "End of day — 14 new, 11 resolved, 7 open"
    delta = _factsets(card)[0]
    assert delta == {"Created today": "14", "Resolved today": "11", "Remaining open": "7"}
    assert any("What remains, by department" == t for t in texts)


def test_both_cards_state_the_window_in_absolute_dates_and_link_to_the_feed(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.example.com/")
    for slot, now, last in (
        (time(9, 0), _utc(2026, 8, TUE, 9, 0, 1), _utc(2026, 8, MON, 17, 0)),
        (time(17, 0), _utc(2026, 8, TUE, 17, 0, 1), _utc(2026, 8, TUE, 9, 0)),
    ):
        card = teams.build_report_card(_payload(slot, now=now, last=last))
        window_lines = [t for t in _texts(card) if t.startswith("Window: ")]
        assert len(window_lines) == 1
        # Absolute dates, never a bare relative phrase.
        assert "Aug" in window_lines[0] and "→" in window_lines[0]
        actions = card["attachments"][0]["content"]["actions"]
        assert len(actions) == 1
        assert actions[0]["title"] == "Open Alert Feed"
        assert actions[0]["url"] == "https://dash.example.com"


def test_an_all_zero_window_still_produces_a_card(monkeypatch):
    """Silence is ambiguous — quiet night, or dead scheduler? "0 unresolved" proves
    the pipeline and the reporter are both alive."""
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    card = teams.build_report_card(
        _payload(time(9, 0), rows=[], created=0, resolved=0,
                 now=_utc(2026, 8, TUE, 9, 0, 1), last=_utc(2026, 8, MON, 17, 0))
    )
    texts = _texts(card)
    assert texts[0] == "Waiting for you — 0 unresolved"
    assert any("Nothing unresolved" in t for t in texts)
    assert "0 new alerts overnight." in texts
    # No DASHBOARD_URL ⇒ no dead button, same rule as the alert card.
    assert "actions" not in card["attachments"][0]["content"]


def test_the_card_is_in_english():
    """The corpus and the plan document are bilingual; the card is not."""
    card = teams.build_report_card(
        _payload(time(9, 0), rows=[("backend", 1, 0)], created=2,
                 now=_utc(2026, 8, TUE, 9, 0, 1), last=_utc(2026, 8, MON, 17, 0))
    )
    blob = " ".join(_texts(card))
    for english in ("Waiting for you", "unresolved", "urgent", "Window", "by department"):
        assert english in blob
    # A few Romanian words from the plan doc that must never reach the card.
    for romanian in ("alerte", "raport", "canal", "cartel"):
        assert romanian not in blob.lower()


def test_the_card_envelope_matches_the_alert_card_shape():
    """Same message/attachment envelope, so the webhook contract is identical."""
    card = teams.build_report_card(
        _payload(time(9, 0), now=_utc(2026, 8, TUE, 9, 0, 1), last=_utc(2026, 8, MON, 17, 0))
    )
    assert card["type"] == "message"
    attachment = card["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"
    assert attachment["content"]["version"] == "1.4"


def test_notify_dispatches_a_report_to_the_report_builder(monkeypatch):
    """`notify` picks the builder by event type — the report must not be run
    through `build_card`, which builds an alert."""
    monkeypatch.setenv("TEAMS_WEBHOOK_REPORTS", "https://hook/reports")
    payload = _payload(
        time(9, 0), rows=[("backend", 2, 0)], created=3,
        now=_utc(2026, 8, TUE, 9, 0, 1), last=_utc(2026, 8, MON, 17, 0),
    )
    event = report.report_event(payload)
    assert teams._card_for(event) == teams.build_report_card(payload)

    posts = []

    class _Client:
        async def post(self, url, json=None):
            posts.append((url, json))

    import asyncio

    asyncio.run(teams.notify(event, client=_Client()))
    assert posts == [("https://hook/reports", teams.build_report_card(payload))]


async def test_an_unset_general_webhook_prints_the_report_to_stdout(monkeypatch, capsys):
    """Reuses notify's never-crash-on-missing-config path."""
    monkeypatch.delenv("TEAMS_WEBHOOK_REPORTS", raising=False)
    payload = _payload(
        time(9, 0), now=_utc(2026, 8, TUE, 9, 0, 1), last=_utc(2026, 8, MON, 17, 0)
    )
    await teams.notify(report_event_or(payload))
    out = capsys.readouterr().out
    assert "[teams:reports]" in out
    assert "Waiting for you" in out


def report_event_or(payload):  # tiny alias, keeps the test above readable
    return report.report_event(payload)


# --- the DB/Redis layer (faked) ----------------------------------------------


class _FakeRedis:
    def __init__(self, value=None):
        self.store = {report.DIGEST_WATERMARK_KEY: value} if value else {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value):
        self.store[key] = value


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows, self._scalar = rows or [], scalar

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _FakeSession:
    """Returns seeded results in execution order: departments, created, resolved."""

    def __init__(self, rows, created, resolved):
        self._queued = [
            _FakeResult(rows=rows),
            _FakeResult(scalar=created),
            _FakeResult(scalar=resolved),
        ]
        self.executed = 0

    async def execute(self, _stmt):
        result = self._queued[self.executed]
        self.executed += 1
        return result


async def test_send_due_report_sends_and_then_advances_the_watermark():
    redis = _FakeRedis(_utc(2026, 8, MON, 17, 0).isoformat())
    session = _FakeSession([("backend", 5, 1)], 8, 3)
    sent = []

    window = await report.send_due_report(
        session, redis, now=_utc(2026, 8, TUE, 9, 0, 4),
        notify=lambda event: _record(sent, event), slots=SLOTS, tz=TZ,
    )

    assert window.end == _utc(2026, 8, TUE, 9, 0)
    assert len(sent) == 1 and sent[0]["type"] == "report.daily"
    assert sent[0]["data"]["payload"].created == 8
    # Watermark advanced to the BOUNDARY, so the next window starts exactly here.
    assert redis.store[report.DIGEST_WATERMARK_KEY] == window.end.isoformat()


async def _record(sink, event):
    sink.append(event)


async def test_nothing_is_sent_when_no_boundary_has_passed():
    redis = _FakeRedis(_utc(2026, 8, TUE, 9, 0).isoformat())
    session = _FakeSession([], 0, 0)
    sent = []
    window = await report.send_due_report(
        session, redis, now=_utc(2026, 8, TUE, 9, 30),
        notify=lambda e: _record(sent, e), slots=SLOTS, tz=TZ,
    )
    assert window is None
    assert sent == []
    assert session.executed == 0, "must not query the DB when nothing is due"


async def test_a_failed_send_leaves_the_watermark_untouched_and_retries():
    """The load-bearing ordering: notify first, watermark second.

    A duplicate report is cheap; a lost window is irreversible — advancing the
    watermark before a failed POST would destroy that window's numbers permanently.
    """
    original = _utc(2026, 8, MON, 17, 0).isoformat()
    redis = _FakeRedis(original)
    attempts = []

    async def failing_notify(event):
        attempts.append(event)
        raise RuntimeError("Teams unreachable")

    with pytest.raises(RuntimeError):
        await report.send_due_report(
            _FakeSession([], 4, 1), redis, now=_utc(2026, 8, TUE, 9, 0, 5),
            notify=failing_notify, slots=SLOTS, tz=TZ,
        )
    assert redis.store[report.DIGEST_WATERMARK_KEY] == original, "watermark moved!"

    # The next check retries the IDENTICAL window.
    sent = []
    window = await report.send_due_report(
        _FakeSession([], 4, 1), redis, now=_utc(2026, 8, TUE, 9, 1, 30),
        notify=lambda e: _record(sent, e), slots=SLOTS, tz=TZ,
    )
    assert window.start == _utc(2026, 8, MON, 17, 0)
    assert window.end == _utc(2026, 8, TUE, 9, 0)
    assert attempts[0]["data"]["payload"].window_start == window.start
    assert len(sent) == 1


async def test_a_cold_start_watermark_is_written_as_iso_utc():
    redis = _FakeRedis()
    sent = []
    window = await report.send_due_report(
        _FakeSession([], 0, 0), redis, now=_utc(2026, 8, TUE, 9, 0, 2),
        notify=lambda e: _record(sent, e), slots=SLOTS, tz=TZ,
    )
    stored = redis.store[report.DIGEST_WATERMARK_KEY]
    assert stored.endswith("+00:00"), "the watermark must be aware UTC, never naive"
    assert datetime.fromisoformat(stored) == window.end


async def test_an_unparseable_watermark_degrades_to_a_cold_start():
    """Better one duplicate report than a loop that dies forever on a value nobody
    can fix without redis-cli."""
    redis = _FakeRedis("not-a-timestamp")
    assert await report.read_watermark(redis) is None


async def test_a_naive_stored_watermark_is_read_as_utc():
    """The key has always held UTC, so attaching UTC is the correct reading — not
    the server's local zone, which would shift the window by 2-3 hours."""
    redis = _FakeRedis("2026-08-03T14:00:00")
    assert await report.read_watermark(redis) == _utc(2026, 8, MON, 17, 0)
