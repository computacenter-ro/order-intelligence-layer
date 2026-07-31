"""Tests for slice 2 — grounded LLM composition on /chat, and the auth'd proxy.

Two layers, no network and no real LLM anywhere:

* **AI service** — a call-counting fake chat model proves whether the LLM was
  invoked, and the shared breaker is driven with a fake redis + fake clock.
* **Backend** — ``app.dependency_overrides`` for ``get_current_user`` /
  ``get_session`` (the ``tests/test_api.py`` pattern), with the AI-service call
  monkeypatched.

The property under test throughout is the degradation ladder: a healthy model
composes (``mode="ai"``), and **every** failure — breaker open, model raising,
empty output, no sources, AI service unreachable — falls back to the phase-1
deterministic answer with ``mode="retrieval-only"`` rather than erroring. The
chatbot must stay useful with the LLM completely down.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as _tz

import pytest
from fastapi.testclient import TestClient

from ai_service import api, nodes, ragindex
from ai_service.breaker import CircuitBreaker
from ai_service.llm import LLMError
from ai_service.ragindex import RagDeps, RagIndex

from tests.test_ai_service import FakeClock, FakeRedis
from tests.test_semcache import FakeEncoder

MARGIN = "cc-checker-service ERROR margin check failed below threshold for order"
SAP = "cc-outbound-osw ERROR sap rfc communication failure partner not reached"


# --- fakes -------------------------------------------------------------------
class CountingChatModel:
    """Records invocations and the exact messages it was shown."""

    def __init__(self, reply: str = "The margin check blocked it. [a1]") -> None:
        self.reply = reply
        self.calls = 0
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        self.calls += 1
        self.prompts.append("\n".join(m.content for m in messages))
        return AIMessage(content=self.reply)


class ExplodingChatModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise RuntimeError("provider is down")


def _breaker(redis=None, clock=None) -> CircuitBreaker:
    return CircuitBreaker(redis or FakeRedis(), clock=clock or FakeClock())


@pytest.fixture
def wired():
    """Install a populated index + api deps; auto-teardown both module globals."""

    def _wire(chat_model=None, breaker=None, populate=True):
        idx = RagIndex(FakeEncoder(), min_score=0.30, max_entries=100)
        if populate:
            idx.index("a1", "alert", MARGIN, {"department": "general", "order_id": "ORD-1"})
            idx.index("a2", "alert", SAP, {"department": "networking"})
        ragindex.configure(RagDeps(index=idx, redis=None, dump_key="test:ragindex"))
        api.configure(
            api.SummaryDeps(
                breaker=breaker or _breaker(), model=None, chat=chat_model
            )
        )
        return idx

    yield _wire
    ragindex.configure(None)
    api._deps = None


# =============================================================================
# nodes.compose_chat_answer + the grounding prompt
# =============================================================================
def _sources() -> list[dict]:
    return [
        {"id": "a1", "kind": "alert", "text": MARGIN,
         "metadata": {"department": "general", "order_id": "ORD-1"}, "score": 0.7},
        {"id": "a2", "kind": "alert", "text": SAP,
         "metadata": {"department": "networking"}, "score": 0.5},
    ]


def test_chat_prompt_contains_only_the_retrieved_context():
    """No leakage: the prompt must contain the retrieved records and nothing else.

    With a grounding prompt this is a correctness property — an unrelated record
    in the context is a record the model may cite as if it were relevant.
    """
    prompt = nodes.build_chat_prompt("why was it blocked", _sources())
    assert "why was it blocked" in prompt
    assert MARGIN in prompt and SAP in prompt
    assert "[a1]" in prompt and "[a2]" in prompt
    # A record that was NOT retrieved must not appear.
    assert "track-trace" not in prompt
    assert "Registered order" not in prompt


def test_chat_prompt_includes_useful_metadata_only():
    prompt = nodes.build_chat_prompt("q", _sources())
    assert "department=general" in prompt
    assert "order_id=ORD-1" in prompt
    # score is retrieval bookkeeping, not something to reason about.
    assert "score=" not in prompt


def test_prompt_carries_no_coverage_text():
    """Coverage is a SERVER-computed field, not something the model is told about.

    Regression: two earlier versions put "n of k records shown" in the context and
    asked the model to mention it only for counting questions. Measured over 12
    live calls it obeyed roughly half the time in each direction — caveats landed
    on cause questions where they were noise, and vanished from counting questions
    where they mattered. The prompt must now be silent on the subject.
    """
    prompt = nodes.build_chat_prompt("how many failed", _sources())
    for leaked in ("coverage", "limit", "more may exist", "relevance floor", "cut off"):
        assert leaked not in prompt.lower()


def test_system_prompt_forbids_asserting_a_total():
    assert "never assert a total" in nodes._CHAT_SYSTEM.lower()
    assert "only once" in nodes._CHAT_SYSTEM


def test_system_prompt_tells_the_model_to_leave_coverage_alone():
    """It is reported separately, so the model must not editorialise about it."""
    system = nodes._CHAT_SYSTEM
    assert "Do NOT discuss how many records you were given" in system
    assert "reported separately" in system


async def test_compose_returns_the_model_text():
    model = CountingChatModel("Order ORD-1 was blocked by the margin check. [a1]")
    answer = await nodes.compose_chat_answer("why", _sources(), model)
    assert answer == "Order ORD-1 was blocked by the margin check. [a1]"
    assert model.calls == 1


async def test_compose_system_prompt_states_the_grounding_rules():
    model = CountingChatModel()
    await nodes.compose_chat_answer("why", _sources(), model)
    system = model.prompts[0]
    assert "ONLY the incident records provided" in system
    assert "Never invent" in system
    assert "Cite the record ids" in system


async def test_compose_raises_without_a_model():
    with pytest.raises(LLMError):
        await nodes.compose_chat_answer("why", _sources(), None)


async def test_compose_refuses_with_no_sources():
    """Nothing to ground in is exactly when a model invents — so refuse."""
    model = CountingChatModel()
    with pytest.raises(LLMError):
        await nodes.compose_chat_answer("why", [], model)
    assert model.calls == 0  # not even attempted


async def test_compose_wraps_a_provider_error_as_llmerror():
    with pytest.raises(LLMError):
        await nodes.compose_chat_answer("why", _sources(), ExplodingChatModel())


async def test_compose_treats_empty_output_as_a_failure():
    with pytest.raises(LLMError):
        await nodes.compose_chat_answer("why", _sources(), CountingChatModel("   "))


# =============================================================================
# POST /chat — the degradation ladder
# =============================================================================
def test_healthy_model_yields_mode_ai(wired):
    model = CountingChatModel("The margin check blocked order ORD-1. [a1]")
    wired(chat_model=model)
    body = TestClient(api.app).post(
        "/chat", json={"query": "why was the order blocked by margin", "k": 3}
    ).json()
    assert body["mode"] == "ai"
    assert body["answer"] == "The margin check blocked order ORD-1. [a1]"
    assert body["sources"], "sources are returned in AI mode too"
    assert model.calls == 1


def test_sources_carry_metadata_for_link_building(wired):
    wired(chat_model=CountingChatModel())
    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold"}
    ).json()
    top = body["sources"][0]
    assert set(top) == {"id", "kind", "score", "snippet", "metadata"}
    assert top["metadata"]["department"] == "general"


def test_exploding_model_falls_back_to_retrieval_only(wired):
    """THE fallback path: the LLM raises, the endpoint still answers."""
    model = ExplodingChatModel()
    wired(chat_model=model)
    resp = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold"}
    )
    assert resp.status_code == 200  # did NOT crash
    body = resp.json()
    assert body["mode"] == "retrieval-only"
    assert "Found" in body["answer"]  # the phase-1 deterministic template
    assert body["sources"], "retrieval still worked; only composition failed"
    assert model.calls == 1


def test_no_chat_model_configured_falls_back(wired):
    """No Azure creds => chat_model() is None => retrieval-only, not an error."""
    wired(chat_model=None)
    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold"}
    ).json()
    assert body["mode"] == "retrieval-only"
    assert body["sources"]


def test_empty_model_output_falls_back(wired):
    wired(chat_model=CountingChatModel("   "))
    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold"}
    ).json()
    assert body["mode"] == "retrieval-only"


async def test_open_breaker_skips_the_llm_entirely(wired):
    """Breaker open => no call at all, and still a useful answer."""
    redis, clock = FakeRedis(), FakeClock()
    breaker = _breaker(redis, clock)
    for _ in range(3):  # trip it
        await breaker.record_failure()
    model = CountingChatModel()
    wired(chat_model=model, breaker=breaker)

    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold"}
    ).json()
    assert body["mode"] == "retrieval-only"
    assert model.calls == 0, "an open breaker must not reach the provider"


def test_no_retrieval_results_skips_the_llm(wired):
    """Nothing retrieved => nothing to ground in => don't call the model."""
    model = CountingChatModel()
    wired(chat_model=model)
    body = TestClient(api.app).post(
        "/chat", json={"query": "zebra quilt harpsichord botany"}
    ).json()
    assert body["mode"] == "retrieval-only"
    assert body["sources"] == []
    assert "No related incidents found" in body["answer"]
    assert model.calls == 0


