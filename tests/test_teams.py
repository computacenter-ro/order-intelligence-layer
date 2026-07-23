"""Tests for backend/teams.py — Microsoft Teams notifications.

Pure logic (routing + card building) is tested directly; the notifier's I/O is
driven with a fake httpx-style client and env monkeypatching — no network.
"""

import pytest

from backend import teams
from backend.teams import channel_for, build_card, notify


# --- event fixtures (the {"type","data"} envelopes from backend/ws.py) -------


def _alert_event(**over) -> dict:
    data = {
        "alert_id": "al-1",
        "emitted_at": "2026-07-20T08:00:00Z",
        "log_id": "log-1",
        "level": "ERROR",
        "app_name": "cc-spt-service",
        "logger": "c.c.spt.Client",
        "message": "SPT pricing timeout",
        "event_id": "evt-1",
        "order_id": "ORD-1",
        "cart_header_id": "C1",
        "account_number": "81036533",
        "explanation": "SPT pricing service was unreachable",
        "department": "backend",
        "confidence": 0.82,
        "source": "ai",
        "journey_id": "J1",
    }
    data.update(over)
    return {"type": "alert.new", "data": data}


def _journey_completed_event(**over) -> dict:
    data = {
        "journey_id": "J1",
        "status": "SUCCESS",
        "outcome": "SUCCESS",
        "order_id": "ORD-1",
        "summary": "Order flowed end to end.",
        "events": [],
    }
    data.update(over)
    return {"type": "journey.completed", "data": data}


def _content(card: dict) -> dict:
    """The AdaptiveCard content out of the ``message`` envelope."""
    return card["attachments"][0]["content"]


def _facts(card: dict) -> dict:
    for block in _content(card)["body"]:
        if block.get("type") == "FactSet":
            return {f["title"]: f["value"] for f in block["facts"]}
    return {}


def _text_blocks(card: dict) -> list[str]:
    return [b["text"] for b in _content(card)["body"] if b.get("type") == "TextBlock"]


# --- channel_for (pure routing) ----------------------------------------------


def test_channel_for_ai_alert_with_department():
    assert channel_for(_alert_event(source="ai", department="devops")) == "devops"


def test_channel_for_ai_alert_without_department_is_general():
    assert channel_for(_alert_event(source="ai", department=None)) == "general"


def test_channel_for_fallback_alert_is_general():
    assert channel_for(_alert_event(source="fallback", department=None)) == "general"


def test_channel_for_journey_completed_is_general():
    assert channel_for(_journey_completed_event()) == "general"


def test_channel_for_journey_updated_is_none():
    assert channel_for({"type": "journey.updated", "data": {"journey_id": "J1"}}) is None


def test_channel_for_unknown_type_is_none():
    assert channel_for({"type": "something.else", "data": {}}) is None


# --- build_card (pure) -------------------------------------------------------


