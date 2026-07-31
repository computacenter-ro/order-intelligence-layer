"""[3] AI Service — the system-documentation index (docs RAG).

The second grounding channel. The incident index (:mod:`ai_service.ragindex`)
holds **things that happened**; this one holds **how things work** — the
per-service docs in ``ai_service/knowledge/``.

Why a SEPARATE index rather than a new ``kind`` in ``ragindex`` (rag-plan.md D1),
first reason decisive:

1. ``ragindex`` evicts oldest-first once past ``RAGINDEX_MAX_ENTRIES``. Docs load
   once at startup, so they are permanently the OLDEST entries and would be
   silently evicted within days by the continuous alert stream — no error, no
   symptom except a chatbot that quietly stops knowing what JAM is.
2. One shared ``k`` makes doc chunks and alerts compete. "What does the checker
   do?" loses its own documentation to five checker *failures*.
3. ``RAGINDEX_MIN_SCORE`` is a recall floor tuned for incident history.
4. Feedback boosts would let a downvote on a badly-worded answer demote correct
   reference material — so this index is queried with ``feedback_weight=0``.

Separating also lets a caller GUARANTEE a mix (n doc chunks + m incident records)
instead of hoping one top-k contains both.

The store itself is :class:`~ai_service.ragindex.RagIndex` — same class, different
settings — because the only real differences are the cap, the floor and the
absence of feedback, all of which are already parameters. What is new here is the
BUILD: chunks come from files on disk (:mod:`ai_service.knowledge_loader`), not
from the backend pushing records up. Docs were never in Postgres, so this is a
third, simpler path alongside the existing one: *files own themselves*.

**No persistence, deliberately** (rag-plan.md D4/§8). The index rebuilds from the
files in seconds, so storing it in Redis would create a second copy that can drift
from the source of truth. No Redis key, no migration, no backfill script.

Self-disabling, like semcache and ragindex: no encoder, no ``knowledge/`` folder,
or an unreadable corpus all leave an empty index and an assistant that answers
from incident history alone — never a crash at startup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ai_service import knowledge_loader as kl
from ai_service import knowledge_routing as kr
from ai_service import settings
from ai_service.ragindex import RagIndex

__all__ = [
    "DocsDeps",
    "build",
    "configure",
    "get",
    "reload",
    "retrieve",
    "stats",
]


@dataclass
class DocsDeps:
    index: RagIndex
    registry: kr.Registry
    knowledge_dir: Path
    chunks: int = 0
    error: str | None = None
    kinds: dict[str, int] = field(default_factory=dict)


_deps: DocsDeps | None = None


def build(knowledge_dir: str | Path, encoder) -> DocsDeps:
    """Chunk the corpus and embed it. Never raises — a failure disables the index.

    ``max_entries`` is set from the corpus size itself: the cap exists in
    :class:`RagIndex` for the incident stream, and here it must never evict,
    because every entry was deliberately authored rather than accumulated.
    """
    knowledge_dir = Path(knowledge_dir)
    empty = DocsDeps(
        index=RagIndex(None, min_score=settings.DOCSINDEX_MIN_SCORE, max_entries=1),
        registry=kr.Registry(),
        knowledge_dir=knowledge_dir,
    )
    if not knowledge_dir.is_dir():
        empty.error = f"knowledge dir not found: {knowledge_dir}"
        return empty
    try:
        chunks = kl.load_corpus(knowledge_dir)
        registry = kr.build_registry(knowledge_dir)
    except Exception as exc:  # noqa: BLE001 — a bad doc must not stop the service
        empty.error = f"corpus load failed: {exc}"
        return empty
    if not chunks:
        empty.error = "corpus is empty"
        empty.registry = kr.Registry()
        return empty

    index = RagIndex(
        encoder,
        min_score=settings.DOCSINDEX_MIN_SCORE,
        max_entries=len(chunks) + 1,  # never evict authored content
    )
    stored = 0
    for chunk in chunks:
        if index.index(chunk.id, "doc", chunk.text, chunk.metadata):
            stored += 1
    kinds: dict[str, int] = {}
    for chunk in chunks:
        kinds[chunk.kind or "unlabelled"] = kinds.get(chunk.kind or "unlabelled", 0) + 1
    return DocsDeps(
        index=index,
        registry=registry,
        knowledge_dir=knowledge_dir,
        chunks=stored,
        # No encoder means the chunks were parsed but not embedded — worth saying
        # so explicitly, since "0 stored, 274 parsed" is a very different problem
        # from "the folder is missing".
        error=None if stored else "no encoder — chunks parsed but not embedded",
        kinds=kinds,
    )


def configure(deps: DocsDeps | None) -> None:
    """Install the runtime index (called by main.py / tests)."""
    global _deps
    _deps = deps


def get() -> DocsDeps | None:
    return _deps


def reload() -> DocsDeps | None:
    """Re-read the folder and rebuild in place (POST /docs/reload).

    Keeps the CURRENT encoder — reloading is about changed documents, never about
    changing models. The rebuild replaces the index atomically, so a failed reload
    leaves the previous, working index untouched rather than emptying it.
    """
    if _deps is None:
        return None
    rebuilt = build(_deps.knowledge_dir, _deps.index._encoder)
    if rebuilt.chunks:
        configure(rebuilt)
        return rebuilt
    return _deps


def retrieve(query: str, k: int | None = None) -> list[dict]:
    """Top-k doc chunks for ``query``, narrowed by service and question kind.

    Returns ``[]`` when the index is unconfigured or disabled — an empty result is
    a normal answer here, exactly as in ``ragindex``. Feedback is deliberately NOT
    blended (see the module docstring): a downvote rates the ANSWER, and must not
    demote a correct reference page.
    """
    if _deps is None or not _deps.index.enabled:
        return []
    return kr.retrieve_docs(
        _deps.index,
        query,
        _deps.registry,
        k=settings.DOCSINDEX_K if k is None else k,
        kind_mode=settings.DOCSINDEX_KIND_MODE,
    )


def stats() -> dict:
    """Index size + health, for GET /docs/stats (mirrors ragindex.stats)."""
    if _deps is None:
        return {"enabled": False, "chunks": 0, "services": [], "error": "not configured"}
    return {
        "enabled": bool(_deps.index.enabled and _deps.chunks),
        "chunks": _deps.chunks,
        "services": sorted(_deps.registry.documented),
        "kinds": dict(sorted(_deps.kinds.items())),
        "error": _deps.error,
    }