def test_coverage_reports_truncation_when_the_limit_is_hit(wired):
    """shown == limit => more records very likely exist beyond the cut."""
    wired(chat_model=CountingChatModel())
    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold", "k": 1}
    ).json()
    assert body["coverage"] == {"shown": 1, "limit": 1, "truncated": True}


def test_coverage_reports_no_truncation_below_the_limit(wired):
    """Fewer results than k => the relevance floor ended the list, not the cap."""
    wired(chat_model=CountingChatModel())
    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold", "k": 10}
    ).json()
    assert body["coverage"]["truncated"] is False
    assert body["coverage"]["limit"] == 10
    assert body["coverage"]["shown"] == len(body["sources"])


def test_coverage_is_present_on_the_retrieval_only_path(wired):
    """An LLM outage must not cost the caller its coverage information."""
    wired(chat_model=ExplodingChatModel())
    body = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold", "k": 1}
    ).json()
    assert body["mode"] == "retrieval-only"
    assert body["coverage"]["truncated"] is True


def test_coverage_on_an_empty_result_is_not_truncated(wired):
    """"Nothing matched" is not the same fact as "the list was cut short"."""
    wired(chat_model=CountingChatModel())
    body = TestClient(api.app).post(
        "/chat", json={"query": "zebra quilt harpsichord botany", "k": 5}
    ).json()
    assert body["coverage"] == {"shown": 0, "limit": 5, "truncated": False}


