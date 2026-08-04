"""[5] Core Backend — Microsoft Teams notifications (CLAUDE.md [5] "Slack").

Teams-flavoured sibling of the Slack notifier: it consumes the same
``{"type": ..., "data": ...}`` event envelopes that ``backend/ws.py`` broadcasts
and turns the interesting ones into channel notifications.

Pure logic is separated from I/O so it is trivially testable:

* :func:`channel_for` — routing: which channel (if any) an event belongs to.
* :func:`build_card` — a simple, transport-agnostic card ``dict`` (title +
  fields + one "View alert" deep link), easy to adapt between an Incoming Webhook
  and a Power Automate flow.
* :func:`notify` — the only I/O: resolve the channel + its webhook URL and POST
  the card with ``httpx``; if the channel's webhook env var is unset, print the
  card to stdout instead. **Never crash on missing config.**

**Exactly TWO event types route to a channel: ``alert.new`` and ``report.daily``.**
Every other event — ``journey.completed``, ``journey.updated``, every
``incident.*``, anything added later — returns ``None`` from :func:`channel_for` and
notifies nothing. That restraint is what keeps these channels readable:
AI-analyzed alerts go to their department's channel, unprocessed pass-throughs
(``source="fallback"``) go to ``fallback``, the twice-daily report goes to
``reports``, and nothing else arrives at all.

``journey.completed`` used to post to ``general``. It was dropped outright rather
than deferred: at the deployed rate (~2 flows/30s) one card per completed journey
is thousands a day — more traffic than the alerts themselves, and the same
unreadability that broke the ``general`` channel in the first place. The daily
report (stage 3 of ``docs/teams-notifications-plan.md``) covers completions as
aggregate numbers instead. **This is not a gap waiting to be filled** — do not
re-add a per-journey card. The WebSocket still carries ``journey.completed``; only
the Teams sink ignores it.

**A channel name is NOT a department name**, even where the two strings match.
``channel_for`` used to return ``data["department"]`` verbatim, so the ``general``
*department* (business rejections: margin block, missing ``costCenter`` UDF,
disabled JAM account, unmapped product) shared the ``general`` *channel* with
every ``source="fallback"`` pass-through. That made the channel unreadable: "the
pipeline correctly rejected an order" and "the LLM was down so nobody looked at
this" are unrelated facts needing different readers. They are fully separable in
the data — per the ``ProcessedAlert`` contract an AI alert always has a
department and a fallback alert never does — so routing is an explicit
department → channel table (:data:`_DEPARTMENT_CHANNELS`).

The department has since been renamed ``general`` → ``business``, so that table's
business row now maps ``business`` → ``business``. **That identity is not a
reason to collapse the table back into ``return data["department"]``** — see the
comment on :data:`_DEPARTMENT_CHANNELS`.

``backend/main.py``'s lifespan fans every event out to both the WS hub and
:func:`notify`, so this module sees the same stream the dashboard does and simply
declines most of it.
"""

from __future__ import annotations

import json
import os

import httpx

from shared.models import Department

# Channel names — every channel this module can post to. Four share their name
# with a Department value, and so does BUSINESS (the department was renamed
# general → business); FALLBACK deliberately does not. Never re-derive a channel
# from a department without going through the table below.
NETWORKING = "networking"
DEVOPS = "devops"
BACKEND = "backend"
DATABASE = "database"
BUSINESS = "business"
FALLBACK = "fallback"

