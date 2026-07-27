"""[3] AI Service — semantic cache for the explainer+router (CLAUDE.md [3]).

The order-pipeline log corpus is highly repetitive: the same handful of
WARN/ERROR *types* recur constantly, differing only by volatile ids
(``ORD-6001`` vs ``ORD-6002``, a fresh ``evt-...`` per flow, a 19-digit cart
header). Explaining and routing each recurrence with two fresh LLM calls is
pure waste. This module caches the AI answer per *log type* and reuses it,
skipping BOTH LLM calls on a hit.

The design (CLAUDE.md "semantic cache"), in order:

1. **Normalization is the core trick.** :func:`normalize` masks the
   volatile-irrelevant ids in a message (``ORD-\\d+`` → ``<ORD>`` etc.) so two
   same-type logs that differ only by ids produce the SAME key and collide.
   Semantically meaningful tokens (retry counters like ``attempt 2/3``, margin
   percentages, thresholds) are deliberately NOT masked — masking them would
   collapse alerts that must stay distinct.

2. **Local embeddings, loaded once.** A sentence-transformers model
   (``all-MiniLM-L6-v2``, CPU) is loaded a single time at startup and injected
   (:func:`configure`), so tests pass a fake encoder — no network, no downloads.

3. **Store — no new infra.** An in-memory LRU (cap ~500) of
   ``{normalized_text, vector, payload}``, persisted to the existing Redis
   under one key (:func:`dump` / :func:`load`). No new container.

4. **Lookup order.** normalize → exact-match on normalized text (fast path,
   most hits) → else cosine vs stored vectors, taking the best match only if its
   similarity ``>= SEMCACHE_THRESHOLD``. Otherwise a miss.

5. **Divergence guard (cosine path only).** Cosine is blind to a *small but
   meaning-flipping* difference: ``"submission succeeded"`` vs ``"submission
   failed"`` score ~0.95 on a general-purpose embedding, yet must NOT share an
   answer. So a cosine candidate is accepted only if the two normalized strings
   ALSO agree on their **salient tokens** — numbers/units, outcome/polarity
   words (failed/succeeded/passed/aborted/blocked/timeout/…), and negation. Any
   salient-token disagreement vetoes the hit → miss → the LLM runs. This is the
   safety half of "recall from cosine, precision from the guard"; the cache
   always fails toward a miss (a false miss costs one LLM call, a false hit
   serves a wrong AI-labelled answer). The exact-match fast path never needs the
   guard — identical normalized text can't disagree on anything.

6. **Id re-fill on hit.** The explanation is stored in NORMALIZED form (ids
   masked); on a hit the CURRENT log's ids are substituted back in, so the
   returned explanation shows the right order id, never the cached one. EVERY
   mask token normalize() can emit must be re-fillable — including ``<ACC>``
   from ``accountNumber`` — otherwise the placeholder leaks verbatim into the
   agent-facing explanation. A token with no value on this log degrades to
   neutral prose ("the account"), never the raw ``<ACC>``.

7. **Single flight (:class:`InFlight`).** The store happens only AFTER both LLM
   calls return, leaving a multi-second window where an answer is being computed
   but nothing records it. Since the poller processes alerts CONCURRENTLY and a
   failure burst emits byte-identical lines milliseconds apart, that window let
   every log in a burst miss and duplicate the work (measured: 4 identical logs
   → 8 LLM calls). The first caller for a key computes; concurrent callers await
   its payload and re-fill their OWN ids. Cost-only — it never changes which
   answer a log gets.

The cache holds only successful AI answers (a hit is still an AI answer, just
reused). Misses — including every miss while the breaker is open — fall through
to the pipeline unchanged, so the "useful with the LLM completely down"
guarantee is untouched.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

from shared.models import Department, Severity

# --- id masking (mirrors backend/stitching.py id shapes) ----------------------
# Each family has a mask token and the pattern that recognizes it. ORDER
# MATTERS: the 19-digit cart id is masked before the 8-digit account run, so a
# cart header is never mis-masked as an account. The account pattern is 8 digits
# exactly so it can NOT swallow a retry counter ("2/3"), a percentage ("12%"), a
# threshold, or a 7-digit product id — those stay visible and keep distinct
# alerts distinct.
_MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<EVT>", re.compile(r"evt-[0-9a-f-]{8,}")),
    ("<ORD>", re.compile(r"\bORD-\d+\b")),
    ("<CART>", re.compile(r"\b\d{19}\b")),
    # Account numbers are EXACTLY 8 digits (shared/scenarios.py). The old \d{6,}
    # also swallowed 7-digit PRODUCT ids, which are meaning-bearing, not volatile:
    # "No internal SKU mapping found for product 9999999" is *about* that id, and
    # masking it both merged distinct SKU-mapping alerts and made refill()
    # substitute the log's accountNumber where a product id belonged. Product ids
    # therefore stay visible, like retry counters and thresholds.
    ("<ACC>", re.compile(r"\b\d{8}\b")),
)


def normalize(message: str) -> str:
    """Mask volatile ids in ``message`` so same-type logs share one cache key.

    Masks ``evt-...`` → ``<EVT>``, ``ORD-N`` → ``<ORD>``, a 19-digit cart id →
    ``<CART>``, and a remaining 8-digit run (an account number) → ``<ACC>``.
    Leaves everything else — including retry counters, percentages, thresholds
    and 7-digit product ids — untouched, so semantically distinct alerts do not
    collapse.
    """
    text = message or ""
    for token, pattern in _MASKS:
        text = pattern.sub(token, text)
    return text.strip()


# --- divergence guard ---------------------------------------------------------
# A "salient" token is one whose difference changes the MEANING of an otherwise
# near-identical log, so two logs that disagree on any salient token must not
# share a cached answer even at high cosine. Two kinds are salient:
#   * numeric/unit tokens — any token containing a digit ("2/3", "14%", "503",
#     "500ms", "98"). These survived normalization on purpose (see _MASKS), so a
#     numeric difference here is always meaningful (counters, thresholds, codes).
#   * configured outcome/polarity/negation words (settings.SEMCACHE_SALIENT_WORDS)
#     — "failed" vs "succeeded", "not" vs absent, etc.
# The mask tokens themselves (<ORD>/<EVT>/<CART>/<ACC>) contain no digit and are
# not words, so they are correctly NOT salient — differing ids are noise.
# Token = a run of letters, OR any whitespace-delimited chunk containing a digit
# ("2/3", "14%", "503", "500ms"). The mask placeholders (<ORD>/<EVT>/<CART>/<ACC>)
# contain no digit and their inner letters (ord/evt/cart/acc) are not salient
# words, so they are never picked up — a differing id can never veto a hit.
_WORD = re.compile(r"[A-Za-z]+|\S*\d\S*")


def _salient_tokens(normalized_text: str, salient_words: frozenset[str]) -> frozenset[str]:
    """The meaning-bearing tokens of a normalized message.

    Returns the lowercased set of: every token containing a digit (counters,
    percentages, thresholds, status codes), plus every word in ``salient_words``
    (outcome/polarity/negation). Order-independent (a set), so the guard is
    insensitive to word reordering — which is exactly what cosine recall is for.
    """
    tokens: set[str] = set()
    for match in _WORD.finditer(normalized_text):
        tok = match.group(0).lower()
        if any(ch.isdigit() for ch in tok):
            tokens.add(tok)
        elif tok in salient_words:
            tokens.add(tok)
    return frozenset(tokens)


def diverges(a_norm: str, b_norm: str, salient_words: frozenset[str]) -> bool:
    """True if two normalized messages disagree on any salient token.

    This is the veto: when it returns True, a cosine candidate is rejected even
    if its similarity cleared the threshold. Symmetric set difference means a
    salient token present in one message but not the other also counts as
    divergence (e.g. a "not"/"final" that appears on only one side).
    """
    return _salient_tokens(a_norm, salient_words) != _salient_tokens(b_norm, salient_words)


# Every mask token normalize() can produce, paired with the LogLine field that
# holds the concrete value. accountNumber is included here for re-fill ONLY —
# that is a display concern and says nothing about correlation, which never
# consults accountNumber (see backend/stitching.py).
_REFILL_FIELDS: tuple[tuple[str, str], ...] = (
    ("<EVT>", "eventId"),
    ("<ORD>", "orderId"),
    ("<CART>", "cartHeaderId"),
    ("<ACC>", "accountNumber"),
)

# Fallback wording for a mask token with no concrete value on this log — an
# unfilled "<ACC>" is leaked internals in an agent-facing explanation, so the
# text degrades to a neutral phrase instead of showing the placeholder.
_MASK_FALLBACK: dict[str, str] = {
    "<EVT>": "the event",
    "<ORD>": "the order",
    "<CART>": "the cart header",
    "<ACC>": "the account",
}


def _current_ids(log) -> dict[str, str]:
    """The concrete ids to re-fill into a normalized explanation for ``log``.

    Prefers the structured fields; falls back to mining the message text (the
    creation logs carry the ids only in text) — same sources the stitcher uses.
    Returns only the mask tokens that have a concrete value for this log.
    """
    ids: dict[str, str] = {}
    for token, attr in _REFILL_FIELDS:
        value = getattr(log, attr, None)
        if value:
            ids[token] = str(value)
    # Mining fallback. _MASKS order is load-bearing here exactly as it is in
    # normalize(): each family is masked out of the text before the next pattern
    # runs, so the generic 6+-digit account pattern can never mine a 19-digit
    # cart header (or the digits inside an ORD-N) and mislabel it as an account.
    remaining = log.message or ""
    for token, pattern in _MASKS:
        if token not in ids:
            match = pattern.search(remaining)
            if match:
                ids[token] = match.group(0)
        remaining = pattern.sub(token, remaining)
    return ids


def refill(normalized_text: str, log) -> str:
    """Substitute the CURRENT log's ids back into a normalized explanation.

    Each mask token (``<ORD>`` …) is replaced with this log's actual id, so a
    reused explanation reads with the right order id — never the cached one. A
    token with no concrete value for this log degrades to neutral prose ("the
    order") rather than leaking the placeholder into agent-facing text.
    """
    text = normalized_text
    ids = _current_ids(log)
    for token, _attr in _REFILL_FIELDS:
        replacement = ids.get(token) or _MASK_FALLBACK[token]
        text = text.replace(token, replacement)
    return text


# --- injectable encoder -------------------------------------------------------
class Encoder(Protocol):
    """The tiny slice of a sentence-transformers model the cache needs.

    ``encode(text) -> list[float]`` (or anything indexable/iterable of floats).
    Real: ``SentenceTransformer(...).encode``. Tests pass a deterministic fake.
    """

    def encode(self, text: str): ...


def load_encoder(model_name: str) -> Encoder | None:
    """Load the local sentence-transformers model ONCE, or ``None`` on failure.

    Deferred import so the module (and the whole service) still loads if the
    optional ``sentence-transformers`` package is absent — in that case the
    cache is simply disabled (every lookup misses, pipeline runs as today). The
    model runs on CPU; the caller loads it a single time at startup and injects
    it via :func:`configure`.
    """
    if not model_name:
        return None
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:  # pragma: no cover - optional dependency missing
        print(f"[semcache] sentence-transformers unavailable, cache disabled: {exc}", flush=True)
        return None
    try:
        return SentenceTransformer(model_name, device="cpu")
    except Exception as exc:  # pragma: no cover - model download/init failure
        print(f"[semcache] could not load model {model_name!r}, cache disabled: {exc}", flush=True)
        return None


# --- payload ------------------------------------------------------------------
@dataclass
class CachePayload:
    """The reusable AI answer for one log type (all fields normalized-safe).

    ``normalized_explanation`` has ids masked; :func:`refill` puts the current
    log's ids back on a hit. department/severity are stored as their enum string
    values and re-validated against the enums on the way out (defensive).
    """

    normalized_explanation: str
    department: str
    severity: str | None
    confidence: float | None

    def to_dict(self) -> dict:
        return {
            "explanation": self.normalized_explanation,
            "department": self.department,
            "severity": self.severity,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CachePayload":
        return cls(
            normalized_explanation=data["explanation"],
            department=data["department"],
            severity=data.get("severity"),
            confidence=data.get("confidence"),
        )


@dataclass
class _Entry:
    normalized_text: str
    vector: list[float]
    payload: CachePayload


# --- cosine -------------------------------------------------------------------
# Public (unprefixed) because ai_service/ragindex.py imports them: both the cache
# and the retrieval index run cosine over vectors from the SAME encoder, so they
# must use the same similarity and the same coercion. Shared, never duplicated —
# two copies could drift and make a 0.95 cache threshold and a 0.30 retrieval
# floor mean subtly different things.
def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0 if either is zero)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def as_floats(vector) -> list[float]:
    """Coerce an encoder result (list / numpy array / tuple) to list[float]."""
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(x) for x in vector]


# Back-compat aliases: the private names predate the ragindex extraction and are
# still used below (and possibly by tests), so keep both pointing at one impl.
_cosine = cosine
_as_floats = as_floats


class SemanticCache:
    """LRU semantic cache of AI answers, keyed by normalized log text.

    Lookup: exact normalized-text match (fast path) → cosine vs stored vectors
    (>= threshold) → miss. Stores are LRU-capped and idempotent per normalized
    text. The encoder and hit/miss counters are injected; the whole thing is
    unit-tested with a fake encoder and no Redis.
    """

    def __init__(
        self,
        encoder: Encoder | None,
        *,
        threshold: float,
        max_entries: int,
        salient_words: frozenset[str] | None = None,
        guard: bool = True,
    ) -> None:
        self._encoder = encoder
        self._threshold = threshold
        self._max_entries = max(1, max_entries)
        # Divergence guard: applied on the cosine path only (see :func:`diverges`).
        # ``guard=False`` disables the veto (cosine alone decides) — not advised.
        self._guard = guard
        self._salient_words = salient_words if salient_words is not None else frozenset()
        # normalized_text -> _Entry, ordered by recency (move_to_end on use).
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    @property
    def enabled(self) -> bool:
        """The cache only operates with an encoder present (else always miss)."""
        return self._encoder is not None

    def __len__(self) -> int:
        return len(self._entries)

    # --- lookup ---------------------------------------------------------------
    def lookup(self, message: str) -> CachePayload | None:
        """Return a cached payload for ``message``'s type, or ``None`` (miss).

        Never calls the LLM. The returned payload's explanation is still
        NORMALIZED — the caller re-fills the current log's ids via :func:`refill`.
        """
        if not self.enabled:
            return None
        key = normalize(message)
        # Fast path: exact normalized match — the overwhelmingly common hit.
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
            return entry.payload
        # Semantic path: cosine against stored vectors.
        if not self._entries:
            return None
        vector = _as_floats(self._encoder.encode(key))
        best_key, best_sim = None, -1.0
        for stored_key, stored in self._entries.items():
            sim = _cosine(vector, stored.vector)
            if sim > best_sim:
                best_key, best_sim = stored_key, sim
        if best_key is None or best_sim < self._threshold:
            return None
        # Divergence guard: cosine cleared the threshold, but reject the hit if
        # the two normalized messages disagree on a salient token (a meaning
        # flip cosine is blind to). A veto is a MISS — the LLM runs, which is the
        # safe direction. If the top match diverges we do NOT fall back to a
        # lower-similarity candidate: a weaker match is a weaker reason to reuse.
        if self._guard and diverges(key, best_key, self._salient_words):
            return None
        self._entries.move_to_end(best_key)
        return self._entries[best_key].payload

    # --- store ----------------------------------------------------------------
    def peek_exact(self, message: str) -> CachePayload | None:
        """The payload stored under ``message``'s EXACT normalized key, or None.

        Deliberately skips the cosine path: single flight uses this to hand the
        leader's own answer to its followers, and a fuzzy neighbour is not the
        leader's answer. If the leader stored nothing (fallback / breaker open),
        followers must get ``None`` and run the pipeline — never a lookalike.
        Does not touch LRU recency or the hit/miss counters.
        """
        entry = self._entries.get(normalize(message))
        return entry.payload if entry is not None else None

    def store(self, message: str, payload: CachePayload) -> None:
        """Cache the AI answer for ``message``'s type (LRU, idempotent per key)."""
        if not self.enabled:
            return
        key = normalize(message)
        vector = _as_floats(self._encoder.encode(key))
        self._entries[key] = _Entry(normalized_text=key, vector=vector, payload=payload)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)  # evict least-recently-used

    # --- persistence (to the existing Redis, no new infra) --------------------
    def dump(self) -> str:
        """Serialize the whole cache to a JSON string (for Redis)."""
        return json.dumps(
            [
                {
                    "normalized_text": e.normalized_text,
                    "vector": e.vector,
                    "payload": e.payload.to_dict(),
                }
                for e in self._entries.values()
            ]
        )

    def load(self, blob: str | bytes | None) -> None:
        """Restore entries from a :func:`dump` blob (best-effort; bad data → empty)."""
        if not blob:
            return
        if isinstance(blob, bytes):
            blob = blob.decode()
        try:
            items = json.loads(blob)
        except (ValueError, json.JSONDecodeError):
            return
        self._entries.clear()
        for item in items:
            try:
                self._entries[item["normalized_text"]] = _Entry(
                    normalized_text=item["normalized_text"],
                    vector=[float(x) for x in item["vector"]],
                    payload=CachePayload.from_dict(item["payload"]),
                )
            except (KeyError, TypeError, ValueError):
                continue  # skip a malformed entry, keep the rest