def test_coverage_is_deterministic_across_identical_requests(wired):
    """The whole point: computed, so it cannot vary run to run the way prose did."""
    wired(chat_model=CountingChatModel())
    client = TestClient(api.app)
    payload = {"query": "margin check failed below threshold", "k": 2}
    first = client.post("/chat", json=payload).json()["coverage"]
    second = client.post("/chat", json=payload).json()["coverage"]
    assert first == second


def test_the_model_only_sees_retrieved_records(wired):
    """End-to-end leakage check: a2 is in the index but must not reach the prompt
    when the query only retrieves a1."""
    model = CountingChatModel()
    wired(chat_model=model)
    TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold", "k": 1}
    )
    assert model.calls == 1
    prompt = model.prompts[0]
    assert MARGIN in prompt
    assert SAP not in prompt, "an unretrieved record leaked into the grounding context"


# =============================================================================
# backend: authenticated proxy
# =============================================================================
def _backend_client(monkeypatch, *, reply=None, capture=None):
    """A TestClient for the backend app with auth + session + AI call faked."""
    from backend.auth import get_current_user
    from backend.db import get_session
    from backend.main import app

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async def _session_override():
        yield _FakeSession()

    app.dependency_overrides[get_session] = _session_override

    async def _fake_ask(query, k=5, filters=None, boosts=None, client=None):
        if capture is not None:
            capture.append({"query": query, "k": k, "filters": filters})
        return reply or {
            "answer": "composed answer [J1]",
            "sources": [
                {"id": "J1", "kind": "journey", "score": 0.8, "snippet": "Journey FAILED: ...",
                 "metadata": {"journey_id": "J1", "order_id": "ORD-1"}}
            ],
            "mode": "ai",
        }

    monkeypatch.setattr("backend.rag_client.ask", _fake_ask)
    return TestClient(app), app


class _FakeSession:
    """Returns a seeded row for the context lookup; records the statements."""

    def __init__(self, row=None):
        self.row = row
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _FakeResult(self.row)

    async def commit(self):
        pass


class _FakeResult:
    """Serves both access shapes the chat route uses.

    ``_context_text`` calls ``scalar_one_or_none()`` for the record, then
    ``scalars().all()`` for a journey's events — so a single fake must answer both
    or the second query raises AttributeError.
    """

    def __init__(self, one, items=None):
        self._one = one
        self._items = items or []

    def scalar_one_or_none(self):
        return self._one

    def scalars(self):
        return self

    def all(self):
        return self._items


def test_backend_chat_requires_auth():
    """No session cookie => 401, before anything reaches the AI service."""
    from backend.main import app

    app.dependency_overrides.clear()
    resp = TestClient(app).post("/chat", json={"query": "anything"})
    assert resp.status_code == 401


