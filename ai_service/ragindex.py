"""[3] AI Service — retrieval index over incident history (RAG, phase 1).

Semantic search over the incidents the system has already produced: processed
alerts and completed journeys. **No LLM anywhere in this module** — this phase
delivers retrieval only, so ``/chat`` answers from a deterministic template built
from the retrieved records. A generation step can be layered on later without
touching the store or the retrieval contract.

Relationship to :mod:`ai_service.semcache` — same embedding stack, opposite goal:

===================  ==========================  ============================
                     semcache                    ragindex
===================  ==========================  ============================
question             "have I answered THIS log    "which past incidents relate
                     before?"                     to this question?"
keyed by             normalized message text      caller-supplied record id
threshold            0.95 — near-identical only   0.30 — a loose recall floor
on a wrong answer    serves a wrong AI answer     shows a less relevant source
size                 ~500 log TYPES               ~5000 incident RECORDS
===================  ==========================  ============================

The thresholds differ by design and must not be unified: the cache must fail
toward a miss because a false hit is a wrong AI-labelled answer, whereas
retrieval wants recall — a marginally relevant source is visible to the agent and
costs nothing. That is why ``RAGINDEX_MIN_SCORE`` defaults to 0.30 and is a
*floor to drop noise*, not a precision gate.

Shared with semcache (imported, never duplicated): :class:`~ai_service.semcache.Encoder`,
:func:`~ai_service.semcache.load_encoder`, :func:`~ai_service.semcache.cosine`
and ``as_floats``. If the encoder cannot load, the index **disables itself** and
retrieval returns ``[]`` — exactly the semcache self-disabling pattern, so the
service runs unchanged without ``sentence-transformers`` installed.

Store: an in-memory ``dict`` of record id -> entry, persisted to the existing
Redis under one key (``ai:ragindex``). Upsert by id, so re-indexing a record —
including a full backfill re-run — replaces it rather than duplicating it. No new
infra, no pgvector; retrieval is a linear cosine scan, which is fine at the 5000
cap and would only need an ANN index at much larger scale.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# The embedding stack is shared with the semantic cache — one model, one cosine
# implementation, one self-disabling convention. Importing (not copying) is what
# keeps "no new ML dependencies" true and the two paths consistent.
from ai_service.semcache import Encoder, as_floats, cosine, load_encoder

__all__ = [
    "Encoder",
    "load_encoder",
    "RagRecord",
    "RagIndex",
    "RagDeps",
    "configure",
    "get",
    "index_record",
    "retrieve",
    "persist",
    "restore",
    "stats",
]


# --- record -------------------------------------------------------------------
@dataclass
class RagRecord:
    """One indexed incident: an alert or a completed journey.

    ``text`` is the embedded prose (what retrieval matches on). ``metadata`` is
    free-form and used for cheap equality filtering — department, outcome,
    app_name, level, journey_id, order_id, ts. It is deliberately untyped: the
    backend owns the DB schema and decides what is worth filtering on, and a new
    filter key must not require an AI-service change.
    """

    id: str
    kind: str  # "alert" | "journey"
    text: str
    vector: list[float]
    metadata: dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "vector": self.vector,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RagRecord":
        return cls(
            id=str(data["id"]),
            kind=str(data["kind"]),
            text=str(data["text"]),
            vector=[float(x) for x in data["vector"]],
            metadata=dict(data.get("metadata") or {}),
        )


def _matches(metadata: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    """True if ``metadata`` equals ``filters`` on every filter key.

    Equality only — this is a cheap pre-filter, not a query language. A filter
    key absent from the record's metadata never matches, so filtering on a field
    a record does not carry excludes it rather than silently passing it through.
    Values are compared as strings so a filter of ``"3"`` matches metadata ``3``
    (JSON round-trips through Redis and would otherwise change the type).
    """
    if not filters:
        return True
    for key, want in filters.items():
        if key not in metadata:
            return False
        if str(metadata[key]) != str(want):
            return False
    return True


# --- index --------------------------------------------------------------------
class RagIndex:
    """An id-keyed vector store with cosine retrieval and metadata filtering.

    Upsert semantics by record id: indexing the same id twice re-embeds and
    replaces, which is what makes the backfill script idempotent. Eviction is
    insertion-order (oldest first) once ``max_entries`` is exceeded — unlike the
    cache this is NOT an LRU, because a rarely-retrieved incident is not less
    valuable than a popular one; age is the honest proxy for "least worth
    keeping" in an incident history.
    """

    def __init__(self, encoder: Encoder | None, *, min_score: float, max_entries: int) -> None:
        self._encoder = encoder
        self._min_score = min_score
        self._max_entries = max(1, max_entries)
        self._records: dict[str, RagRecord] = {}

    @property
    def enabled(self) -> bool:
        """The index only operates with an encoder (else retrieval is empty)."""
        return self._encoder is not None

    def __len__(self) -> int:
        return len(self._records)

    # --- write ----------------------------------------------------------------
    def index(self, record_id: str, kind: str, text: str, metadata: dict | None = None) -> bool:
        """Embed and store one record (upsert by id). False if disabled/empty.

        Returns False rather than raising when the index is disabled or the text
        is blank — indexing is a best-effort side channel and its caller (the
        backend) must never fail because retrieval is unavailable.
        """
        if not self.enabled or not text or not text.strip():
            return False
        vector = as_floats(self._encoder.encode(text))
        self._records[record_id] = RagRecord(
            id=record_id, kind=kind, text=text, vector=vector, metadata=dict(metadata or {})
        )
        # Trim oldest-first. dict preserves insertion order, and re-indexing an
        # existing id keeps its ORIGINAL position (assignment to an existing key
        # does not reorder), so a re-run of the backfill cannot reshuffle age.
        while len(self._records) > self._max_entries:
            oldest = next(iter(self._records))
            del self._records[oldest]
        return True

    # --- read -----------------------------------------------------------------
    def retrieve(
        self, query_text: str, k: int = 5, filters: dict | None = None
    ) -> list[dict]:
        """Top-``k`` records by cosine against ``query_text``, best first.

        Applies ``filters`` (metadata equality) BEFORE scoring, and drops anything
        below ``min_score``. Returns ``[]`` when the index is disabled, empty, or
        nothing clears the floor — an empty result is a normal answer here, not an
        error: ``/chat`` says so plainly rather than inventing a source.
        """
        if not self.enabled or not self._records or not query_text or not query_text.strip():
            return []
        query_vector = as_floats(self._encoder.encode(query_text))
        scored: list[dict] = []
        for record in self._records.values():
            if not _matches(record.metadata, filters):
                continue
            score = cosine(query_vector, record.vector)
            if score < self._min_score:
                continue
            scored.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "text": record.text,
                    "metadata": dict(record.metadata),
                    "score": score,
                }
            )
        # Sort by score desc; id as a tiebreaker so equal-scoring records come
        # back in a stable order (tests and the UI both depend on determinism).
        scored.sort(key=lambda r: (-r["score"], r["id"]))
        return scored[: max(0, k)]

    # --- persistence (the existing Redis, no new infra) -----------------------
    def dump(self) -> str:
        return json.dumps([r.to_dict() for r in self._records.values()])

    def load(self, blob: str | bytes | None) -> None:
        """Restore from a :meth:`dump` blob (best-effort; bad data → keep going)."""
        if not blob:
            return
        if isinstance(blob, bytes):
            blob = blob.decode()
        try:
            items = json.loads(blob)
        except (ValueError, json.JSONDecodeError):
            return
        self._records.clear()
        for item in items:
            try:
                record = RagRecord.from_dict(item)
            except (KeyError, TypeError, ValueError):
                continue  # skip a malformed record, keep the rest
            self._records[record.id] = record


# --- module-level dependency holder (same pattern as semcache/api.configure) ---
@dataclass
class RagDeps:
    index: RagIndex
    redis: object | None  # redis.asyncio client, or None in unit tests
    dump_key: str


_deps: RagDeps | None = None


def configure(deps: RagDeps | None) -> None:
    """Install the runtime index + Redis handle (called by main.py / tests)."""
    global _deps
    _deps = deps


def get() -> RagDeps | None:
    """The installed deps, or ``None`` if the index was never configured."""
    return _deps


async def index_record(
    record_id: str, kind: str, text: str, metadata: dict | None = None
) -> bool:
    """Embed + store + persist one record. False when not indexed."""
    if _deps is None:
        return False
    stored = _deps.index.index(record_id, kind, text, metadata)
    if stored:
        await persist()
    return stored


def retrieve(query_text: str, k: int = 5, filters: dict | None = None) -> list[dict]:
    """Top-k related records for ``query_text`` (empty when unconfigured)."""
    if _deps is None:
        return []
    return _deps.index.retrieve(query_text, k=k, filters=filters)


async def persist() -> None:
    """Persist the index to Redis (best-effort; never raises to the caller)."""
    if _deps and _deps.redis is not None:
        await _deps.redis.set(_deps.dump_key, _deps.index.dump())


async def restore() -> None:
    """Load the index from Redis at startup (best-effort)."""
    if _deps and _deps.redis is not None:
        blob = await _deps.redis.get(_deps.dump_key)
        _deps.index.load(blob)


async def stats() -> dict:
    """Index size + enabled flag (for observability, mirrors semcache.stats)."""
    return {
        "size": len(_deps.index) if _deps else 0,
        "enabled": bool(_deps and _deps.index.enabled),
    }
