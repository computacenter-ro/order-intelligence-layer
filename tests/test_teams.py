"""Tests for backend/teams.py — Microsoft Teams notifications.

Pure logic (routing + card building) is tested directly; the notifier's I/O is
driven with a fake httpx-style client and env monkeypatching — no network.
"""

import pytest

from backend import teams, ws
from backend.teams import channel_for, build_card, notify
from shared.models import Department


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
#
# TWO invariants are pinned here, and the first is the module's defining property:
#
# 1. `alert.new` is the ONLY event type that resolves to a channel. Every other
#    event notifies nothing — `journey.completed` included, since it was dropped
#    outright (thousands of cards a day at the deployed flow rate) rather than
#    deferred to the stage-3 report.
# 2. A channel name is NOT a department name, even where the strings coincide.
#    Every department maps to a like-named channel (the `general` department became
#    `business`), but `fallback` is a channel with no department, and the
#    `reports` channel has no department either.
#
# ⚠ The word "general" still appears in this file, and every occurrence is the
# retired department VALUE, fed deliberately as stale input. It is never a live
# department and never a channel: the report channel is `reports`
# (`TEAMS_WEBHOOK_REPORTS`), renamed from `general` because that name had meant
# three different things.
# =============================================================================

# The complete roster of event types the system emits, derived from backend/ws.py
# rather than restated, so this file cannot fall behind the hub it mirrors.
_ALL_EVENT_TYPES = [
    ws.EVENT_ALERT_NEW,
    ws.EVENT_JOURNEY_UPDATED,
    ws.EVENT_JOURNEY_COMPLETED,
    ws.EVENT_INCIDENT_NEW,
    ws.EVENT_INCIDENT_UPDATED,
]

# Minimal `data` per event type — enough for channel_for, which only ever reads
# `source` and `department` (and only on alert.new).
_EVENT_DATA = {
    ws.EVENT_ALERT_NEW: {"source": "ai", "department": "backend"},
    ws.EVENT_JOURNEY_UPDATED: {"journey_id": "J1"},
    ws.EVENT_JOURNEY_COMPLETED: {"journey_id": "J1", "outcome": "SUCCESS"},
    ws.EVENT_INCIDENT_NEW: {"incident_id": "I1"},
    ws.EVENT_INCIDENT_UPDATED: {"incident_id": "I1"},
}


@pytest.mark.parametrize(
    "department", ["networking", "devops", "backend", "database"]
)
def test_channel_for_ai_alert_goes_to_its_engineering_department(department):
    assert channel_for(_alert_event(source="ai", department=department)) == department


def test_channel_for_ai_business_alert_is_the_business_channel():
    """A business rejection (margin block, missing costCenter UDF, disabled JAM
    account, unmapped product) is `source="ai"` + `department="business"`: the
    pipeline worked as designed and correctly rejected an order. It gets its own
    channel — nobody in engineering has work to do on it.

    Department and channel are the same string here, which is the whole point of
    the rename. It is NOT a reason to drop the mapping table — see
    `test_department_channel_mapping_is_explicit_not_identity` below."""
    assert channel_for(_alert_event(source="ai", department="business")) == "business"


def test_channel_for_fallback_alert_is_fallback():
    """An unprocessed pass-through (the LLM was down) — a different fact from a
    business rejection, and it used to share the `general` channel with one."""
    assert channel_for(_alert_event(source="fallback", department=None)) == "fallback"


def test_channel_for_ai_alert_without_department_is_fallback():
    """Defensive: cannot happen per the ProcessedAlert contract (an AI alert
    always has a department). If it does, it is semantically unprocessed, so it
    goes to `fallback` — never to `business`, which would assert the pipeline
    deliberately rejected the order. And never an exception."""
    assert channel_for(_alert_event(source="ai", department=None)) == "fallback"


def test_channel_for_ai_alert_with_unknown_department_is_fallback():
    """Same reasoning for a department outside the enum: route it, don't raise."""
    assert channel_for(_alert_event(source="ai", department="quantum")) == "fallback"


