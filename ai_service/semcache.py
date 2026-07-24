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
   returned explanation shows the right order id, never the cached one.

The cache holds only successful AI answers (a hit is still an AI answer, just
reused). Misses — including every miss while the breaker is open — fall through
to the pipeline unchanged, so the "useful with the LLM completely down"
guarantee is untouched.
"""
from __future__ import annotations

import json
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from shared.models import Department, Severity

# --- id masking (mirrors backend/stitching.py id shapes) ----------------------
# Each family has a mask token and the pattern that recognizes it. ORDER
# MATTERS: the 19-digit cart id is masked before the generic 6+-digit account
# run, so a cart header is never mis-masked as an account. The account pattern
# needs >= 6 digits precisely so it can NOT swallow a retry counter ("2/3"),
# a percentage ("12%"), an attempt number or a small threshold — those stay
# visible and keep distinct alerts distinct.
_MASKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("<EVT>", re.compile(r"evt-[0-9a-f-]{8,}")),
    ("<ORD>", re.compile(r"\bORD-\d+\b")),
    ("<CART>", re.compile(r"\b\d{19}\b")),
    ("<ACC>", re.compile(r"\b\d{6,}\b")),  # account numbers (>=6 digits)
)


def normalize(message: str) -> str:
    """Mask volatile ids in ``message`` so same-type logs share one cache key.

    Masks ``evt-...`` → ``<EVT>``, ``ORD-N`` → ``<ORD>``, a 19-digit cart id →
    ``<CART>``, and any remaining 6+-digit run (an account number) → ``<ACC>``.
    Leaves everything else — including retry counters, percentages and
    thresholds — untouched, so semantically distinct alerts do not collapse.
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


def _current_ids(log) -> dict[str, str]:
    """The concrete ids to re-fill into a normalized explanation for ``log``.

    Prefers the structured fields; falls back to mining the message text (the
    creation logs carry the ids only in text) — same sources the stitcher uses.
    Returns only the mask tokens that have a concrete value for this log.
    """
    ids: dict[str, str] = {}
    for token, field in (("<EVT>", "eventId"), ("<ORD>", "orderId"), ("<CART>", "cartHeaderId")):
        value = getattr(log, field, None)
        if value:
            ids[token] = value
    message = log.message or ""
    for token, pattern in _MASKS:
        if token in ("<EVT>", "<ORD>", "<CART>") and token not in ids:
            match = pattern.search(message)
            if match:
                ids[token] = match.group(0)
    return ids


def refill(normalized_text: str, log) -> str:
    """Substitute the CURRENT log's ids back into a normalized explanation.

    Each mask token (``<ORD>`` …) is replaced with this log's actual id, so a
    reused explanation reads with the right order id — never the cached one. A
    token with no concrete id for this log is left as-is (rare; still readable).
    """
    text = normalized_text
    for token, value in _current_ids(log).items():
        text = text.replace(token, value)
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
def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (0 if either is zero)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _as_floats(vector) -> list[float]:
    """Coerce an encoder result (list / numpy array / tuple) to list[float]."""
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(x) for x in vector]


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


# --- module-level dependency holder (same pattern as api.configure) ----------
@dataclass
class SemCacheDeps:
    cache: SemanticCache
    redis: object | None  # redis.asyncio client, or None in unit tests
    dump_key: str
    hits_key: str
    misses_key: str


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