def test_backend_chat_returns_answer_sources_and_mode(monkeypatch):
    client, app = _backend_client(monkeypatch)
    try:
        body = client.post("/chat", json={"query": "why did it fail"}).json()
        assert body["mode"] == "ai"
        assert body["answer"] == "composed answer [J1]"
        assert body["sources"][0]["id"] == "J1"
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_attaches_dashboard_links(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "http://dash.local")
    client, app = _backend_client(monkeypatch)
    try:
        body = client.post("/chat", json={"query": "q"}).json()
        assert body["sources"][0]["link"] == "http://dash.local/journeys/J1"
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_link_is_none_for_an_alert_with_only_an_order_id(monkeypatch):
    """An order id is not a journey id, so it cannot become a journey link.

    It used to be the fallback, producing ``/journeys/ORD-8944`` against a route
    that resolves a journey id — the citation chip led straight to "Journey not
    found". An alert's journey_id is nullable, so this was the common case, not an
    edge one. No link is correct: the UI renders an unlinked citation as plain
    text, and an alert chip opens the alert in the assistant panel anyway.
    """
    monkeypatch.setenv("DASHBOARD_URL", "http://dash.local")
    client, app = _backend_client(
        monkeypatch,
        reply={
            "answer": "a",
            "sources": [
                {
                    "id": "A1",
                    "kind": "alert",
                    "score": 0.9,
                    "snippet": "ERROR ...",
                    "metadata": {"order_id": "ORD-8944"},
                }
            ],
            "mode": "ai",
        },
    )
    try:
        assert client.post("/chat", json={"query": "q"}).json()["sources"][0]["link"] is None
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_links_a_journey_record_by_its_own_id(monkeypatch):
    """A journey record's id IS the journey id — the last-resort branch, kept so
    the most link-worthy citation kind never renders unlinked."""
    monkeypatch.setenv("DASHBOARD_URL", "http://dash.local")
    client, app = _backend_client(
        monkeypatch,
        reply={
            "answer": "a",
            "sources": [
                {"id": "J9", "kind": "journey", "score": 0.9, "snippet": "s", "metadata": {}}
            ],
            "mode": "ai",
        },
    )
    try:
        body = client.post("/chat", json={"query": "q"}).json()
        assert body["sources"][0]["link"] == "http://dash.local/journeys/J9"
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_link_is_none_without_dashboard_url(monkeypatch):
    monkeypatch.delenv("DASHBOARD_URL", raising=False)
    client, app = _backend_client(monkeypatch)
    try:
        assert client.post("/chat", json={"query": "q"}).json()["sources"][0]["link"] is None
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_passes_coverage_through(monkeypatch):
    """The dashboard renders coverage as a badge, so it must survive the proxy."""
    client, app = _backend_client(
        monkeypatch,
        reply={"answer": "a", "sources": [], "mode": "ai",
               "coverage": {"shown": 3, "limit": 3, "truncated": True}},
    )
    try:
        body = client.post("/chat", json={"query": "how many", "k": 3}).json()
        assert body["coverage"] == {"shown": 3, "limit": 3, "truncated": True}
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_defaults_coverage_when_absent(monkeypatch):
    """An older/degraded AI service body must not 500 the route."""
    client, app = _backend_client(
        monkeypatch, reply={"answer": "a", "sources": [], "mode": "retrieval-only"}
    )
    try:
        body = client.post("/chat", json={"query": "q"}).json()
        assert body["coverage"] == {"shown": 0, "limit": 0, "truncated": False}
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_passes_through_retrieval_only_mode(monkeypatch):
    """An LLM-down answer from the AI service reaches the dashboard as-is."""
    client, app = _backend_client(
        monkeypatch,
        reply={"answer": "Found 1 related incident(s)...", "sources": [], "mode": "retrieval-only"},
    )
    try:
        body = client.post("/chat", json={"query": "q"}).json()
        assert body["mode"] == "retrieval-only"
        assert body["sources"] == []
    finally:
        app.dependency_overrides.clear()


# --- the context ("ask about this record") path ------------------------------
# --- scoped-context event selection (pure) -----------------------------------
class _Ev:
    """A minimal JourneyEvent stand-in (only .raw and .ts are read)."""

    def __init__(self, level: str, message: str, ts: str = "2026-07-27T08:00:00Z"):
        self.raw = {"level": level, "message": message, "app_name": "cc-x", "timestamp": ts}
        self.ts = None


def test_select_context_events_keeps_warn_error_plus_first_and_last():
    """The summary alone could not answer "what came next?" or "how many retries?",
    but the full DEBUG trace would bury those answers in a large prompt. Keep the
    lines where a failure narrative actually lives."""
    from backend.api import select_context_events

    events = [
        _Ev("INFO", "received"),          # first — kept for "where did it start"
        _Ev("DEBUG", "noise 1"),
        _Ev("WARN", "retrying"),          # kept
        _Ev("DEBUG", "noise 2"),
        _Ev("ERROR", "failed"),           # kept
        _Ev("INFO", "moved to dlq"),     # last — kept for "how did it end"
    ]
    kept = [e.raw["message"] for e in select_context_events(events)]
    assert kept == ["received", "retrying", "failed", "moved to dlq"]
    assert "noise 1" not in kept and "noise 2" not in kept


def test_select_context_events_is_empty_for_no_events():
    from backend.api import select_context_events

    assert select_context_events([]) == []


def test_select_context_events_caps_the_total():
    """A journey can carry 40+ events; an uncapped context would make every scoped
    question a large, slow prompt."""
    from backend.api import _CONTEXT_EVENT_CAP, select_context_events

    events = [_Ev("ERROR", f"boom {i}") for i in range(_CONTEXT_EVENT_CAP * 2)]
    kept = select_context_events(events)
    assert len(kept) <= _CONTEXT_EVENT_CAP
    # Head and tail survive: losing the terminal line would cost more than a
    # truncated middle.
    assert kept[0].raw["message"] == "boom 0"
    assert kept[-1].raw["message"] == f"boom {_CONTEXT_EVENT_CAP * 2 - 1}"


def test_format_context_events_renders_in_the_requested_timezone():
    """The scoped context is per-request, so it CAN be localised — unlike indexed
    text, which is shared by every viewer and stays UTC."""
    from backend.api import format_context_events

    ev = _Ev("ERROR", "failed", ts="2026-07-27T16:26:19+00:00")
    assert "16:26:19 UTC" in format_context_events([ev])
    assert "19:26:19 EEST" in format_context_events([ev], "Europe/Bucharest")