# --- single-flight (in-flight coalescing) ------------------------------------
class InFlight:
    """Coalesces concurrent work on the SAME normalized key ("single flight").

    The cache is only populated AFTER the two LLM calls return, so between a
    miss and the store there is a multi-second window in which the answer is
    being computed but nothing records that. The poller processes alerts
    CONCURRENTLY (``ALERT_CONCURRENCY``), and a burst of a single failure type
    emits byte-identical lines milliseconds apart — so without this, every log
    in the burst misses, calls the LLM, and stores the same answer. Measured: 4
    identical logs → 8 LLM calls where 2 suffice.

    This registry closes that window. The first caller for a key becomes the
    LEADER and computes; concurrent callers become FOLLOWERS and await the
    leader's :class:`CachePayload`. Followers re-fill ids from their OWN log, so
    a shared payload never leaks the leader's ids (the payload is normalized —
    that is precisely why the leader shares the payload, not its finished
    alert).

    Failure handling preserves "fail toward a miss": if the leader produces no
    reusable payload (fallback, breaker open, LLM error), followers are woken
    with ``None`` and run the pipeline themselves rather than inheriting a
    failure. Correctness never depends on this class — it only removes
    duplicate work.
    """

    def __init__(self) -> None:
        # normalized key -> the future carrying that key's payload (or None).
        self._waiters: dict[str, "asyncio.Future[CachePayload | None]"] = {}

    def leader(self, key: str) -> "asyncio.Future[CachePayload | None] | None":
        """Claim ``key``, or return the in-flight future to await as a follower.

        ``None`` means the caller is the leader and must compute, then call
        :meth:`resolve` exactly once. A returned future means another task is
        already computing this key.
        """
        existing = self._waiters.get(key)
        if existing is not None:
            return existing
        self._waiters[key] = asyncio.get_running_loop().create_future()
        return None

    def resolve(self, key: str, payload: CachePayload | None) -> None:
        """Publish the leader's result to followers and release the key.

        Always called by the leader (in a ``finally``), so an exception can
        never leave followers waiting forever — they get ``None`` and fall
        through to their own pipeline run.
        """
        future = self._waiters.pop(key, None)
        if future is not None and not future.done():
            future.set_result(payload)

    def __len__(self) -> int:
        return len(self._waiters)