def test_channel_for_journey_completed_is_none():
    """Completed journeys notify NOTHING. They used to post to `general`, kept as
    an interim while the daily report did not exist; dropped outright because at
    ~2 flows/30s that is a card per completed journey — thousands a day, more than
    the alerts themselves, and the same volume that made `general` unreadable.

    This is not a gap to fill: the stage-3 report covers completions in aggregate.
    The dashboard still receives the event over the WebSocket."""
    assert channel_for(_journey_completed_event()) is None


@pytest.mark.parametrize("type_", _ALL_EVENT_TYPES)
def test_alert_new_is_the_only_ws_event_that_resolves_to_a_channel(type_):
    """Of the events on the WebSocket, only `alert.new` notifies Teams.

    Parametrised from `backend/ws.py`'s constants so this cannot silently cover four
    of five event types after someone adds a fifth. `report.daily` is deliberately
    NOT in that roster — it never travels over the WebSocket and reaches Teams
    directly from the scheduler — so it is out of scope here and covered in
    tests/test_report.py."""
    channel = channel_for({"type": type_, "data": _EVENT_DATA[type_]})
    if type_ == ws.EVENT_ALERT_NEW:
        assert channel is not None
    else:
        assert channel is None, f"{type_} must not notify Teams"


def test_the_event_roster_is_complete():
    """Forces a decision when a new event type is added.

    The test above proves every KNOWN event type behaves; this proves the known
    list is the whole list. Add an event type to `backend/ws.py` and this fails —
    at which point you must decide whether Teams should notify it (and say so in
    `channel_for`) rather than inheriting `None` by accident. `None` is very likely
    the right answer; the point is that it be chosen."""
    emitted = {
        value
        for name, value in vars(ws).items()
        if name.startswith("EVENT_") and isinstance(value, str)
    }
    assert emitted == set(_ALL_EVENT_TYPES)


def test_no_alert_or_journey_event_can_route_to_the_general_channel():
    """`reports` carries the twice-daily report and NOTHING else.

    Updated deliberately, not worked around: stage 1 removed the channel constant
    and asserted its absence precisely so that re-adding it would have to be a
    conscious act. Adding the report (`backend/report.py`) was that act, so the
    constant is back and this test now pins the narrower invariant — no ALERT and no
    journey/incident event may reach it, whatever combination of `source` and
    `department` they carry, including contract violations and the retired
    `"general"` department value.
    """
    sources = ["ai", "fallback", None, "", "something-new"]
    departments = [d.value for d in Department] + [None, "", "quantum", "general"]
    for source in sources:
        for department in departments:
            channel = channel_for(_alert_event(source=source, department=department))
            assert channel != teams.REPORTS, (source, department)
    for type_ in _ALL_EVENT_TYPES:
        assert channel_for({"type": type_, "data": _EVENT_DATA[type_]}) != teams.REPORTS
    # The one thing that DOES route there. (Fully covered in tests/test_report.py.)
    assert channel_for({"type": "report.daily", "data": {}}) == teams.REPORTS


def test_every_department_has_a_channel():
    """Guard against enum drift: a department added to `shared/models.py` without
    a row in the table would silently route to `fallback` instead of anywhere
    useful — exactly the kind of drift `_DEPARTMENT_GUIDE` needs a test for too."""
    assert set(teams._DEPARTMENT_CHANNELS) == {d.value for d in Department}


def test_department_channel_mapping_is_explicit_not_identity():
    """Every row of the table is an identity mapping (`business` → `business`
    completed the set), which invites "just return data['department']". That would
    re-couple the two namespaces, and they are NOT the same set: `fallback` is a
    channel with no department.

    So this pins the SETS apart rather than the table's contents — it keeps passing
    if a department is added, and fails if someone concludes the two are the same
    thing. `_DEPARTMENT_CHANNELS` is also still the only reason an unknown
    department degrades to `fallback` instead of inventing a channel name from
    whatever string arrived on the wire."""
    channels = {
        teams.NETWORKING, teams.DEVOPS, teams.BACKEND, teams.DATABASE,
        teams.BUSINESS, teams.FALLBACK, teams.REPORTS,
    }
    departments = {d.value for d in Department}
    assert teams.FALLBACK not in departments
    assert teams.REPORTS not in departments
    assert channels - departments == {teams.FALLBACK, teams.REPORTS}
    # ...and a department is never used as a channel without going through the table.
    assert channel_for(_alert_event(source="ai", department="quantum")) == teams.FALLBACK


