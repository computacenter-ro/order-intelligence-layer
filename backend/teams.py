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
    """Build a simple card ``dict`` (title + fields + link) for an event.

    Deliberately minimal and structural (no Teams/Adaptive-Card markup) so it
    can be mapped onto either an Incoming Webhook or a Power Automate payload.
    """
    type_ = event.get("type", "")
    data = event.get("data") or {}
    is_fallback = data.get("source") == "fallback"
    badge = "fallback" if is_fallback else "AI"

    # Headline: outcome for a completed journey, level for an alert.
    if type_ == "journey.completed":
        headline = data.get("outcome") or data.get("status") or "journey"
        title = f"Journey {headline}"
    else:
        headline = data.get("level") or type_
        service = data.get("app_name") or ""
        title = f"{headline} · {service}".strip(" ·") or type_

    # Explanation: the AI text, the fallback placeholder, or a journey summary.
    if is_fallback:
        explanation = "unprocessed — LLM unavailable"
    else:
        explanation = data.get("explanation") or data.get("summary")

    fields: list[dict] = []
    if data.get("app_name"):
        fields.append({"name": "Service", "value": data["app_name"]})
    if explanation:
        fields.append({"name": "Explanation", "value": explanation})
    for label, key in (
        ("Event", "event_id"),
        ("Order", "order_id"),
        ("Cart", "cart_header_id"),
        ("Journey", "journey_id"),
    ):
        if data.get(key):
            fields.append({"name": label, "value": data[key]})
    if data.get("confidence") is not None:
        fields.append({"name": "Confidence", "value": f"{data['confidence']:.2f}"})
    if data.get("message"):
        fields.append({"name": "Message", "value": data["message"]})
    fields.append({"name": "Analyzed by", "value": badge})

    return {
        "title": title,
        "badge": badge,
        "text": explanation or "",
        "fields": fields,
        "link": _dashboard_link(data),
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

    if client is not None:
        await client.post(url, json=card)
    else:
        async with httpx.AsyncClient(timeout=10.0) as http:
            await http.post(url, json=card)