def test_format_context_events_one_line_each():
    from backend.api import format_context_events

    out = format_context_events([_Ev("ERROR", "failed"), _Ev("WARN", "retrying")])
    assert out.count("\n") == 1
    assert "failed" in out and "retrying" in out
    assert "ERROR" in out and "cc-x" in out


def test_build_scoped_query_prepends_context():
    out = api_backend_build("why did this fail", "Journey FAILED: SAP was unreachable.")
    assert "Journey FAILED: SAP was unreachable." in out
    assert "why did this fail" in out
    assert out.index("Journey FAILED") < out.index("why did this fail")


def test_build_scoped_query_without_context_is_the_bare_question():
    assert api_backend_build("why did this fail", None) == "why did this fail"


def api_backend_build(query, context_text):
    from backend.api import build_scoped_query

    return build_scoped_query(query, context_text)


def test_backend_chat_context_fetches_and_prepends_the_record(monkeypatch):
    """The "ask about this journey" button: the record's text is prepended."""
    from backend.auth import get_current_user
    from backend.db import Journey, get_session
    from backend.main import app

    journey = Journey(journey_id="J1", status="FAILED", outcome="SAP_SUBMISSION_FAILED",
                      summary="SAP was unreachable after 3 retries.")
    session = _FakeSession(journey)

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override

    seen: list[dict] = []

    async def _fake_ask(query, k=5, filters=None, boosts=None, client=None):
        seen.append({"query": query})
        return {"answer": "a", "sources": [], "mode": "ai"}

    monkeypatch.setattr("backend.rag_client.ask", _fake_ask)
    try:
        client = TestClient(app)
        resp = client.post(
            "/chat",
            json={"query": "why did it fail", "context": {"kind": "journey", "id": "J1"}},
        )
        assert resp.status_code == 200
        forwarded = seen[0]["query"]
        assert "SAP was unreachable after 3 retries." in forwarded
        assert "why did it fail" in forwarded
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_journey_context_includes_log_lines(monkeypatch):
    """The point of the richer context: the forwarded question must carry the
    journey's WARN/ERROR log lines, not just its 2-4 sentence summary."""
    from backend.auth import get_current_user
    from backend.db import Journey, get_session
    from backend.main import app

    journey = Journey(
        journey_id="J1", status="FAILED", outcome="SAP_SUBMISSION_FAILED",
        order_id="ORD-9", summary="SAP was unreachable.",
    )

    class _Session:
        """First execute() returns the journey, the second returns its events."""

        def __init__(self):
            self._calls = 0

        async def execute(self, stmt):
            self._calls += 1
            if self._calls == 1:
                return _FakeResult(journey)
            return _FakeResult(None, [_Ev("ERROR", "RFC_COMMUNICATION_FAILURE")])

        async def commit(self):
            pass

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async def _session_override():
        yield _Session()

    app.dependency_overrides[get_session] = _session_override

    seen: list[str] = []

    async def _fake_ask(query, k=5, filters=None, boosts=None, client=None):
        seen.append(query)
        return {"answer": "a", "sources": [], "mode": "ai"}

    monkeypatch.setattr("backend.rag_client.ask", _fake_ask)
    try:
        resp = TestClient(app).post(
            "/chat",
            json={"query": "how many retries", "context": {"kind": "journey", "id": "J1"}},
        )
        assert resp.status_code == 200
        forwarded = seen[0]
        assert "SAP was unreachable." in forwarded          # the summary
        assert "RFC_COMMUNICATION_FAILURE" in forwarded     # ...AND the log line
        assert "order_id=ORD-9" in forwarded                # ...AND the ids
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_missing_context_record_degrades_to_the_bare_question(monkeypatch):
    """A stale id must not 404 the chat — the answer is merely unscoped."""
    from backend.auth import get_current_user
    from backend.db import get_session
    from backend.main import app

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async def _session_override():
        yield _FakeSession(None)  # no such record

    app.dependency_overrides[get_session] = _session_override

    seen: list[dict] = []

    async def _fake_ask(query, k=5, filters=None, boosts=None, client=None):
        seen.append({"query": query})
        return {"answer": "a", "sources": [], "mode": "retrieval-only"}

    monkeypatch.setattr("backend.rag_client.ask", _fake_ask)
    try:
        resp = TestClient(app).post(
            "/chat",
            json={"query": "why did it fail", "context": {"kind": "journey", "id": "nope"}},
        )
        assert resp.status_code == 200
        assert seen[0]["query"] == "why did it fail"
    finally:
        app.dependency_overrides.clear()


# --- rag_client.ask degradation ----------------------------------------------
async def test_ask_degrades_when_the_ai_service_is_down():
    """A dead AI service yields a well-formed body, never an exception."""
    from backend.rag_client import ask

    class _Boom:
        async def post(self, *a, **kw):
            raise RuntimeError("connection refused")

    result = await ask("q", k=4, client=_Boom())
    assert result["mode"] == "retrieval-only"
    assert result["sources"] == []
    assert "unavailable" in result["answer"].lower()
    # An unreachable service retrieved nothing, so nothing was truncated — that
    # must not be reported as a capped result set.
    assert result["coverage"] == {"shown": 0, "limit": 4, "truncated": False}