def test_channel_for_journey_updated_is_none():
    """Per-chunk updates would be spam — unchanged."""
    assert channel_for({"type": "journey.updated", "data": {"journey_id": "J1"}}) is None


@pytest.mark.parametrize("type_", ["incident.new", "incident.updated"])
def test_channel_for_incident_events_are_none(type_):
    """Incidents are a dashboard view, not a notification channel — unchanged."""
    assert channel_for({"type": type_, "data": {"incident_id": "I1"}}) is None


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
    # The router no longer produces a confidence, so the card never shows one —
    # not even when an upstream payload still carries the key.
    assert "Confidence" not in facts
    assert facts["Order"] == "ORD-1"
    assert facts["Event"] == "evt-1"
    assert facts["Cart"] == "C1"
    # ONE action, pointing at the alert itself — not the journey.
    actions = _content(card)["actions"]
    assert len(actions) == 1, "a second button would duplicate the drawer's RELATED link"
    action = actions[0]
    assert action["type"] == "Action.OpenUrl"
    assert action["title"] == "View alert"
    assert action["url"] == "https://dash.example.com/?alert=al-1"


def test_build_card_badge_and_text_differ_between_ai_and_fallback():
    ai = build_card(_alert_event(source="ai"))
    fb = build_card(_alert_event(source="fallback", explanation=None,
                                 department=None))
    # badge differs
    assert "AI-analyzed" in _text_blocks(ai)
    assert "AI-analyzed" not in _text_blocks(fb)
    assert "fallback" in _text_blocks(fb)
    # text differs
    assert "SPT pricing service was unreachable" in _text_blocks(ai)
    assert "unprocessed — LLM unavailable" in _text_blocks(fb)


def test_build_card_fallback_alert_uses_placeholder_explanation():
    card = build_card(_alert_event(source="fallback", explanation=None,
                                   department=None))
    assert "fallback" in _text_blocks(card)
    assert "unprocessed — LLM unavailable" in _text_blocks(card)
    # the card never renders a confidence fact — the field no longer exists
    assert "Confidence" not in _facts(card)


def test_build_card_ignores_a_stale_confidence_in_the_payload():
    """Defensive: an in-flight event from an older build could still carry a
    ``confidence`` key. The card must ignore it rather than render a score the
    system no longer produces."""
    card = build_card(_alert_event(confidence=0.82))
    assert "Confidence" not in _facts(card)


def test_build_card_builds_an_alert_card_only(monkeypatch):
    """`build_card` no longer reads journey fields, because no journey event can
    reach it: `channel_for` resolves a channel for `alert.new` and nothing else.
    The `outcome`-as-level and `summary`-as-body handling was removed with the
    routing rather than left as annotated dead code.

    Pinned so nobody "restores" it by noticing the keys are ignored: an event
    carrying BOTH shapes renders the alert fields, never the journey ones."""
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    card = build_card(
        _alert_event(outcome="SUCCESS", summary="Order flowed end to end.")
    )
    texts = _text_blocks(card)
    assert "SPT pricing service was unreachable" in texts   # explanation, not summary
    assert "Order flowed end to end." not in texts
    assert _facts(card)["Level"] == "ERROR"                 # level, not outcome


def _monospace_blocks(card: dict) -> list[str]:
    return [
        b["text"]
        for b in _content(card)["body"]
        if b.get("type") == "TextBlock" and b.get("fontType") == "Monospace"
    ]


