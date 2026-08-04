"""Zero retrieved sources must not mean "refuse to answer" (rag-plan.md §6.4).

The original guard was correct while the index was the ONLY grounding channel:
"retrieval returned nothing" and "there is nothing to answer from" were the same
fact, and composing anyway is exactly when a model invents an incident.

They stopped being the same fact once a caller could supply its own context.
``backend/api.py::build_scoped_query`` prepends the clicked record's text — read
LIVE from Postgres, more authoritative than anything indexed — into ``query``. So
the failure this fixes is:

    click a NOVEL failure (nothing similar is indexed, which is precisely why you
    are asking) -> "what does this mean?" -> "No related incidents found in the
    indexed history for that question. No sources found in official documentation
    either."

...while the alert's full text sits in the very same request.

The guard itself is unchanged — compose only when grounded. "Grounded" is simply
no longer a synonym for "retrieval returned rows".
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_service import api, nodes, ragindex
from ai_service.breaker import CircuitBreaker
from ai_service.ragindex import RagDeps, RagIndex


class _FakeChat:
    """Records what it was asked; returns a fixed grounded-looking answer."""

    def __init__(self, reply: str = "It failed at the margin check. [alert-1]"):
        self.reply = reply
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return type("R", (), {"content": self.reply})()


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    async def hgetall(self, *_a, **_kw):
        return {}

    async def hset(self, *_a, **_kw):
        return 1

    async def set(self, *_a, **_kw):
        return True

    async def get(self, *_a, **_kw):
        return None


@pytest.fixture
def empty_index():
    """An index that retrieves nothing — the situation under test."""
    ragindex.configure(
        RagDeps(index=RagIndex(None, min_score=0.3, max_entries=10), redis=None, dump_key="k")
    )
    yield
    ragindex.configure(None)


@pytest.fixture
def client(empty_index):
    chat = _FakeChat()
    api.configure(
        api.SummaryDeps(breaker=CircuitBreaker(_FakeRedis()), model=None, chat=chat)
    )
    with TestClient(api.app) as c:
        c.fake_chat = chat  # type: ignore[attr-defined]
        yield c
    api.configure(None)


# --- the endpoint -------------------------------------------------------------
def test_scoped_question_is_answered_even_with_zero_sources(client):
    """The fix: grounding came in the query, so compose from it."""
    resp = client.post(
        "/chat",
        json={
            "query": "Regarding this incident: ERROR cc-checker-service line "
            "blocked by margin check\n\nQuestion: what does this mean?",
            "self_grounded": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "ai"
    assert "No related incidents found" not in body["answer"]
    assert len(client.fake_chat.calls) == 1  # the model WAS consulted


def test_bare_question_with_zero_sources_still_refuses(client):
    """The guard it was written for must survive: nothing to answer from."""
    resp = client.post("/chat", json={"query": "what happened yesterday?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "retrieval-only"
    assert "No related incidents found" in body["answer"]
    assert client.fake_chat.calls == []  # the model was NOT consulted


def test_self_grounded_defaults_to_false(client):
    """Existing callers must not change behaviour by omitting the field."""
    resp = client.post("/chat", json={"query": "anything"})
    assert resp.json()["mode"] == "retrieval-only"
    assert client.fake_chat.calls == []


def test_sources_are_empty_in_both_cases(client):
    """`sources` stays truthful — self-grounding adds prose, not citations."""
    grounded = client.post("/chat", json={"query": "q", "self_grounded": True}).json()
    bare = client.post("/chat", json={"query": "q"}).json()
    assert grounded["sources"] == [] == bare["sources"]


def test_coverage_is_still_computed_with_no_sources(client):
    body = client.post("/chat", json={"query": "q", "self_grounded": True}).json()
    assert body["coverage"] == {"shown": 0, "limit": 5, "truncated": False}


# --- the composer ------------------------------------------------------------
async def test_composer_still_refuses_empty_sources_by_default():
    with pytest.raises(nodes.LLMError, match="no sources"):
        await nodes.compose_chat_answer("q", [], _FakeChat())


async def test_composer_accepts_empty_sources_when_told_to():
    answer = await nodes.compose_chat_answer(
        "q", [], _FakeChat("grounded answer"), allow_empty_sources=True
    )
    assert answer == "grounded answer"


async def test_composer_with_no_model_still_raises_regardless():
    """A missing model is not something self-grounding can paper over."""
    with pytest.raises(nodes.LLMError, match="no chat model"):
        await nodes.compose_chat_answer("q", [], None, allow_empty_sources=True)


def test_prompt_tells_the_model_to_use_the_question_context():
    """"(no records retrieved)" reads as "refuse" — the opposite of the intent."""
    prompt = nodes.build_chat_prompt("Regarding this incident: X\n\nQuestion: y", [])
    assert "answer from the context given in the question" in prompt
    assert "Regarding this incident: X" in prompt
