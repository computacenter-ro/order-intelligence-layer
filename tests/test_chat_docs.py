"""POST /chat answers from BOTH channels: incident history and documentation.

The two are retrieved from separate indexes and stay separate all the way into
the prompt. That separation is the point of a second index (rag-plan.md D1) and
of the labelled prompt blocks (§6.1): with one shared top-k, "what does the
checker do?" loses its own documentation to five checker *failures*; and under one
"incident records:" heading the model counted documentation chunks and reported
them as incidents.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_service import api, docsindex, nodes, ragindex
from ai_service.breaker import CircuitBreaker
from ai_service.ragindex import RagDeps, RagIndex


class _FakeChat:
    def __init__(self):
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[-1].content)
        return type("R", (), {"content": "answer [jam-ws#what-this-service-does]"})()


class _FakeRedis:
    async def hgetall(self, *_a, **_kw):
        return {}

    async def hset(self, *_a, **_kw):
        return 1

    async def set(self, *_a, **_kw):
        return True

    async def get(self, *_a, **_kw):
        return None


class _FakeEncoder:
    def encode(self, text: str):
        vector = [0.0] * 26
        for char in text.lower():
            if "a" <= char <= "z":
                vector[ord(char) - 97] += 1.0
        return vector


DOC_HIT = {
    "id": "jam-ws#what-this-service-does",
    "kind": "doc",
    "text": "[jam-ws · 1. What this service does]\njam-ws answers one question: "
    "what is this user allowed to do?",
    "score": 0.71,
    "metadata": {"service": "jam-ws", "kind": "what_it_does", "heading": "1. What this service does"},
}
ALERT_HIT = {
    "id": "alert-1",
    "kind": "alert",
    "text": "ERROR cc-jam-service 403 account disabled",
    "score": 0.66,
    "metadata": {"kind": "alert", "app_name": "cc-jam-service"},
}


@pytest.fixture
def wired(monkeypatch):
    """Empty real indexes; each test injects the hits it wants."""
    ragindex.configure(
        RagDeps(index=RagIndex(None, min_score=0.3, max_entries=10), redis=None, dump_key="k")
    )
    chat = _FakeChat()
    api.configure(api.SummaryDeps(breaker=CircuitBreaker(_FakeRedis()), model=None, chat=chat))
    with TestClient(api.app) as client:
        client.fake_chat = chat  # type: ignore[attr-defined]
        yield client
    api.configure(None)
    ragindex.configure(None)
    docsindex.configure(None)


def _serve(monkeypatch, *, alerts=(), docs=()):
    monkeypatch.setattr(ragindex, "retrieve", lambda *a, **kw: list(alerts))
    monkeypatch.setattr(docsindex, "retrieve", lambda *a, **kw: list(docs))
    monkeypatch.setattr(api.ragindex, "retrieve", lambda *a, **kw: list(alerts))
    monkeypatch.setattr(api.docsindex, "retrieve", lambda *a, **kw: list(docs))


# --- the prompt keeps the channels apart --------------------------------------
def test_prompt_labels_the_two_channels_separately():
    prompt = nodes.build_chat_prompt("what does JAM do?", [ALERT_HIT], [DOC_HIT])
    assert "related incident history (things that happened):" in prompt
    assert "system documentation (how the system works):" in prompt
    # The documentation must come AFTER its own heading, not under the incident one.
    assert prompt.index("system documentation") < prompt.index("jam-ws answers one question")


def test_prompt_omits_a_channel_that_returned_nothing():
    only_docs = nodes.build_chat_prompt("q", [], [DOC_HIT])
    assert "system documentation" in only_docs
    assert "related incident history" not in only_docs

    only_alerts = nodes.build_chat_prompt("q", [ALERT_HIT], [])
    assert "related incident history" in only_alerts
    assert "system documentation" not in only_alerts


def test_prompt_does_not_repeat_the_chunk_header():
    """The chunk text already opens with "[service · heading]" — paying for it
    twice in every prompt is pure token waste."""
    prompt = nodes.build_chat_prompt("q", [], [DOC_HIT])
    assert prompt.count("1. What this service does") == 1


def test_system_prompt_distinguishes_documentation_from_evidence():
    assert "NOT evidence that anything happened" in nodes._CHAT_SYSTEM


# --- the endpoint -------------------------------------------------------------
def test_docs_alone_are_enough_to_compose_an_answer(wired, monkeypatch):
    """A pure "how does it work" question needs zero incident records."""
    _serve(monkeypatch, alerts=[], docs=[DOC_HIT])
    body = wired.post("/chat", json={"query": "what does JAM do?"}).json()
    assert body["mode"] == "ai"
    assert len(wired.fake_chat.prompts) == 1
    assert "system documentation" in wired.fake_chat.prompts[0]


def test_doc_chunks_are_returned_as_citable_sources(wired, monkeypatch):
    _serve(monkeypatch, alerts=[ALERT_HIT], docs=[DOC_HIT])
    body = wired.post("/chat", json={"query": "what does JAM do?"}).json()
    kinds = [s["kind"] for s in body["sources"]]
    assert kinds == ["alert", "doc"]
    doc = body["sources"][-1]
    assert doc["id"] == "jam-ws#what-this-service-does"
    assert doc["metadata"]["service"] == "jam-ws"


def test_coverage_still_describes_incident_history_only(wired, monkeypatch):
    """Docs must not inflate "truncated" — it answers a question about HISTORY."""
    _serve(monkeypatch, alerts=[], docs=[DOC_HIT, DOC_HIT, DOC_HIT, DOC_HIT])
    body = wired.post("/chat", json={"query": "what does JAM do?", "k": 5}).json()
    assert body["coverage"] == {"shown": 0, "limit": 5, "truncated": False}


def test_nothing_anywhere_still_refuses(wired, monkeypatch):
    _serve(monkeypatch, alerts=[], docs=[])
    body = wired.post("/chat", json={"query": "what happened?"}).json()
    assert body["mode"] == "retrieval-only"
    assert "No related incidents found" in body["answer"]
    assert wired.fake_chat.prompts == []


def test_incident_filters_are_not_applied_to_the_docs_channel(wired, monkeypatch):
    """`filters` name incident columns a doc chunk does not carry — applying
    them would silently empty this channel."""
    seen: dict = {}

    def _docs_retrieve(query, k=None, **kw):
        seen.update(kw)
        return [DOC_HIT]

    monkeypatch.setattr(api.ragindex, "retrieve", lambda *a, **kw: [])
    monkeypatch.setattr(api.docsindex, "retrieve", _docs_retrieve)
    wired.post("/chat", json={"query": "what does JAM do?", "filters": {"department": "backend"}})
    assert "filters" not in seen


# --- the LLM-down path --------------------------------------------------------
def test_retrieval_only_answer_lists_docs_under_their_own_heading():
    answer = api.build_retrieval_answer("q", [ALERT_HIT], [DOC_HIT])
    assert "Related system documentation (1 section(s))" in answer
    assert "jam-ws#what-this-service-does" in answer
    # The incident count must not absorb the doc chunks.
    assert "Found 1 related incident(s)" in answer


def test_retrieval_only_answer_with_docs_but_no_incidents():
    answer = api.build_retrieval_answer("what does JAM do?", [], [DOC_HIT])
    assert "No related incidents found" in answer
    assert "documentation has 1 relevant section(s)" in answer
    assert "jam-ws#what-this-service-does" in answer


def test_retrieval_only_answer_with_nothing_is_unchanged():
    assert api.build_retrieval_answer("q", [], []) == api.NO_RESULTS_ANSWER
    assert api.build_retrieval_answer("q", []) == api.NO_RESULTS_ANSWER


# --- no record id ever reaches the reader -------------------------------------
def test_doc_citation_becomes_a_plain_phrase():
    """Substituted, not deleted: the phrase carries meaning (this came from
    documentation, not from log evidence) and keeps the sentence intact."""
    answer = api.clean_answer_citations(
        "JAM returns privileges [jam-ws#what-this-service-does].",
        ["jam-ws#what-this-service-does"],
    )
    assert answer == "JAM returns privileges the official documentation."


def test_doc_citation_is_substituted_not_deleted():
    """Deleting would leave "as described in ." — the sentence must survive."""
    answer = api.clean_answer_citations(
        "as described in [jam-ws#escalation]", ["jam-ws#escalation"]
    )
    assert answer == "as described in the official documentation"


def test_repeated_doc_citations_collapse_to_one_mention():
    answer = api.clean_answer_citations(
        "See [a#one], [a#two] and [a#three].", ["a#one", "a#two", "a#three"]
    )
    assert answer == "See the official documentation."


def test_alert_and_journey_ids_are_deleted():
    """Internal uuids of rows the reader cannot look up by id — pure noise."""
    answer = api.clean_answer_citations(
        "The margin check blocked the order. [4d6e9f08-d1e2-4d40-b1f5-8e9d3668dd1d]",
        [],
        ["4d6e9f08-d1e2-4d40-b1f5-8e9d3668dd1d"],
    )
    assert answer == "The margin check blocked the order."


def test_a_dangling_evidence_list_is_removed_whole():
    """"Evidence: [a], [b]." must not become "Evidence: , .""" ""
    answer = api.clean_answer_citations(
        "Submission failed at SAP. Evidence: [alert-1], [journey-2].",
        [],
        ["alert-1", "journey-2"],
    )
    assert answer == "Submission failed at SAP."