# The report channel. Reinstated deliberately, and that word is load-bearing: it
# was REMOVED in stage 1 (when ``journey.completed`` stopped notifying) so that no
# code path could reach this channel out of reflex, and a test asserted its
# absence. The twice-daily report (``backend/report.py``) is the one deliberate
# thing it now carries — the test was updated as part of adding it, not worked
# around.
#
# It was called ``GENERAL`` / ``TEAMS_WEBHOOK_GENERAL`` until the report shipped,
# and the rename is the point: that name meant something different three times over
# (every alert without an engineering department — business rejections and fallback
# pass-throughs mixed, which is what made it unreadable — then journey completions
# only, then nothing at all), and a name whose meaning keeps moving is a name nobody
# can trust. ``REPORTS`` says what arrives here and nothing else does: two cards per
# weekday, never a stream. Business rejections belong in BUSINESS and unprocessed
# alerts in FALLBACK.
#
# The Teams channel behind it is ``oil-general-reports``; the webhook URL is what
# binds the two, so the channel can be renamed on the Teams side without touching
# any code.
REPORTS = "reports"

# The explicit department → channel table. Keys are ``Department`` values
# (shared/models.py). A test pins this table to the enum, so adding a department
# fails loudly here instead of silently routing to FALLBACK.
#
# ⚠ Every row is an identity mapping, because ``Department.general`` was renamed
# to ``business`` to match the channel it routes to. Do NOT "simplify"
# :func:`channel_for` back to ``return data["department"]`` on the strength of
# that. The table is what keeps channels and departments SEPARABLE, and they are
# still not the same set: ``fallback`` is a channel with no department (a fallback
# alert has none by contract), and the department roster is not a channel roster.
# Collapsing it re-couples the two so the next channel change has to rename a
# department — which is exactly the mess this table was extracted to end.
_DEPARTMENT_CHANNELS: dict[str, str] = {
    Department.networking.value: NETWORKING,
    Department.devops.value: DEVOPS,
    Department.backend.value: BACKEND,
    Department.database.value: DATABASE,
    Department.business.value: BUSINESS,
}


# --- routing (pure) ----------------------------------------------------------


def channel_for(event: dict) -> str | None:
    """Return the channel an event should notify, or ``None`` to skip it.

    **Exactly two event types return a channel:** ``alert.new`` and
    ``report.daily``. Everything else returns ``None``.

    * ``alert.new``, ``source="ai"`` → :data:`_DEPARTMENT_CHANNELS` for its
      ``department`` (each of the five gets its own channel — including
      ``business``, the business-rejection verdict).
    * ``alert.new``, ``source="fallback"`` → ``fallback`` (unprocessed).
    * ``report.daily`` → ``reports``. ONE branch for both slots: the slot travels
      in ``data`` (``backend/report.py``), because routing is per channel and two
      event types would be two identical rows here.
    * **every other event type → ``None``**: ``journey.completed`` (dropped — one
      card per completed journey is thousands a day; the report covers them in
      aggregate), ``journey.updated`` (per-chunk updates would be spam), every
      ``incident.*`` (a dashboard view, not a notification channel), and anything
      added later, which notifies nothing until someone decides it should.

    Defensive: ``source="ai"`` with no department (or an unrecognized one) cannot
    happen per the ``ProcessedAlert`` contract, but if it does it routes to
    ``fallback`` — semantically it is unprocessed — never to ``business``, which
    would assert "the pipeline correctly rejected this order" about an alert
    nothing classified. It never raises; a lost notification beats a dead loop.
    """
    type_ = event.get("type")
    data = event.get("data") or {}

    if type_ == "alert.new":
        if data.get("source") == "ai":
            return _DEPARTMENT_CHANNELS.get(data.get("department") or "", FALLBACK)
        return FALLBACK
    if type_ == "report.daily":
        return REPORTS
    # No further branches, on purpose. Journey and incident events reach this line
    # and are ignored; see the docstring for why each one is not a notification.
    return None


# --- card (pure) -------------------------------------------------------------


