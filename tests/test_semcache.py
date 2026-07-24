"""Tests for the AI-service semantic cache [3].

The cache short-circuits the explainer+router LLM calls when a near-identical
(after id-normalization) WARN/ERROR log was already processed. These tests use
a deterministic FAKE encoder and a fake Redis (no model download, no network),
and a call-counting fake chat model to PROVE the LLM is not invoked on a hit.

Design points asserted here (CLAUDE.md [3] "semantic cache"):
  * normalization masks volatile ids so same-type/different-id logs collide;
  * semantically meaningful tokens (retry counters) are NOT masked;
  * lookup is exact-then-cosine with a threshold floor;
  * the explanation is stored normalized and re-filled with the current ids;
  * hit/miss counters increment.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_service import graph, semcache, settings
from ai_service.breaker import CircuitBreaker
from ai_service.graph import PipelineDeps, process
from ai_service.semcache import CachePayload, SemanticCache, SemCacheDeps
from shared.models import Department, LogLine, Severity

# Reuse the fakes from the main AI-service test module.
from tests.test_ai_service import FakeClock, FakeRedis, _breaker


# --- a call-counting chat model (proves the LLM is/ isn't invoked) -----------
class CountingModel:
    """A chat model that records how many times it was invoked."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        self.calls += 1
        return AIMessage(content=self.reply)


# --- a deterministic fake encoder (no model download) ------------------------
class FakeEncoder:
    """Maps text → a deterministic hashed bag-of-tokens vector.

    Identical text → identical vector (cosine 1.0). Every distinct token gets
    its own hashed dimension, so a single meaningful token difference (e.g.
    ``2/3`` vs ``3/3``) measurably lowers cosine — modelling a real embedding's
    ability to keep those apart, which is what the ``SEMCACHE_THRESHOLD`` floor
    relies on. No fixed vocabulary, no model download, fully deterministic.
    """

    _DIMS = 64

    def encode(self, text: str):
        vec = [0.0] * self._DIMS
        tokens = text.lower().replace(":", " ").replace(";", " ").replace("(", " ").replace(")", " ").split()
        for tok in tokens:
            vec[hash_token(tok) % self._DIMS] += 1.0
        return vec


def hash_token(tok: str) -> int:
    """A small stable non-cryptographic hash (independent of PYTHONHASHSEED)."""
    h = 2166136261
    for ch in tok:
        h = (h ^ ord(ch)) * 16777619 % (2 ** 32)
    return h


def _log(
    *,
    level: str = "ERROR",
    message: str,
    order_id: str | None = "ORD-6001",
    cart: str | None = "1840927365018240001",
    event_id: str | None = None,
    log_id: str = "log-1",
) -> LogLine:
    return LogLine(
        log_id=log_id,
        timestamp=datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc),
        app_name="cc-spt-service",
        level=level,
        logger="c.c.spt.service.PriceListService",
        host="CCECMSRVT001",
        process_id="6340",
        thread="http-nio-8080-exec-8",
        eventId=event_id,
        orderId=order_id,
        cartHeaderId=cart,
        message=message,
    )


@pytest.fixture
def install_cache():
    """Install a semantic cache with a fake encoder + fake redis; auto-teardown.

    Returns (redis, cache) and cleans up the module-global deps afterward so no
    state leaks into the plain pipeline tests (which expect no cache).
    """
    installed = []

    def _install(threshold: float = 0.95, max_entries: int = 500):
        redis = FakeRedis()
        # FakeRedis has no incr/get-int semantics for counters; extend it here.
        _add_counter_support(redis)
        cache = SemanticCache(FakeEncoder(), threshold=threshold, max_entries=max_entries)
        semcache.configure(
            SemCacheDeps(
                cache=cache,
                redis=redis,
                dump_key="test:semcache",
                hits_key="test:semcache:hits",
                misses_key="test:semcache:misses",
            )
        )
        installed.append(True)
        return redis, cache

    yield _install
    semcache.configure(None)  # type: ignore[arg-type]
    semcache._deps = None


def _add_counter_support(redis: FakeRedis) -> None:
    """Teach FakeRedis the incr used by the hit/miss counters."""

    async def incr(key: str):
        current = int(redis.keys.get(key, 0))
        redis.keys[key] = current + 1
        return current + 1

    redis.incr = incr  # type: ignore[attr-defined]


def _healthy_deps(explainer, router) -> PipelineDeps:
    return PipelineDeps(
        breaker=_breaker(FakeRedis(), FakeClock()), explainer=explainer, router=router
    )


# =============================================================================
# normalize() — the core trick
# =============================================================================
def test_normalize_masks_volatile_ids():
    n = semcache.normalize(
        "Generated order number ORD-6001 for cart header 1840927365018240001"
    )
    assert "ORD-6001" not in n and "1840927365018240001" not in n
    assert "<ORD>" in n and "<CART>" in n