# =============================================================================
# backend: incident scope (the "ask about this incident" button)
# =============================================================================
def _ts(offset_seconds: int) -> datetime:
    """A tz-aware timestamp, `offset_seconds` after a fixed base."""
    return datetime(2026, 7, 27, 16, 24, 2, tzinfo=_tz.utc) + timedelta(seconds=offset_seconds)


class _Al:
    """A minimal Alert stand-in for grouping/formatting."""

    def __init__(self, alert_id, journey_id, level, message, emitted_at,
                 order_id=None, event_id=None, explanation=None,
                 app_name="cc-outbound-osw", logger="SapRfcClient"):
        self.alert_id = alert_id
        self.journey_id = journey_id
        self.level = level
        self.message = message
        self.emitted_at = emitted_at
        self.order_id = order_id
        self.event_id = event_id
        self.explanation = explanation
        self.app_name = app_name
        self.logger = logger


class _Jn:
    """A minimal Journey stand-in (only the fields the formatter reads)."""

    def __init__(self, journey_id, outcome, first_ts, last_ts):
        self.journey_id = journey_id
        self.outcome = outcome
        self.first_ts = first_ts
        self.last_ts = last_ts


class _Inc:
    """A minimal Incident stand-in."""

    def __init__(self, title="SAP_SUBMISSION_FAILED — SAP", status="open",
                 department="backend", failure_subtype="SAP_SUBMISSION_FAILED",
                 failing_service="SAP", error_token="RFC_COMMUNICATION_FAILURE",
                 alert_count=999, journey_count=999):
        self.incident_id = "INC-1"
        self.title = title
        self.status = status
        self.department = department
        self.failure_subtype = failure_subtype
        self.failing_service = failing_service
        self.error_token = error_token
        # Deliberately absurd: the formatter must never read these.
        self.alert_count = alert_count
        self.journey_count = journey_count
        # Clustering timestamps — must never reach the model.
        self.first_ts = _ts(600)
        self.last_ts = _ts(900)


def test_incident_groups_one_per_order_ordered_by_recency():
    from backend.api import _incident_order_groups

    alerts = [
        _Al("a1", "J1", "ERROR", "rfc failed", _ts(10), order_id="ORD-1"),
        _Al("a2", "J2", "ERROR", "rfc failed", _ts(50), order_id="ORD-2"),
    ]
    journeys = [
        _Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(10)),
        _Jn("J2", "SAP_SUBMISSION_FAILED", _ts(40), _ts(50)),
    ]
    groups = _incident_order_groups(alerts, journeys)
    assert [g["label"] for g in groups] == ["ORD-2", "ORD-1"]  # newest first
    assert groups[0]["outcome"] == "SAP_SUBMISSION_FAILED"


def test_incident_group_representative_is_the_last_error_not_the_last_alert():
    """Mirrors dashboard/lib/incidents.ts::_pickOutcome — ERROR outranks a later
    WARN, because emitted_at is PROCESSING time and a WARN can finish last."""
    from backend.api import _incident_order_groups

    alerts = [
        _Al("a1", "J1", "ERROR", "the real failure", _ts(10), order_id="ORD-1"),
        _Al("a2", "J1", "WARN", "benign noise", _ts(20), order_id="ORD-1"),
    ]
    groups = _incident_order_groups(alerts, [_Jn("J1", "X", _ts(0), _ts(20))])
    assert len(groups) == 1
    assert groups[0]["alert"].message == "the real failure"


def test_incident_group_label_falls_back_to_event_id_then_alert_id():
    """A pre-creation failure never gets an order id (correlation model)."""
    from backend.api import _incident_order_groups

    groups = _incident_order_groups(
        [_Al("a1", "J1", "ERROR", "transform failed", _ts(10), event_id="evt-abc")],
        [_Jn("J1", "INBOUND_TRANSFORM_FAILED", _ts(0), _ts(10))],
    )
    assert groups[0]["label"] == "evt-abc"

    groups = _incident_order_groups(
        [_Al("a2", "J2", "ERROR", "mystery", _ts(10))], []
    )
    assert groups[0]["label"] == "a2"


def test_incident_alert_without_a_journey_becomes_its_own_group():
    """Distinct degenerate case from the label fallbacks: an alert may be
    clustered before it is linked to a journey. It must not be dropped."""
    from backend.api import _incident_order_groups

    groups = _incident_order_groups(
        [
            _Al("a1", "J1", "ERROR", "grouped", _ts(10), order_id="ORD-1"),
            _Al("a2", None, "ERROR", "orphan", _ts(20), order_id="ORD-2"),
        ],
        [_Jn("J1", "X", _ts(0), _ts(10))],
    )
    assert len(groups) == 2
    assert "orphan" in [g["alert"].message for g in groups]


