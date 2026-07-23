"""[5] Core Backend — Microsoft Teams notifications (CLAUDE.md [5] "Slack").

Teams-flavoured sibling of the Slack notifier: it consumes the same
``{"type": ..., "data": ...}`` event envelopes that ``backend/ws.py`` broadcasts
and turns the interesting ones into channel notifications.

Pure logic is separated from I/O so it is trivially testable:

* :func:`channel_for` — routing: which channel (if any) an event belongs to.
* :func:`build_card` — a simple, transport-agnostic card ``dict`` (title +
  fields + link), easy to adapt between an Incoming Webhook and a Power Automate
  flow.
* :func:`notify` — the only I/O: resolve the channel + its webhook URL and POST
  the card with ``httpx``; if the channel's webhook env var is unset, print the
  card to stdout instead. **Never crash on missing config.**

Routing (CLAUDE.md): AI-analyzed alerts go to their department channel, fallback
alerts and completed journeys go to ``general``, and ``journey.updated`` events
are ignored (they would be spam).

Nothing here is wired to the event stream yet — this is just the module.
"""

from __future__ import annotations

import json
import os

import httpx

# Channel names == Department values (shared/models.py) plus "general".
NETWORKING = "networking"
DEVOPS = "devops"
BACKEND = "backend"
DATABASE = "database"
GENERAL = "general"


# --- routing (pure) ----------------------------------------------------------


def channel_for(event: dict) -> str | None:
    """Return the channel an event should notify, or ``None`` to skip it.

    * ``alert.new`` → the alert's ``department`` when it was AI-analyzed and a
      department is set, else ``general``.
    * ``journey.completed`` → ``general``.
    * ``journey.updated`` (and anything else) → ``None`` (ignored — Teams would
      be spammed by per-chunk updates).
    """
    type_ = event.get("type")
    data = event.get("data") or {}

    if type_ == "alert.new":
        if data.get("source") == "ai" and data.get("department"):
            return data["department"]
        return GENERAL
    if type_ == "journey.completed":
        return GENERAL
    return None


# --- card (pure) -------------------------------------------------------------


def _dashboard_link(data: dict) -> str | None:
    """Link to the journey view: ``DASHBOARD_URL`` + journey_id (or order_id)."""
    base = os.getenv("DASHBOARD_URL", "").rstrip("/")
    ref = data.get("journey_id") or data.get("order_id")
    if not base or not ref:
        return None
    return f"{base}/journeys/{ref}"


def build_card(event: dict) -> dict:
    """Build the exact Teams payload: a ``message`` envelope wrapping an
    Adaptive Card (confirmed working against the Teams workflow with ``curl``).

    Shape::

        {"type": "message",
         "attachments": [{
           "contentType": "application/vnd.microsoft.card.adaptive",
           "content": { <AdaptiveCard 1.4> }
         }]}

    Works for both ``alert.new`` and ``journey.completed`` events — it reads the
    fields that exist in ``data`` and skips the ones that don't.
    """
    type_ = event.get("type", "")
    data = event.get("data") or {}
    is_fallback = data.get("source") == "fallback"

    # Title: event type + service, e.g. "alert.new · cc-spt-service".
    service = data.get("app_name")
    title = f"{type_} · {service}" if service else type_

    # Badge.
    badge = "AI-analyzed" if data.get("source") == "ai" else "fallback"

    # Body text: fallback placeholder, else the explanation (or journey summary).
    if is_fallback:
        text = "unprocessed — LLM unavailable"
    else:
        text = data.get("explanation") or data.get("summary") or ""

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

    # FactSet: level|outcome, severity, department, confidence, order/event/cart ids.
    facts: list[dict] = []
    status = data.get("level") or data.get("outcome")
    if status:
        facts.append({"title": "Level", "value": str(status)})
    if data.get("severity"):
        facts.append({"title": "Severity", "value": str(data["severity"]).capitalize()})
    if data.get("department"):
        facts.append({"title": "Department", "value": str(data["department"])})
    if data.get("confidence") is not None:
        facts.append({"title": "Confidence", "value": f"{data['confidence']:.2f}"})
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

    link = _dashboard_link(data)
    if link:
        card["actions"] = [
            {"type": "Action.OpenUrl", "title": "View journey", "url": link}
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
    """Resolve the ``TEAMS_WEBHOOK_<CHANNEL>`` env var, or ``None`` if unset."""
    return os.getenv(f"TEAMS_WEBHOOK_{channel.upper()}") or None


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

    card = build_card(event)
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