def test_mixed_doc_and_record_citations():
    answer = api.clean_answer_citations(
        "It is a business rejection. Evidence: [alert-1], [jam-ws#escalation].",
        ["jam-ws#escalation"],
        ["alert-1"],
    )
    assert "alert-1" not in answer
    assert "jam-ws#escalation" not in answer
    assert api.DOC_CITATION_TEXT in answer
    assert "Evidence: ," not in answer


def test_business_identifiers_are_never_touched():
    """ORD-6426 is what the agent actually works with — the point of the answer.

    Only ids of RETRIEVED RECORDS are removed, and only inside brackets, so an
    order number cannot be caught by this even when it appears in brackets.
    """
    text = (
        "Order ORD-6426 was blocked: margin 4.97% below the 15% threshold "
        "(event evt-372656a7, cart header 1840927365018240001). [alert-1]"
    )
    answer = api.clean_answer_citations(text, [], ["alert-1"])
    assert "ORD-6426" in answer
    assert "evt-372656a7" in answer
    assert "1840927365018240001" in answer
    assert "alert-1" not in answer


def test_an_unretrieved_bracketed_token_is_left_alone():
    """Only known source ids are removed — this never guesses at brackets."""
    answer = api.clean_answer_citations("See note [3] in the runbook.", [], ["alert-1"])
    assert answer == "See note [3] in the runbook."