def test_build_card_shows_the_raw_log_line_when_there_is_no_explanation():
    """A fallback card used to carry the service, the level and NOTHING about what
    broke — least informative exactly when the AI explained nothing. The raw
    `message` fills that gap, in monospace so a log line reads as a log line.

    The "unprocessed" note stays alongside it: the two say different things (WHY
    there is no explanation vs WHAT was logged), so neither replaces the other."""
    card = build_card(
        _alert_event(source="fallback", explanation=None, department=None,
                     severity=None, message="SPT read timed out after 3 attempt(s)")
    )
    texts = _text_blocks(card)
    assert "unprocessed — LLM unavailable" in texts
    assert "SPT read timed out after 3 attempt(s)" in texts
    assert _monospace_blocks(card) == ["SPT read timed out after 3 attempt(s)"]


def test_build_card_does_not_append_the_raw_line_to_an_ai_card():
    """An explanation already says what happened in readable prose. Appending the
    raw line too would roughly double every AI card to restate it — and card length
    is what makes a channel tiring to read."""
    card = build_card(_alert_event(message="SPT pricing timeout"))
    assert "SPT pricing service was unreachable" in _text_blocks(card)  # explanation
    assert _monospace_blocks(card) == []
    assert "SPT pricing timeout" not in _text_blocks(card)


def test_build_card_without_explanation_or_message_still_explains_itself():
    """Degrades to the note alone rather than an empty body — `message` is always
    present in practice, but the card must not depend on it to say anything."""
    card = build_card(_alert_event(source="fallback", explanation=None, message=None))
    assert "unprocessed — LLM unavailable" in _text_blocks(card)
    assert _monospace_blocks(card) == []