def _alert_link(data: dict) -> str | None:
    """Deep link to THIS ALERT: ``DASHBOARD_URL`` + ``/?alert=<alert_id>``.

    Opens the Alert Feed with the alert's detail drawer already open
    (``dashboard/app/page.tsx`` reads the param and fetches by id).

    It used to link to the journey view instead, which had two problems. The alert
    the card is *about* had no URL at all, so a reader could be sent near it but
    never to it; and ``journey_id`` is **nullable** — an alert not yet stitched to a
    journey has none — so those cards shipped with no button whatsoever. Since
    ``alert_id`` is non-nullable, a card now ALWAYS carries an action whenever
    ``DASHBOARD_URL`` is set. That is the whole point of the change, and a test pins
    it for the ``journey_id is None`` case specifically.

    Deliberately the ONLY action on the card. The drawer offers
    "→ View Full Order Journey" under RELATED, so the journey is one click past the
    landing page; a second button would duplicate existing navigation in the most
    expensive space on the card.

    ``None`` when ``DASHBOARD_URL`` is unset — never a relative or partial URL,
    which Teams would render as a dead button.

    Note ``backend/api.py::_dashboard_link`` still builds ``/journeys/<id>`` links,
    correctly: it links chat citations, where a ``journey`` citation is *about* a
    journey. The two no longer share a target, only the rule that ``order_id`` is
    never a fallback (``/journeys/ORD-8944`` is a guaranteed 404).
    """
    base = os.getenv("DASHBOARD_URL", "").rstrip("/")
    alert_id = data.get("alert_id")
    if not base or not alert_id:
        return None
    return f"{base}/?alert={alert_id}"


def build_card(event: dict) -> dict:
    """Build the exact Teams payload: a ``message`` envelope wrapping an
    Adaptive Card (confirmed working against the Teams workflow with ``curl``).

    Shape::

        {"type": "message",
         "attachments": [{
           "contentType": "application/vnd.microsoft.card.adaptive",
           "content": { <AdaptiveCard 1.4> }
         }]}

    Builds an **alert card**, because :func:`channel_for` resolves a channel for
    ``alert.new`` and nothing else — so that is the only event ``notify`` ever
    passes here. It used to also handle ``journey.completed`` (reading ``outcome``
    as the level and ``summary`` as the body); that was removed with the journey
    routing rather than left annotated, since no planned notification reuses those
    fields — stage 2's interrupts are incident-shaped and stage 3's report is an
    aggregate digest. Fields still absent from a given alert are simply skipped.
    """
    type_ = event.get("type", "")
    data = event.get("data") or {}

    # Title: event type + service, e.g. "alert.new · cc-spt-service".
    service = data.get("app_name")
    title = f"{type_} · {service}" if service else type_

    # Badge.
    badge = "AI-analyzed" if data.get("source") == "ai" else "fallback"

    # Body text: the AI explanation, or the note saying why there isn't one.
    explanation = data.get("explanation") or ""
    text = explanation or "unprocessed — LLM unavailable"

    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": title,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {"type": "TextBlock", "text": badge, "isSubtle": True, "spacing": "None"},
    ]
    if text:
        body.append({"type": "TextBlock", "text": text, "wrap": True})

    # No explanation → show the RAW LOG LINE, in monospace.
    #
    # Without it a fallback card carried the service, the level, and nothing about
    # what actually broke — i.e. it was least informative in exactly the case where
    # the AI explained nothing. `message` is already on the wire (`AlertOut`) and was
    # simply unused. The "unprocessed" note above stays: it explains WHY there is no
    # explanation, which the raw line does not.
    #
    # Only when there is no explanation. Appending the raw line to an AI card would
    # roughly double its length to restate what the explanation already covers —
    # and card length is what made these channels tiring to read.
    if not explanation and data.get("message"):
        body.append(
            {
                "type": "TextBlock",
                "text": str(data["message"]),
                "wrap": True,
                "fontType": "Monospace",
                "size": "Small",
                "spacing": "Small",
            }
        )

    # FactSet: level, severity, department, order/event/cart ids.
    facts: list[dict] = []
    if data.get("level"):
        facts.append({"title": "Level", "value": str(data["level"])})
    if data.get("severity"):
        facts.append({"title": "Severity", "value": str(data["severity"]).capitalize()})
    if data.get("department"):
        facts.append({"title": "Department", "value": str(data["department"])})
    for label, key in (
        ("Order", "order_id"),
        ("Event", "event_id"),
        ("Cart", "cart_header_id"),
    ):
        if data.get(key):
            facts.append({"title": label, "value": str(data[key])})
    if facts:
        body.append({"type": "FactSet", "facts": facts})

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }

    link = _alert_link(data)
    if link:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "View alert", "url": link}
        ]

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }


# --- report card (pure) ------------------------------------------------------


def _report_link() -> str | None:
    """``DASHBOARD_URL`` itself — the report's single "Open Alert Feed" action.

    Unlike an alert card there is no per-record deep link to build: a report is
    about a period, not a row, and the feed is where triage happens. ``None`` when
    ``DASHBOARD_URL`` is unset, same rule as :func:`_alert_link`.
    """
    base = os.getenv("DASHBOARD_URL", "").rstrip("/")
    return base or None


def _department_facts(payload) -> list[dict]:
    """The backlog table: one fact per department, busiest first.

    ``N urgent`` is appended only when N > 0. The header always states the total
    urgent count (including zero), so a quiet day is still visible at a glance,
    while five rows each reading "· 0 urgent" would bury the one row that matters.
    """
    facts: list[dict] = []
    for row in payload.departments:
        value = str(row.unresolved)
        if row.urgent:
            value += f" · {row.urgent} urgent"
        facts.append({"title": row.department, "value": value})
    return facts


def build_report_card(payload) -> dict:
    """Build the twice-daily report card from a ``report.ReportPayload``.

    A SEPARATE builder, not a branch inside :func:`build_card`: that one builds an
    alert card, and when journey support was dropped it was removed rather than
    kept as a branch. Following the same precedent keeps each builder answering one
    question. ONE builder for both slots, parameterised by ``payload.is_morning`` —
    the two variants share the backlog table, the absolute window and the action,
    and differ only in what they lead with.

    All text is English. Every time-relative phrase comes from the payload, which
    derived it from the actual window (``report.describe_span`` /
    ``report.delta_period``) — nothing here says "overnight" or "today" on the
    strength of which slot it is, because Friday's 17:00 card is a weekend handover
    and Monday's 09:00 window is ~64h.
    """
    body: list[dict] = []

    if payload.is_morning:
        # 09:00 — "what's waiting for you". The title names the backlog, because
        # that is what the reader has to act on before anything else arrives.
        title = f"Waiting for you — {payload.total_unresolved} unresolved"
        subtitle = (
            f"as of {payload.as_of_label} · {payload.total_unresolved} unresolved"
            f" · {payload.total_urgent} urgent"
        )
    else:
        # 17:00 — "what we're leaving behind". Leads with the delta: the movement
        # is the day's story, and the backlog follows as what remains.
        remaining = payload.total_unresolved
        title = (
            f"End of day — {payload.created} new, {payload.resolved} resolved, "
            f"{remaining} open"
        )
        subtitle = f"as of {payload.as_of_label}"

    body.append(
        {
            "type": "TextBlock",
            "text": title,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        }
    )
    body.append(
        {"type": "TextBlock", "text": subtitle, "isSubtle": True, "spacing": "None", "wrap": True}
    )
    # The window, always in absolute local dates, with the relative phrase as a
    # SUFFIX rather than a replacement — "overnight" alone becomes a lie after a
    # weekend or an outage, and a posted card cannot be corrected.
    body.append(
        {
            "type": "TextBlock",
            "text": f"Window: {payload.window_label} · {payload.span_label}",
            "isSubtle": True,
            "spacing": "None",
            "wrap": True,
        }
    )

    if payload.is_morning:
        body.append(
            {
                "type": "TextBlock",
                "text": f"{payload.created} new alerts {payload.span_label}.",
                "wrap": True,
                "spacing": "Medium",
            }
        )
    else:
        body.append(
            {
                "type": "FactSet",
                "spacing": "Medium",
                "facts": [
                    {"title": f"Created {payload.delta_label}", "value": str(payload.created)},
                    {"title": f"Resolved {payload.delta_label}", "value": str(payload.resolved)},
                    {"title": "Remaining open", "value": str(payload.total_unresolved)},
                ],
            }
        )

    # Only when the next report is more than a day out (Friday evening, a holiday
    # bridge) — derived, so it is absent on a normal Tuesday rather than always
    # printed and usually noise.
    if payload.handover:
        body.append(
            {
                "type": "TextBlock",
                "text": payload.handover,
                "wrap": True,
                "isSubtle": True,
                "spacing": "None",
            }
        )

    heading = "Open by department" if payload.is_morning else "What remains, by department"
    body.append(
        {
            "type": "TextBlock",
            "text": heading,
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    )
    facts = _department_facts(payload)
    if facts:
        body.append({"type": "FactSet", "facts": facts})
    else:
        # Always sent, even with nothing to report: "0 unresolved" proves the
        # pipeline and this reporter are both alive, whereas silence is ambiguous.
        body.append(
            {
                "type": "TextBlock",
                "text": "Nothing unresolved — the queue is empty.",
                "wrap": True,
                "isSubtle": True,
            }
        )

    card: dict = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": body,
    }
    link = _report_link()
    if link:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "Open Alert Feed", "url": link}
        ]

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": card,
            }
        ],
    }