# --- module-level dependency holder (same pattern as api.configure) ----------
@dataclass
class SemCacheDeps:
    cache: SemanticCache
    redis: object | None  # redis.asyncio client, or None in unit tests
    dump_key: str
    hits_key: str
    misses_key: str
    # Single-flight registry, per process. Defaulted so existing construction
    # sites (and tests) need no change.
    inflight: InFlight = field(default_factory=InFlight)


_deps: SemCacheDeps | None = None


def configure(deps: SemCacheDeps) -> None:
    """Install the runtime cache + Redis handles (called by main.py / tests)."""
    global _deps
    _deps = deps


def get() -> SemCacheDeps | None:
    """The installed deps, or ``None`` if the cache was never configured."""
    return _deps


# --- hit/miss accounting (the demo number) -----------------------------------
async def record_hit() -> None:
    if _deps and _deps.redis is not None:
        await _deps.redis.incr(_deps.hits_key)


async def record_miss() -> None:
    if _deps and _deps.redis is not None:
        await _deps.redis.incr(_deps.misses_key)


async def persist() -> None:
    """Persist the current cache to Redis (called after a store)."""
    if _deps and _deps.redis is not None:
        await _deps.redis.set(_deps.dump_key, _deps.cache.dump())


async def restore() -> None:
    """Load the cache from Redis at startup (best-effort)."""
    if _deps and _deps.redis is not None:
        blob = await _deps.redis.get(_deps.dump_key)
        _deps.cache.load(blob)


async def stats() -> dict:
    """Current hit/miss counts + hit rate + cache size (for /semcache/stats)."""
    hits = misses = 0
    if _deps and _deps.redis is not None:
        hits = int(await _deps.redis.get(_deps.hits_key) or 0)
        misses = int(await _deps.redis.get(_deps.misses_key) or 0)
    total = hits + misses
    size = len(_deps.cache) if _deps else 0
    return {
        "hits": hits,
        "misses": misses,
        "total": total,
        "hit_rate": (hits / total) if total else 0.0,
        "size": size,
        "enabled": bool(_deps and _deps.cache.enabled),
    }
