"""Tests for the AI-service retrieval index [3] and the retrieval-only /chat.

No LLM, no model download, no network: the deterministic ``FakeEncoder`` from
``tests/test_semcache.py`` is reused (one fake encoder for both embedding paths,
mirroring the fact that the real service shares one model), and persistence is
exercised against ``FakeRedis``.

What is asserted here:
  * ranking — retrieve returns matches ordered by cosine, best first;
  * the recall floor — a query below RAGINDEX_MIN_SCORE returns nothing;
  * metadata filters narrow results, and filtering on an absent key excludes;
  * dump/load round-trips through Redis (the index survives a restart);
  * upsert-by-id — re-indexing replaces, which is what makes backfill idempotent;
  * self-disabling — no encoder => retrieval is empty, never an exception;
  * /index and /chat over the FastAPI TestClient with the fakes injected;
  * /chat is deterministic and LLM-free, and says so plainly when nothing matches.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ai_service import api, ragindex
from ai_service.ragindex import NEUTRAL_BOOST, RagDeps, RagIndex

# Reuse the deterministic fakes — no duplicate test scaffolding.
from tests.test_ai_service import FakeRedis
from tests.test_semcache import FakeEncoder

# Three records with deliberately disjoint vocabulary so the bag-of-tokens
# FakeEncoder gives them clearly different cosines against each query below.
MARGIN = "cc-checker-service ERROR margin check failed below threshold for order"
SAP = "cc-outbound-osw ERROR sap rfc communication failure partner not reached"
JOURNEY = "Journey SAP_SUBMISSION_FAILED: the order reached outbound and failed sap submission"


@pytest.fixture
def index() -> RagIndex:
    """A populated, enabled index (fake encoder, permissive floor)."""
    idx = RagIndex(FakeEncoder(), min_score=0.30, max_entries=100)
    idx.index("a1", "alert", MARGIN, {"department": "business", "level": "ERROR"})
    idx.index("a2", "alert", SAP, {"department": "networking", "level": "ERROR"})
    idx.index("j1", "journey", JOURNEY, {"outcome": "SAP_SUBMISSION_FAILED"})
    return idx


@pytest.fixture
def install_index():
    """Install module-global RagDeps with a fake redis; auto-teardown."""
    created = []

    def _install(idx: RagIndex | None = None, redis=None):
        idx = idx if idx is not None else RagIndex(FakeEncoder(), min_score=0.30, max_entries=100)
        deps = RagDeps(index=idx, redis=redis, dump_key="test:ragindex")
        ragindex.configure(deps)
        created.append(deps)
        return deps

    yield _install
    ragindex.configure(None)


# =============================================================================
# store + retrieval
# =============================================================================
def test_index_stores_and_reports_size(index):
    assert len(index) == 3
    assert index.enabled is True


def test_retrieve_ranks_by_cosine(index):
    # A margin-flavoured query must put the margin alert first.
    results = index.retrieve("margin check failed below threshold", k=5)
    assert results, "expected at least one match"
    assert results[0]["id"] == "a1"
    # Scores are sorted descending.
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_retrieve_ranks_a_different_query_differently(index):
    # The same index, a SAP-flavoured query: a different record must win, proving
    # the ranking follows the query rather than insertion order.
    results = index.retrieve("sap rfc communication failure partner", k=5)
    assert results[0]["id"] in {"a2", "j1"}
    assert results[0]["id"] != "a1"


def test_retrieve_returns_all_expected_fields(index):
    top = index.retrieve("margin check failed", k=1)[0]
    # final_score/boost were added with feedback-blended ranking; `score` stays the
    # raw cosine so the blend's effect is inspectable rather than invisible.
    assert set(top) == {"id", "kind", "text", "metadata", "score", "final_score", "boost"}
    assert top["kind"] == "alert"
    assert 0.0 <= top["score"] <= 1.0


def test_retrieve_honours_k(index):
    assert len(index.retrieve("order failed error", k=1)) <= 1
    assert len(index.retrieve("order failed error", k=2)) <= 2


def test_below_floor_query_returns_nothing(index):
    """A query sharing no vocabulary scores ~0 and must be dropped by the floor."""
    assert index.retrieve("zebra quilt harpsichord botany", k=5) == []


def test_a_high_floor_suppresses_everything(index):
    strict = RagIndex(FakeEncoder(), min_score=0.999, max_entries=100)
    strict.index("a1", "alert", MARGIN, {})
    # Only a near-identical query could clear 0.999; a partial one must not.
    assert strict.retrieve("margin", k=5) == []


def test_retrieval_floor_is_looser_than_the_cache_threshold():
    """Regression guard on intent: retrieval wants recall, the cache wants
    precision. If someone ever "harmonizes" these two numbers, this fails."""
    from ai_service import settings

    assert settings.RAGINDEX_MIN_SCORE < settings.SEMCACHE_THRESHOLD


# =============================================================================
# feedback-blended ranking
# =============================================================================
def test_zero_weight_is_an_exact_no_op(index):
    """The escape hatch must be provably inert: RAGINDEX_FEEDBACK_WEIGHT=0 has to
    leave ranking bit-for-bit unchanged, or "turn it off" is not a real option."""
    plain = index.retrieve("margin check failed below threshold", k=5)
    boosted = index.retrieve(
        "margin check failed below threshold", k=5,
        boosts={"a1": 1.0, "a2": 0.0}, feedback_weight=0.0,
    )
    assert [r["id"] for r in plain] == [r["id"] for r in boosted]
    assert all(r["score"] == r["final_score"] for r in boosted)


def test_feedback_cannot_surface_an_irrelevant_record(index):
    """THE safety property. The relevance floor is applied to raw cosine BEFORE the
    blend, so no amount of likes can put an unrelated incident in front of an
    agent. Without this, a brigade of votes could break search entirely."""
    index.index("offtopic", "alert", "completely unrelated bread baking recipe", {})
    results = index.retrieve(
        "sap rfc communication failure partner", k=10,
        boosts={"offtopic": 1.0}, feedback_weight=0.15,
    )
    assert all(r["id"] != "offtopic" for r in results)


def test_an_unvoted_record_is_not_penalised(index):
    """A record missing from `boosts` must score NEUTRAL, not zero — most records
    are never voted on, and treating silence as bad would tax them all."""
    results = index.retrieve(
        "margin check failed below threshold", k=5,
        boosts={}, feedback_weight=0.15,
    )
    assert results
    assert all(r["boost"] == NEUTRAL_BOOST for r in results)


def test_a_boost_can_reorder_close_matches(index):
    """The point of the feature: among records that BOTH cleared the floor, feedback
    decides order."""
    idx = RagIndex(FakeEncoder(), min_score=0.0, max_entries=10)
    # Near-identical text so cosine is close and the boost is decisive.
    idx.index("liked", "alert", "sap rfc failure partner not reached", {})
    idx.index("plain", "alert", "sap rfc failure partner unreachable", {})
    q = "sap rfc failure partner"
    before = [r["id"] for r in idx.retrieve(q, k=2)]
    after = [
        r["id"]
        for r in idx.retrieve(q, k=2, boosts={"liked": 1.0}, feedback_weight=0.5)
    ]
    assert before != after or before[0] == "liked"
    assert after[0] == "liked"


def test_the_blend_formula_is_applied_as_documented(index):
    results = index.retrieve(
        "margin check failed below threshold", k=1,
        boosts={"a1": 1.0}, feedback_weight=0.2,
    )
    top = results[0]
    assert top["final_score"] == pytest.approx(top["score"] * 0.8 + 1.0 * 0.2)


def test_weight_is_clamped_to_a_sane_range(index):
    """A misconfigured weight must not invert or explode the ranking."""
    for weight in (-5.0, 5.0):
        results = index.retrieve(
            "margin check failed below threshold", k=5,
            boosts={"a1": 1.0}, feedback_weight=weight,
        )
        assert all(0.0 <= r["final_score"] <= 1.0 for r in results)


def test_raw_score_is_still_reported_alongside_the_blend(index):
    """Both numbers are returned so the effect is inspectable rather than invisible."""
    top = index.retrieve(
        "margin check failed below threshold", k=1,
        boosts={"a1": 0.9}, feedback_weight=0.15,
    )[0]
    assert "score" in top and "final_score" in top and "boost" in top
    assert top["final_score"] != top["score"]  # the blend did something


# =============================================================================
# metadata filters
# =============================================================================
def test_filter_narrows_results(index):
    # The query must clear the recall floor on its own; the filter then narrows
    # WITHIN the matches. (A vague query like "error failed" legitimately scores
    # below RAGINDEX_MIN_SCORE, so it would prove nothing about filtering.)
    query = "sap rfc communication failure partner"
    assert {r["id"] for r in index.retrieve(query, k=5)} == {"a2"}
    # Matching filter → kept; non-matching filter on the same query → dropped.
    assert {r["id"] for r in index.retrieve(query, k=5, filters={"department": "networking"})} == {"a2"}
    assert index.retrieve(query, k=5, filters={"department": "business"}) == []


def test_filter_on_kind_style_metadata(index):
    results = index.retrieve("sap submission failed", k=5, filters={"outcome": "SAP_SUBMISSION_FAILED"})
    assert {r["id"] for r in results} == {"j1"}


def test_filter_on_absent_key_excludes_the_record(index):
    # The journey record has no "department", so a department filter must exclude
    # it rather than treating the missing key as a match.
    results = index.retrieve("journey sap submission", k=5, filters={"department": "business"})
    assert all(r["id"] != "j1" for r in results)


def test_multiple_filters_are_conjunctive(index):
    # a1 matches department=business but its level is ERROR, not WARN → excluded.
    query = "margin check failed below threshold"
    assert index.retrieve(query, k=5, filters={"department": "business", "level": "ERROR"})
    assert index.retrieve(query, k=5, filters={"department": "business", "level": "WARN"}) == []


def test_filter_compares_as_strings(index):
    idx = RagIndex(FakeEncoder(), min_score=0.0, max_entries=10)
    idx.index("x", "alert", MARGIN, {"line": 3})
    # JSON persistence can turn 3 into "3"; the filter must match either way.
    assert idx.retrieve("margin", k=5, filters={"line": "3"})
    assert idx.retrieve("margin", k=5, filters={"line": 3})


# =============================================================================
# upsert / capacity / self-disabling
# =============================================================================
def test_index_upserts_by_id_so_backfill_is_idempotent(index):
    before = len(index)
    index.index("a1", "alert", MARGIN + " (re-indexed)", {"department": "business"})
    assert len(index) == before  # replaced, not duplicated
    hit = [r for r in index.retrieve("margin check failed", k=5) if r["id"] == "a1"][0]
    assert "re-indexed" in hit["text"]


def test_capacity_evicts_oldest_first():
    idx = RagIndex(FakeEncoder(), min_score=0.0, max_entries=2)
    idx.index("first", "alert", "alpha alpha", {})
    idx.index("second", "alert", "beta beta", {})
    idx.index("third", "alert", "gamma gamma", {})
    assert len(idx) == 2
    ids = {r["id"] for r in idx.retrieve("alpha beta gamma", k=5)}
    assert "first" not in ids


def test_disabled_index_returns_empty_and_does_not_raise():
    """No encoder (sentence-transformers absent / model load failed) => the index
    disables itself, exactly like the semantic cache."""
    idx = RagIndex(None, min_score=0.30, max_entries=10)
    assert idx.enabled is False
    assert idx.index("a1", "alert", MARGIN, {}) is False
    assert idx.retrieve("margin check failed", k=5) == []


def test_blank_text_is_not_indexed(index):
    assert index.index("blank", "alert", "   ", {}) is False
    assert index.retrieve("", k=5) == []


# =============================================================================
# persistence
# =============================================================================
async def test_dump_load_round_trips_through_redis(index, install_index):
    redis = FakeRedis()
    install_index(index, redis)
    await ragindex.persist()

    # A fresh, empty index restored from the same Redis must retrieve identically.
    restored = RagIndex(FakeEncoder(), min_score=0.30, max_entries=100)
    install_index(restored, redis)
    assert len(restored) == 0
    await ragindex.restore()

    assert len(restored) == 3
    assert restored.retrieve("margin check failed below threshold", k=1)[0]["id"] == "a1"


def test_load_survives_corrupt_payload(index):
    index.load("{not json at all")
    assert len(index) == 3  # unchanged, no exception


def test_load_skips_malformed_records():
    idx = RagIndex(FakeEncoder(), min_score=0.0, max_entries=10)
    idx.load('[{"id": "ok", "kind": "alert", "text": "t", "vector": [1.0], "metadata": {}},'
             ' {"id": "bad", "kind": "alert"}]')
    assert len(idx) == 1


# =============================================================================
# module-level helpers
# =============================================================================
async def test_index_record_persists_after_write(install_index):
    redis = FakeRedis()
    install_index(redis=redis)
    assert await ragindex.index_record("a1", "alert", MARGIN, {"level": "ERROR"}) is True
    assert redis.keys.get("test:ragindex")  # dump written


async def test_unconfigured_module_is_inert():
    ragindex.configure(None)
    assert await ragindex.index_record("a1", "alert", MARGIN) is False
    assert ragindex.retrieve("margin") == []
    assert (await ragindex.stats())["size"] == 0


async def test_stats_reports_size_and_enabled(index, install_index):
    install_index(index, FakeRedis())
    stats = await ragindex.stats()
    assert stats == {"size": 3, "enabled": True}


# =============================================================================
# /index and /chat (FastAPI, retrieval-only)
# =============================================================================
def test_index_endpoint_stores_a_record(install_index):
    install_index(redis=FakeRedis())
    client = TestClient(api.app)
    resp = client.post(
        "/index",
        json={"id": "a1", "kind": "alert", "text": MARGIN, "metadata": {"level": "ERROR"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"indexed": True}
    assert ragindex.retrieve("margin check failed", k=1)[0]["id"] == "a1"


def test_index_endpoint_reports_false_when_disabled(install_index):
    install_index(RagIndex(None, min_score=0.3, max_entries=10), FakeRedis())
    resp = TestClient(api.app).post(
        "/index", json={"id": "a1", "kind": "alert", "text": MARGIN}
    )
    # A disabled index is a 200 with indexed=false — the backend pushes
    # fire-and-forget and must not have to handle an error status.
    assert resp.status_code == 200
    assert resp.json() == {"indexed": False}


def test_chat_returns_sources_and_retrieval_only_mode(index, install_index):
    # No api.configure() here, so there is no chat model: slice 2's composition
    # step is skipped and the phase-1 deterministic answer is served. The
    # LLM-composed path and its fallbacks are covered in tests/test_chat.py.
    api._deps = None
    install_index(index, FakeRedis())
    resp = TestClient(api.app).post(
        "/chat", json={"query": "margin check failed below threshold", "k": 3}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "retrieval-only"
    assert body["sources"], "expected retrieved sources"
    assert body["sources"][0]["id"] == "a1"
    # metadata was added in slice 2 so the backend can build dashboard links
    # from journey_id/order_id without a second lookup.
    assert set(body["sources"][0]) == {"id", "kind", "score", "snippet", "metadata"}
    # The answer is a deterministic template over the sources — it must name them.
    assert "a1" in body["answer"]
    assert "1 related incident" in body["answer"] or "related incident(s)" in body["answer"]


def test_chat_says_so_when_nothing_matches(index, install_index):
    install_index(index, FakeRedis())
    body = TestClient(api.app).post(
        "/chat", json={"query": "zebra quilt harpsichord botany"}
    ).json()
    assert body["sources"] == []
    assert "No related incidents found" in body["answer"]
    assert body["mode"] == "retrieval-only"


def test_chat_applies_filters(index, install_index):
    install_index(index, FakeRedis())
    body = TestClient(api.app).post(
        "/chat",
        json={
            "query": "sap rfc communication failure partner",
            "filters": {"department": "networking"},
        },
    ).json()
    assert [s["id"] for s in body["sources"]] == ["a2"]


def test_chat_is_deterministic(index, install_index):
    install_index(index, FakeRedis())
    client = TestClient(api.app)
    payload = {"query": "sap rfc communication failure", "k": 3}
    first = client.post("/chat", json=payload).json()
    second = client.post("/chat", json=payload).json()
    assert first == second  # no LLM anywhere: byte-identical replies


def test_chat_on_unconfigured_index_is_still_a_200():
    ragindex.configure(None)
    body = TestClient(api.app).post("/chat", json={"query": "anything"}).json()
    assert body["sources"] == []
    assert body["mode"] == "retrieval-only"


def test_ragindex_stats_endpoint(index, install_index):
    install_index(index, FakeRedis())
    assert TestClient(api.app).get("/ragindex/stats").json() == {"size": 3, "enabled": True}


# =============================================================================
# the deterministic answer builder (pure)
# =============================================================================
def test_build_retrieval_answer_counts_kinds():
    answer = api.build_retrieval_answer(
        "why did it fail",
        [
            {"id": "a1", "kind": "alert", "text": MARGIN, "metadata": {}, "score": 0.9},
            {"id": "j1", "kind": "journey", "text": JOURNEY, "metadata": {}, "score": 0.5},
        ],
    )
    assert "Found 2 related incident(s)" in answer
    assert "1 alert(s)" in answer and "1 journey(s)" in answer
    assert "a1" in answer and "j1" in answer


def test_build_retrieval_answer_with_no_results_is_plain():
    assert "No related incidents found" in api.build_retrieval_answer("q", [])


def test_snippet_truncates_long_text():
    long_text = "word " * 200
    snippet = api._snippet(long_text)
    assert len(snippet) <= api.SNIPPET_CHARS + 1  # +1 for the ellipsis
    assert snippet.endswith("…")