def test_build_card_is_message_envelope_with_adaptive_card():
    card = build_card(_alert_event())
    assert card["type"] == "message"
    attachment = card["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert attachment["content"]["type"] == "AdaptiveCard"
    assert attachment["content"]["version"] == "1.4"


def test_build_card_ai_alert(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.example.com")
    card = build_card(_alert_event())
    texts = _text_blocks(card)
    # title = event type + service
    assert "alert.new · cc-spt-service" in texts
    assert "AI-analyzed" in texts  # badge
    assert "SPT pricing service was unreachable" in texts  # explanation
    facts = _facts(card)
    assert facts["Level"] == "ERROR"
    assert facts["Department"] == "backend"
    assert facts["Confidence"] == "0.82"
    assert facts["Order"] == "ORD-1"
    assert facts["Event"] == "evt-1"
    assert facts["Cart"] == "C1"
    # journey link preferred
    action = _content(card)["actions"][0]
    assert action["type"] == "Action.OpenUrl"
    assert action["title"] == "View journey"
    assert action["url"].endswith("J1")


def test_build_card_badge_and_text_differ_between_ai_and_fallback():
    ai = build_card(_alert_event(source="ai"))
    fb = build_card(_alert_event(source="fallback", explanation=None,
                                 department=None, confidence=None))
    # badge differs
    assert "AI-analyzed" in _text_blocks(ai)
    assert "AI-analyzed" not in _text_blocks(fb)
    assert "fallback" in _text_blocks(fb)
    # text differs
    assert "SPT pricing service was unreachable" in _text_blocks(ai)
    assert "unprocessed — LLM unavailable" in _text_blocks(fb)


def test_build_card_fallback_alert_uses_placeholder_explanation():
    card = build_card(_alert_event(source="fallback", explanation=None,
                                   department=None, confidence=None))
    assert "fallback" in _text_blocks(card)
    assert "unprocessed — LLM unavailable" in _text_blocks(card)
    # no confidence fact when it is null
    assert "Confidence" not in _facts(card)


def test_build_card_journey_completed(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.example.com/")
    card = build_card(_journey_completed_event())
    texts = _text_blocks(card)
    assert "journey.completed" in texts[0]  # title
    assert "Order flowed end to end." in texts  # summary as body text
    assert _facts(card)["Level"] == "SUCCESS"  # outcome
    assert _content(card)["actions"][0]["url"] == "https://dash.example.com/journeys/J1"


def test_build_card_no_action_without_dashboard_url(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    assert "actions" not in _content(build_card(_alert_event()))


def test_build_card_link_falls_back_to_order_id(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "https://d")
    card = build_card(_journey_completed_event(journey_id=None, order_id="ORD-9"))
    assert _content(card)["actions"][0]["url"].endswith("ORD-9")


# --- notify (I/O, faked) -----------------------------------------------------


class _FakeClient:
    def __init__(self):
        self.posts = []

    async def post(self, url, json=None):
        self.posts.append((url, json))


async def test_notify_noop_when_channel_is_none():
    client = _FakeClient()
    await notify({"type": "journey.updated", "data": {}}, client=client)
    assert client.posts == []


async def test_notify_prints_when_webhook_unset(monkeypatch, capsys):
    monkeypatch.delenv("TEAMS_WEBHOOK_GENERAL", raising=False)
    client = _FakeClient()
    await notify(_journey_completed_event(), client=client)
    assert client.posts == []  # never posted
    out = capsys.readouterr().out
    assert "general" in out.lower()  # printed the card to stdout, no crash


async def test_notify_posts_to_configured_webhook(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_BACKEND", "https://hook.example/backend")
    client = _FakeClient()
    event = _alert_event(source="ai", department="backend")
    await notify(event, client=client)
    assert len(client.posts) == 1
    url, payload = client.posts[0]
    assert url == "https://hook.example/backend"
    assert payload == build_card(event)


async def test_notify_routes_ai_alert_to_department_webhook(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_NETWORKING", "https://hook/net")
    client = _FakeClient()
    await notify(_alert_event(source="ai", department="networking"), client=client)
    assert client.posts[0][0] == "https://hook/net"


# --- notify default I/O path: real httpx.AsyncClient mocked ------------------


async def test_notify_uses_httpx_when_no_client_injected(monkeypatch):
    # Exercise the default branch (no client=): notify() opens its own
    # httpx.AsyncClient and POSTs the card. We mock httpx so nothing hits the
    # network.
    monkeypatch.setenv("TEAMS_WEBHOOK_GENERAL", "https://hook/general")
    posts: list[tuple[str, dict]] = []

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            posts.append((url, json))

    monkeypatch.setattr(teams.httpx, "AsyncClient", _MockAsyncClient)

    event = _journey_completed_event()
    await notify(event)  # no client= -> goes through httpx

    assert posts == [("https://hook/general", build_card(event))]


async def test_notify_journey_updated_sends_nothing_via_httpx(monkeypatch):
    # journey.updated must never notify — assert the httpx path is never taken.
    def _boom(*a, **k):
        raise AssertionError("httpx.AsyncClient must not be constructed")

    monkeypatch.setattr(teams.httpx, "AsyncClient", _boom)
    await notify({"type": "journey.updated", "data": {"journey_id": "J1"}})  # no-op