def test_normalize_masks_event_id_and_account():
    n = semcache.normalize(
        "Received inbound order event evt-372656a7-abcd-1234 for account 81036533"
    )
    assert "evt-372656a7-abcd-1234" not in n and "81036533" not in n
    assert "<EVT>" in n and "<ACC>" in n


def test_same_type_different_ids_normalize_equal():
    a = semcache.normalize("Get order by Order Number:ORD-6001")
    b = semcache.normalize("Get order by Order Number:ORD-7777")
    assert a == b  # same key → they will collide in the cache


def test_normalize_preserves_retry_counter():
    # Retry counters are semantically meaningful and MUST survive normalization.
    n = semcache.normalize("Retrying order creation for event evt-1234abcd5678 (attempt 2/3)")
    assert "2/3" in n


def test_normalize_preserves_percentages_and_thresholds():
    n = semcache.normalize("Margin 12% below threshold 15% for line 3")
    assert "12%" in n and "15%" in n and "line 3" in n


# =============================================================================
# refill() — id re-fill on hit
# =============================================================================
def test_refill_substitutes_current_ids():
    normalized = "Order <ORD> (cart <CART>) failed pricing"
    log = _log(message="anything", order_id="ORD-9999", cart="9999999999999999999")
    out = semcache.refill(normalized, log)
    assert "ORD-9999" in out and "9999999999999999999" in out
    assert "<ORD>" not in out and "<CART>" not in out


def test_refill_mines_ids_from_message_text():
    # Creation logs carry ids only in text; refill must mine them too.
    normalized = "Generated order number <ORD> for cart header <CART>"
    log = _log(
        message="Generated order number ORD-6001 for cart header 1840927365018240001",
        order_id=None,
        cart=None,
    )
    out = semcache.refill(normalized, log)
    assert "ORD-6001" in out and "1840927365018240001" in out


# =============================================================================
# SemanticCache — exact / cosine / threshold
# =============================================================================
def _payload(expl: str = "Pricing for order <ORD> failed") -> CachePayload:
    return CachePayload(
        normalized_explanation=expl,
        department=Department.backend.value,
        severity=Severity.high.value,
        confidence=0.8,
    )


def test_exact_normalized_match_is_a_hit():
    cache = SemanticCache(FakeEncoder(), threshold=0.95, max_entries=10)
    cache.store("Get order by Order Number:ORD-6001", _payload())
    # different id, same type → same normalized key → exact-path hit
    hit = cache.lookup("Get order by Order Number:ORD-7777")
    assert hit is not None and hit.department == Department.backend.value


def test_different_error_types_miss():
    cache = SemanticCache(FakeEncoder(), threshold=0.95, max_entries=10)
    cache.store("SPT price list unavailable for order ORD-6001", _payload())
    miss = cache.lookup("Order blocked by margin check for order ORD-6001")
    assert miss is None  # semantically different → no false collapse


def test_below_threshold_misses():
    # A high threshold means a merely-similar (not near-identical) log misses.
    cache = SemanticCache(FakeEncoder(), threshold=0.99, max_entries=10)
    cache.store("SAP submission failed for order ORD-6001", _payload())
    # shares "order" but differs on the salient tokens → cosine < 0.99
    assert cache.lookup("Auth disabled for order ORD-6001") is None


def test_lru_evicts_oldest():
    cache = SemanticCache(FakeEncoder(), threshold=0.95, max_entries=2)
    cache.store("alpha beta gamma ORD-1", _payload())
    cache.store("delta epsilon zeta ORD-2", _payload())
    cache.store("eta theta iota ORD-3", _payload())  # evicts the alpha entry
    assert len(cache) == 2
    # The evicted type is gone (neither exact nor near-enough on cosine).
    assert cache.lookup("alpha beta gamma ORD-9") is None


def test_disabled_cache_without_encoder_always_misses():
    cache = SemanticCache(None, threshold=0.95, max_entries=10)
    cache.store("anything ORD-1", _payload())  # no-op
    assert cache.lookup("anything ORD-1") is None
    assert not cache.enabled


# =============================================================================
# dump / load persistence
# =============================================================================
def test_dump_and_load_roundtrip():
    cache = SemanticCache(FakeEncoder(), threshold=0.95, max_entries=10)
    cache.store("Get order by Order Number:ORD-6001", _payload())
    blob = cache.dump()

    restored = SemanticCache(FakeEncoder(), threshold=0.95, max_entries=10)
    restored.load(blob)
    hit = restored.lookup("Get order by Order Number:ORD-7777")
    assert hit is not None and hit.confidence == 0.8


