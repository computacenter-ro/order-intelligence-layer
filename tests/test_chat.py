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

    async def _fake_ask(query, k=5, filters=None, client=None):
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
    def __init__(self, one):
        self._one = one

    def scalar_one_or_none(self):
        return self._one


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

    async def _fake_ask(query, k=5, filters=None, client=None):
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

    async def _fake_ask(query, k=5, filters=None, client=None):
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