def test_incident_context_header_counts_come_from_rows_not_counters():
    """The stored counters drift as orders join; a number in an agent-facing
    answer must be one we just counted."""
    from backend.api import format_incident_context

    alerts = [
        _Al("a1", "J1", "ERROR", "rfc failed", _ts(10), order_id="ORD-1"),
        _Al("a2", "J1", "ERROR", "rfc failed again", _ts(20), order_id="ORD-1"),
        _Al("a3", "J2", "ERROR", "rfc failed", _ts(30), order_id="ORD-2"),
    ]
    journeys = [
        _Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(20)),
        _Jn("J2", "SAP_SUBMISSION_FAILED", _ts(25), _ts(30)),
    ]
    out = format_incident_context(_Inc(), alerts, journeys)
    assert "orders=2" in out
    assert "alerts=3" in out
    assert "999" not in out          # neither stored counter leaked


def test_incident_context_never_exposes_clustering_timestamps():
    """Incident.first_ts/last_ts are when the ENGINE noticed, not when anything
    failed. The model quotes what it is given, so they are not given."""
    from backend.api import format_incident_context

    incident = _Inc()
    out = format_incident_context(
        incident,
        [_Al("a1", "J1", "ERROR", "rfc failed", _ts(10), order_id="ORD-1")],
        [_Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(10))],
    )
    assert "clustered" not in out.lower()
    # The clustering stamps are at +600s/+900s (16:34:02 / 16:39:02); the real
    # window is +0s..+10s. Neither clustering minute may appear.
    assert "16:34:02" not in out
    assert "16:39:02" not in out


def test_incident_context_window_and_span_come_from_the_journeys():
    from backend.api import format_incident_context

    journeys = [
        _Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(10)),
        _Jn("J2", "SAP_SUBMISSION_FAILED", _ts(20), _ts(466)),   # +7m46s
    ]
    alerts = [
        _Al("a1", "J1", "ERROR", "rfc failed", _ts(10), order_id="ORD-1"),
        _Al("a2", "J2", "ERROR", "rfc failed", _ts(466), order_id="ORD-2"),
    ]
    out = format_incident_context(_Inc(), alerts, journeys)
    assert "16:24:02" in out          # MIN(first_ts)
    assert "16:31:48" in out          # MAX(last_ts)
    assert "span 7m 46s" in out       # precomputed, not left to the model


def test_incident_context_omits_the_window_when_no_journey_timestamps():
    from backend.api import format_incident_context

    out = format_incident_context(
        _Inc(), [_Al("a1", None, "ERROR", "orphan", _ts(10))], []
    )
    assert "failures from" not in out
    assert "span" not in out
    assert "orders=1" in out          # still useful


def test_incident_context_caps_orders_and_says_so():
    from backend.api import _CONTEXT_ORDER_CAP, format_incident_context

    total = _CONTEXT_ORDER_CAP + 6
    alerts = [
        _Al(f"a{i}", f"J{i}", "ERROR", f"boom {i}", _ts(i), order_id=f"ORD-{i}")
        for i in range(total)
    ]
    journeys = [_Jn(f"J{i}", "SAP_SUBMISSION_FAILED", _ts(i), _ts(i)) for i in range(total)]
    out = format_incident_context(_Inc(), alerts, journeys)
    assert f"Affected orders ({total}, showing {_CONTEXT_ORDER_CAP})" in out
    # One line per shown order, and the freshest survive the cut.
    assert f"ORD-{total - 1}" in out
    assert "ORD-0" not in out


def test_incident_context_untruncated_reports_a_plain_count():
    from backend.api import format_incident_context

    out = format_incident_context(
        _Inc(),
        [_Al("a1", "J1", "ERROR", "rfc failed", _ts(10), order_id="ORD-1")],
        [_Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(10))],
    )
    assert "Affected orders (1)" in out
    assert "showing" not in out


def test_incident_context_line_carries_outcome_service_and_explanation():
    from backend.api import format_incident_context

    out = format_incident_context(
        _Inc(),
        [_Al("a1", "J1", "ERROR", "RFC_COMMUNICATION_FAILURE on submit", _ts(10),
             order_id="ORD-1", explanation="SAP could not be reached.")],
        [_Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(10))],
    )
    assert "ORD-1 (SAP_SUBMISSION_FAILED)" in out
    assert "ERROR cc-outbound-osw SapRfcClient" in out
    assert "RFC_COMMUNICATION_FAILURE on submit" in out
    assert "SAP could not be reached." in out


def test_incident_context_states_that_one_line_is_shown_per_order():
    """Without this the model reports "2 alerts" because it counted 2 lines."""
    from backend.api import format_incident_context

    alerts = [
        _Al("a1", "J1", "ERROR", "x", _ts(10), order_id="ORD-1"),
        _Al("a2", "J1", "ERROR", "y", _ts(20), order_id="ORD-1"),
        _Al("a3", "J2", "ERROR", "z", _ts(30), order_id="ORD-2"),
    ]
    journeys = [
        _Jn("J1", "X", _ts(0), _ts(20)), _Jn("J2", "X", _ts(25), _ts(30)),
    ]
    out = format_incident_context(_Inc(), alerts, journeys)
    assert "one representative line shown per order" in out