def test_build_card_no_action_without_dashboard_url(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    assert "actions" not in _content(build_card(_alert_event()))


def test_build_card_always_has_an_action_when_dashboard_url_is_set(monkeypatch):
    """**The reason this link changed.** ``alert_id`` is non-nullable, so the card
    always has a button — including for an alert with ``journey_id = None``, which
    is exactly the case the old journey link failed on.

    An alert is stitched to a journey by a LATER `raw.events` message, so at
    `alert.new` time `journey_id` is routinely still null (see backend/linking.py,
    which back-fills it). The old link was DASHBOARD_URL + journey_id, so those
    cards arrived with no action at all — a notification about something you then
    could not open. Parametrised over the shapes that used to break it."""
    monkeypatch.setenv("DASHBOARD_URL", "https://d")
    for label, event in (
        ("unstitched", _alert_event(journey_id=None)),
        ("no order id either", _alert_event(journey_id=None, order_id=None)),
        ("bare minimum", {"type": "alert.new", "data": {"alert_id": "al-9"}}),
        ("fallback", _alert_event(source="fallback", explanation=None,
                                  department=None, severity=None, journey_id=None)),
    ):
        actions = _content(build_card(event)).get("actions")
        assert actions, f"no action for the {label} case"
        assert actions[0]["title"] == "View alert"
        assert actions[0]["url"].startswith("https://d/?alert=")


def test_build_card_link_normalizes_a_trailing_slash(monkeypatch):
    """DASHBOARD_URL is operator-supplied and often ends in "/". Without the strip
    the URL would be `https://d//?alert=...`."""
    monkeypatch.setenv("DASHBOARD_URL", "https://dash.example.com/")
    card = build_card(_alert_event(alert_id="al-3"))
    assert _content(card)["actions"][0]["url"] == "https://dash.example.com/?alert=al-3"


def test_build_card_link_ignores_order_id_and_journey_id(monkeypatch):
    """The link is addressed by ``alert_id`` alone.

    ``order_id`` must never be part of it — the rule that outlived the retargeting.
    It used to be a fallback for the journey link and produced ``/journeys/ORD-9``,
    a guaranteed "Journey not found" (that route resolves a journey id). The same
    trap still applies to ``backend/api.py::_dashboard_link``, which does still
    build journey links for chat citations."""
    monkeypatch.setenv("DASHBOARD_URL", "https://d")
    url = _content(build_card(
        _alert_event(alert_id="al-7", journey_id="J7", order_id="ORD-9")
    ))["actions"][0]["url"]
    assert url == "https://d/?alert=al-7"
    assert "ORD-9" not in url
    assert "J7" not in url and "journeys" not in url


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
    """An unset webhook prints the card to stdout — never crashes on missing
    config. Exercised on a department channel; it used to use journey.completed
    routed to `general`, which notifies nothing at all now."""
    monkeypatch.delenv("TEAMS_WEBHOOK_DEVOPS", raising=False)
    client = _FakeClient()
    await notify(_alert_event(source="ai", department="devops"), client=client)
    assert client.posts == []  # never posted
    out = capsys.readouterr().out
    assert "devops" in out.lower()  # printed the card to stdout, no crash


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


async def test_notify_routes_a_business_rejection_to_the_business_webhook(monkeypatch):
    """`_webhook_url` derives the env var from the CHANNEL, so the new channels
    need no change there — `business` reads TEAMS_WEBHOOK_BUSINESS."""
    monkeypatch.setenv("TEAMS_WEBHOOK_BUSINESS", "https://hook/business")
    # The general CHANNEL's webhook is set too, so a regression that sent alerts
    # back there would post successfully instead of falling into the stdout path
    # and passing by accident.
    monkeypatch.setenv("TEAMS_WEBHOOK_REPORTS", "https://hook/reports")
    client = _FakeClient()
    await notify(_alert_event(source="ai", department="business"), client=client)
    assert client.posts[0][0] == "https://hook/business"


async def test_notify_routes_a_fallback_alert_to_the_fallback_webhook(monkeypatch):
    monkeypatch.setenv("TEAMS_WEBHOOK_FALLBACK", "https://hook/fallback")
    monkeypatch.setenv("TEAMS_WEBHOOK_REPORTS", "https://hook/reports")
    client = _FakeClient()
    await notify(
        _alert_event(source="fallback", department=None, explanation=None),
        client=client,
    )
    assert client.posts[0][0] == "https://hook/fallback"


async def test_notify_prints_when_a_new_channels_webhook_is_unset(monkeypatch, capsys):
    """The new channels inherit the never-crash-on-missing-config posture: an
    unconfigured webhook prints the card to stdout."""
    monkeypatch.delenv("TEAMS_WEBHOOK_BUSINESS", raising=False)
    client = _FakeClient()
    await notify(_alert_event(source="ai", department="business"), client=client)
    assert client.posts == []
    assert "business" in capsys.readouterr().out.lower()


# --- notify default I/O path: real httpx.AsyncClient mocked ------------------


async def test_notify_uses_httpx_when_no_client_injected(monkeypatch):
    # Exercise the default branch (no client=): notify() opens its own
    # httpx.AsyncClient and POSTs the card. We mock httpx so nothing hits the
    # network. Driven by an alert — the only event type that reaches this path.
    monkeypatch.setenv("TEAMS_WEBHOOK_BACKEND", "https://hook/backend")
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

    event = _alert_event(source="ai", department="backend")
    await notify(event)  # no client= -> goes through httpx

    assert posts == [("https://hook/backend", build_card(event))]


@pytest.mark.parametrize(
    "type_", [t for t in _ALL_EVENT_TYPES if t != ws.EVENT_ALERT_NEW]
)
async def test_notify_sends_nothing_for_any_non_alert_event(monkeypatch, type_):
    """Every non-alert event is silent at the I/O layer, not merely unrouted:
    assert `httpx.AsyncClient` is never even constructed.

    Parametrised over the whole roster, so `journey.completed` is covered by the
    same guard that has always covered `journey.updated` — no card, no request, no
    stdout fallback either (a channel of None returns before the print).
    """
    def _boom(*a, **k):
        raise AssertionError(f"httpx.AsyncClient must not be constructed for {type_}")

    monkeypatch.setattr(teams.httpx, "AsyncClient", _boom)
    # Every webhook configured, so nothing passes merely for want of a URL.
    for channel in ("REPORTS", "BACKEND", "BUSINESS", "FALLBACK"):
        monkeypatch.setenv(f"TEAMS_WEBHOOK_{channel}", f"https://hook/{channel.lower()}")

    client = _FakeClient()
    await notify({"type": type_, "data": _EVENT_DATA[type_]}, client=client)
    assert client.posts == []

    await notify({"type": type_, "data": _EVENT_DATA[type_]})  # no client= either