def test_nothing_to_remove_leaves_the_answer_untouched():
    assert api.clean_answer_citations("Plain answer.", [], []) == "Plain answer."


def test_endpoint_strips_ids_from_the_composed_answer(wired, monkeypatch):
    _serve(monkeypatch, alerts=[], docs=[DOC_HIT])
    body = wired.post("/chat", json={"query": "what does JAM do?"}).json()
    # The fake model replies with exactly the citation the prompt asks it to avoid.
    assert "[jam-ws#what-this-service-does]" not in body["answer"]
    assert api.DOC_CITATION_TEXT in body["answer"]
    # ...but the source is still RETURNED, so the UI can show its chip and the id
    # stays available for the evaluation set and network-tab debugging.
    assert [s["id"] for s in body["sources"]] == ["jam-ws#what-this-service-does"]


def test_prompt_forbids_bracketed_citations():
    assert "no square-bracket citations of any kind" in nodes._CHAT_SYSTEM


def test_prompt_still_allows_business_identifiers():
    """Removing ORD-6426 from answers would gut them."""
    assert "ORD-6426" in nodes._CHAT_SYSTEM


async def test_composer_accepts_docs_with_no_incident_sources():
    answer = await nodes.compose_chat_answer("q", [], _FakeChat(), docs=[DOC_HIT])
    assert answer.startswith("answer")


async def test_composer_still_refuses_when_both_channels_are_empty():
    with pytest.raises(nodes.LLMError, match="no sources"):
        await nodes.compose_chat_answer("q", [], _FakeChat(), docs=[])