def test_incident_context_renders_in_the_requested_timezone():
    from backend.api import format_incident_context

    out = format_incident_context(
        _Inc(),
        [_Al("a1", "J1", "ERROR", "x", _ts(10), order_id="ORD-1")],
        [_Jn("J1", "X", _ts(0), _ts(10))],
        "Europe/Bucharest",
    )
    assert "19:24:02 EEST" in out


class _SeqSession:
    """Returns queued results in order — the incident branch runs 3 queries.

    The existing _FakeSession answers every execute() with the same row, which
    cannot represent "incident, then its alerts, then its journeys". An exhausted
    queue yields an empty result, which is what the chat route's later
    feedback-boosts query gets (and boosts_from([]) is {}).
    """

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._results.pop(0) if self._results else _FakeResult(None)

    async def commit(self):
        pass


def _incident_chat(monkeypatch, results, query="how bad is this", incident_id="INC-1"):
    """POST /chat scoped to an incident; returns the query forwarded to the AI."""
    from backend.auth import get_current_user
    from backend.db import get_session
    from backend.main import app

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async def _session_override():
        yield _SeqSession(results)

    app.dependency_overrides[get_session] = _session_override

    seen: list[dict] = []

    async def _fake_ask(query, k=5, filters=None, boosts=None, client=None):
        seen.append({"query": query, "filters": filters})
        return {"answer": "a", "sources": [], "mode": "ai"}

    monkeypatch.setattr("backend.rag_client.ask", _fake_ask)
    try:
        resp = TestClient(app).post(
            "/chat",
            json={"query": query, "context": {"kind": "incident", "id": incident_id}},
        )
        assert resp.status_code == 200
        return seen[0]
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_incident_context_is_prepended(monkeypatch):
    """The "ask about this incident" button: the incident's membership is
    prepended to the question."""
    alerts = [
        _Al("a1", "J1", "ERROR", "RFC_COMMUNICATION_FAILURE", _ts(10), order_id="ORD-1"),
        _Al("a2", "J2", "ERROR", "RFC_COMMUNICATION_FAILURE", _ts(20), order_id="ORD-2"),
    ]
    journeys = [
        _Jn("J1", "SAP_SUBMISSION_FAILED", _ts(0), _ts(10)),
        _Jn("J2", "SAP_SUBMISSION_FAILED", _ts(15), _ts(20)),
    ]
    call = _incident_chat(
        monkeypatch,
        [_FakeResult(_Inc()), _FakeResult(None, alerts), _FakeResult(None, journeys)],
    )
    forwarded = call["query"]
    assert "SAP_SUBMISSION_FAILED — SAP" in forwarded   # the incident title
    assert "orders=2" in forwarded                       # exact membership
    assert "ORD-1" in forwarded and "ORD-2" in forwarded
    assert "how bad is this" in forwarded                # ...and the question
    assert forwarded.index("orders=2") < forwarded.index("how bad is this")


def test_backend_chat_incident_scope_sends_no_filters(monkeypatch):
    """An anchored conversation must not ALSO be narrowed by an order id lifted
    out of the prose — the scope already anchors it."""
    call = _incident_chat(
        monkeypatch,
        [_FakeResult(_Inc()), _FakeResult(None, []), _FakeResult(None, [])],
        query="what about ORD-6001",
    )
    assert call["filters"] is None


def test_backend_chat_unknown_incident_degrades_to_the_bare_question(monkeypatch):
    """A stale id must not 404 the chat — the answer is merely unscoped."""
    call = _incident_chat(monkeypatch, [_FakeResult(None)])
    assert call["query"] == "how bad is this"


def test_backend_chat_unknown_kind_degrades_to_the_bare_question(monkeypatch):
    """Regression guard on _context_text's fall-through: kind is a plain str, so
    an unrecognized value must be unscoped rather than an error."""
    from backend.auth import get_current_user
    from backend.db import get_session
    from backend.main import app

    app.dependency_overrides[get_current_user] = lambda: "test-user"

    async def _session_override():
        yield _SeqSession([])

    app.dependency_overrides[get_session] = _session_override

    seen: list[str] = []

    async def _fake_ask(query, k=5, filters=None, boosts=None, client=None):
        seen.append(query)
        return {"answer": "a", "sources": [], "mode": "ai"}

    monkeypatch.setattr("backend.rag_client.ask", _fake_ask)
    try:
        resp = TestClient(app).post(
            "/chat",
            json={"query": "hi", "context": {"kind": "nonsense", "id": "X"}},
        )
        assert resp.status_code == 200
        assert seen[0] == "hi"
    finally:
        app.dependency_overrides.clear()


def test_backend_chat_incident_with_no_alerts_still_gives_a_header(monkeypatch):
    """Possible briefly, before retry_unclustered_completions catches up."""
    call = _incident_chat(
        monkeypatch,
        [_FakeResult(_Inc()), _FakeResult(None, []), _FakeResult(None, [])],
    )
    assert "SAP_SUBMISSION_FAILED — SAP" in call["query"]
    assert "orders=0" in call["query"]