def test_load_tolerates_garbage():
    cache = SemanticCache(FakeEncoder(), threshold=0.95, max_entries=10)
    cache.load("not json")          # no raise
    cache.load(None)                # no raise
    assert len(cache) == 0


# =============================================================================
# process() integration — hit skips both LLM calls, miss runs + stores
# =============================================================================
async def test_hit_skips_llm(install_cache):
    redis, cache = install_cache()
    explainer = CountingModel("SPT pricing for order ORD-6001 was unreachable.")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    # 1st log: miss → runs the LLM, stores the result.
    a1 = await process(_log(message="SPT price list unavailable", log_id="L1"), deps)
    assert a1.source == "ai" and a1.cached is False
    assert explainer.calls == 1 and router.calls == 1

    # 2nd log, SAME type, DIFFERENT order id: hit → NO further LLM calls.
    a2 = await process(
        _log(message="SPT price list unavailable", order_id="ORD-7777", log_id="L2"), deps
    )
    assert a2.cached is True and a2.source == "ai"
    assert explainer.calls == 1 and router.calls == 1  # unchanged → LLM not called
    assert a2.department == Department.backend and a2.severity == Severity.high


async def test_hit_refills_current_order_id(install_cache):
    install_cache()
    explainer = CountingModel("SPT pricing for order ORD-6001 was unreachable.")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    await process(_log(message="SPT price list unavailable", order_id="ORD-6001", log_id="L1"), deps)
    a2 = await process(
        _log(message="SPT price list unavailable", order_id="ORD-8888", log_id="L2"), deps
    )
    # The reused explanation shows THIS log's id, not the cached one.
    assert a2.cached is True
    assert "ORD-8888" in a2.explanation
    assert "ORD-6001" not in a2.explanation


async def test_different_type_is_a_miss_and_calls_llm(install_cache):
    install_cache()
    explainer = CountingModel("expl for the log")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    await process(_log(message="SPT price list unavailable", log_id="L1"), deps)
    # A different error type must NOT reuse the cached answer.
    await process(_log(message="Order blocked by margin check", log_id="L2"), deps)
    assert explainer.calls == 2 and router.calls == 2  # both were LLM misses


async def test_retry_counter_difference_is_a_miss(install_cache):
    install_cache()
    explainer = CountingModel("expl")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    await process(
        _log(message="Retrying order creation for event evt-aaaa1111 (attempt 2/3)", log_id="L1"),
        deps,
    )
    await process(
        _log(message="Retrying order creation for event evt-bbbb2222 (attempt 3/3)", log_id="L2"),
        deps,
    )
    # The retry counter is meaningful and unmasked → different key → LLM ran twice.
    assert explainer.calls == 2 and router.calls == 2


async def test_below_threshold_process_miss(install_cache):
    install_cache(threshold=0.999)  # effectively only exact matches hit
    explainer = CountingModel("expl")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    await process(_log(message="SAP submission failed for the order", log_id="L1"), deps)
    await process(_log(message="Auth disabled for the account", log_id="L2"), deps)
    assert explainer.calls == 2  # the near-but-not-identical second log missed


async def test_hit_miss_counters_increment(install_cache):
    redis, cache = install_cache()
    explainer = CountingModel("expl for order ORD-6001")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    await process(_log(message="SPT price list unavailable", log_id="L1"), deps)  # miss
    await process(
        _log(message="SPT price list unavailable", order_id="ORD-2", log_id="L2"), deps
    )  # hit
    stats = await semcache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1
    assert stats["total"] == 2 and stats["hit_rate"] == 0.5


async def test_fallback_result_is_not_cached(install_cache):
    """When the LLM is down the alert is a fallback pass-through — never cached,
    so recovery doesn't serve a stale fallback as if it were an AI answer."""
    install_cache()
    deps = PipelineDeps(breaker=_breaker(FakeRedis(), FakeClock()), explainer=None, router=None)
    a1 = await process(_log(message="SPT price list unavailable", log_id="L1"), deps)
    assert a1.source == "fallback"
    deps2 = get_deps_from(semcache)
    assert len(deps2.cache) == 0  # nothing stored


def get_deps_from(mod):
    return mod.get()


# =============================================================================
# Divergence guard — the safety veto on the cosine path
# =============================================================================
# The real all-MiniLM model rates these meaning-flipping pairs >= 0.95 (measured:
# "succeeded" vs "failed" = 0.951, "2/3" vs "3/3" = 0.992, "14%" vs "2%" = 0.982,
# "80%" vs "98%" = 0.982). Cosine alone would MERGE them and serve a wrong answer.
# The guard vetoes any cosine candidate that disagrees on a salient token.
from ai_service.semcache import diverges, _salient_tokens, normalize  # noqa: E402

# The production salient word set (what main.py injects).
SALIENT = settings.SEMCACHE_SALIENT_WORDS