# --- notifier (I/O) ----------------------------------------------------------


def _webhook_url(channel: str) -> str | None:
    """Resolve the ``TEAMS_WEBHOOK_<CHANNEL>`` env var, or ``None`` if unset.

    Derived from the channel name, so a channel needs no change here — the
    department channels read ``TEAMS_WEBHOOK_<DEPT>``, and ``fallback`` reads
    ``TEAMS_WEBHOOK_FALLBACK``.

    ``TEAMS_WEBHOOK_REPORTS`` is read now that the twice-daily report routes to
    ``reports`` — see the note on :data:`REPORTS` for what that channel does and
    does not mean, and for why it is no longer called ``GENERAL``.
    """
    return os.getenv(f"TEAMS_WEBHOOK_{channel.upper()}") or None


def _card_for(event: dict) -> dict:
    """Pick the builder for this event type.

    The dispatch lives HERE rather than inside a builder: each builder answers one
    question (:func:`build_card` an alert, :func:`build_report_card` a report), and
    a card that branched on event type internally is exactly what was removed when
    journey support was dropped. Anything unrecognised cannot reach this function —
    :func:`channel_for` returned ``None`` for it and :func:`notify` already
    returned.
    """
    if event.get("type") == "report.daily":
        return build_report_card((event.get("data") or {})["payload"])
    return build_card(event)


async def notify(event: dict, *, client: httpx.AsyncClient | None = None) -> None:
    """Notify Teams about an event (no-op for ignored events).

    Resolves the channel; ``None`` → nothing to do. If the channel's webhook env
    var is unset, prints the card to stdout (never crashes on missing config);
    otherwise POSTs the card with ``httpx``. ``client`` may be injected (tests /
    connection reuse); otherwise a short-lived client is used.
    """
    channel = channel_for(event)
    if channel is None:
        return

    card = _card_for(event)
    url = _webhook_url(channel)
    if url is None:
        print(
            f"[teams:{channel}] no webhook configured — card:\n"
            f"{json.dumps(card, indent=2, ensure_ascii=False)}",
            flush=True,
        )
        return

    try:
        if client is not None:
            resp = await client.post(url, json=card)
        else:
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.post(url, json=card)
        print(f"[teams:{channel}] POST -> {resp.status_code}", flush=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — surface the failure, never crash the loop
        print(f"[teams:{channel}] POST FAILED: {exc}", flush=True)