class ConstantEncoder:
    """Returns the SAME vector for every input → cosine is 1.0 for any pair.

    This removes cosine from the equation entirely, so a lookup can only miss
    because of the divergence guard — isolating and proving the guard's effect.
    """

    def encode(self, text: str):
        return [1.0, 0.0, 0.0]


# --- pure guard functions ----------------------------------------------------
def test_salient_tokens_picks_numbers_and_outcome_words():
    toks = _salient_tokens(normalize("SAP submission failed after attempt 2/3"), SALIENT)
    assert "failed" in toks and "2/3" in toks
    # ordinary words are not salient
    assert "submission" not in toks and "after" not in toks


def test_salient_tokens_ignores_masked_ids():
    # <ORD>/<CART> carry no meaning for the guard — a differing id must not veto.
    toks = _salient_tokens(normalize("pricing failed for order ORD-6001"), SALIENT)
    assert "<ord>" not in toks and "ord-6001" not in toks
    assert "failed" in toks


def test_diverges_on_outcome_flip():
    assert diverges(
        normalize("SAP submission succeeded for order ORD-1"),
        normalize("SAP submission failed for order ORD-1"),
        SALIENT,
    )


def test_diverges_on_number_flip():
    assert diverges(
        normalize("Margin 14% below threshold 15%"),
        normalize("Margin 2% below threshold 15%"),
        SALIENT,
    )
    assert diverges(
        normalize("Retrying order creation (attempt 2/3)"),
        normalize("Retrying order creation (attempt 3/3)"),
        SALIENT,
    )


def test_does_not_diverge_on_id_only_difference():
    # Same type, different ids → NO divergence (this is exactly what should hit).
    assert not diverges(
        normalize("SPT price list unavailable for order ORD-1"),
        normalize("SPT price list unavailable for order ORD-9999"),
        SALIENT,
    )


def test_diverges_on_missing_negation():
    # "not" present on one side only must count as divergence.
    assert diverges(
        normalize("costCenter UDF found"),
        normalize("costCenter UDF not found"),
        SALIENT,
    )


# --- guard inside the cache (cosine forced to 1.0 by ConstantEncoder) --------
def _payload_dept(dept=Department.backend, sev=Severity.high, conf=0.8, expl="x for <ORD>"):
    return CachePayload(expl, dept.value, sev.value, conf)


def test_guard_vetoes_high_cosine_meaning_flip():
    # cosine == 1.0 for everything, so ONLY the guard can cause a miss.
    cache = SemanticCache(ConstantEncoder(), threshold=0.95, max_entries=10, salient_words=SALIENT)
    cache.store("SAP submission succeeded for order ORD-1", _payload_dept())
    # A different outcome must NOT reuse the "succeeded" answer despite cosine 1.0.
    assert cache.lookup("SAP submission failed for order ORD-1") is None


def test_guard_allows_id_only_difference_at_high_cosine():
    cache = SemanticCache(ConstantEncoder(), threshold=0.95, max_entries=10, salient_words=SALIENT)
    cache.store("SPT price list unavailable for order ORD-1", _payload_dept())
    # Same salient tokens, different id → guard passes → cosine hit.
    hit = cache.lookup("SPT price list unavailable for order ORD-9999")
    assert hit is not None and hit.department == Department.backend.value


def test_guard_can_be_disabled():
    # With guard=False the meaning-flip is (wrongly) served — proves the guard is
    # what's doing the work when enabled.
    cache = SemanticCache(
        ConstantEncoder(), threshold=0.95, max_entries=10, salient_words=SALIENT, guard=False
    )
    cache.store("SAP submission succeeded for order ORD-1", _payload_dept())
    assert cache.lookup("SAP submission failed for order ORD-1") is not None


async def test_guard_forces_llm_on_meaning_flip(install_cache):
    """End-to-end: an outcome-flipped log misses (guard veto) and the LLM runs,
    rather than reusing the near-identical cached answer."""
    # install_cache uses the real FakeEncoder; force cosine high by making the
    # two messages share almost all tokens but differ on a salient one.
    redis, cache = install_cache()
    # Replace the cache with one using the production salient set + constant
    # encoder so cosine can't save us — the guard must.
    semcache.get().cache._encoder = ConstantEncoder()
    semcache.get().cache._salient_words = SALIENT

    explainer = CountingModel("SAP submission result for order ORD-1")
    router = CountingModel('{"department": "backend", "severity": "high", "confidence": 0.8}')
    deps = _healthy_deps(explainer, router)

    await process(_log(message="SAP submission succeeded for order ORD-1", log_id="L1"), deps)
    assert explainer.calls == 1
    # The flipped outcome must NOT be served from cache → LLM runs again.
    await process(_log(message="SAP submission failed for order ORD-1", log_id="L2"), deps)
    assert explainer.calls == 2
