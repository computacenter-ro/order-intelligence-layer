"""[3] AI Service — composition root (``python -m ai_service.main``).

The one place the REAL dependencies are constructed and wired together — the
async ``redis`` client, the RabbitMQ ``Publisher``, the shared circuit breaker,
and the Explainer/Router chat models (via ``llm.py``) — then the poller loop is
started. Everything below the composition root takes its dependencies injected,
so this module is the only one that touches live infra.

Running with NO Azure credentials is fully supported: ``llm.py`` returns ``None``
chat models, the pipeline takes the fallback path, and the service still
publishes raw events and (fallback) alerts. That is the "useful with the LLM
completely down" guarantee (CLAUDE.md [3]).

(The ``/summarize-journey`` + ``/health`` FastAPI app is added in slice 5 and
will be served on :8100 alongside this loop.)
"""
from __future__ import annotations

import asyncio
from contextlib import suppress

import redis.asyncio as aioredis
import uvicorn

from ai_service import api, langsmith_stats, llm, ragindex, semcache, settings, docsindex
from ai_service.breaker import CircuitBreaker
from ai_service.graph import PipelineDeps
from ai_service.poller import Poller
from ai_service.publisher import Publisher
from ai_service.ragindex import RagDeps, RagIndex
from ai_service.semcache import SemanticCache, SemCacheDeps


async def _run() -> None:
    redis_client = aioredis.from_url(settings.REDIS_URL)
    publisher = await Publisher().connect()
    deps = PipelineDeps(
        breaker=CircuitBreaker(redis_client),
        explainer=llm.explainer_model(),
        router=llm.router_model(),
    )

    # Semantic cache: load the local embedding model ONCE, share the existing
    # Redis for its dump + hit/miss counters, and restore any persisted entries.
    encoder = semcache.load_encoder(settings.SEMCACHE_MODEL) if settings.SEMCACHE_ENABLED else None
    semcache.configure(
        SemCacheDeps(
            cache=SemanticCache(
                encoder,
                threshold=settings.SEMCACHE_THRESHOLD,
                max_entries=settings.SEMCACHE_MAX_ENTRIES,
                salient_words=settings.SEMCACHE_SALIENT_WORDS,
                guard=settings.SEMCACHE_GUARD,
            ),
            redis=redis_client,
            dump_key=settings.SEMCACHE_KEY,
            hits_key=settings.SEMCACHE_HITS_KEY,
            misses_key=settings.SEMCACHE_MISSES_KEY,
        )
    )
    await semcache.restore()

    # Retrieval index (RAG phase 1): REUSES the semantic cache's encoder when the
    # two are configured with the same model — one sentence-transformers load
    # serves both, which is what keeps startup at a single model download. Only
    # load a second encoder if RAGINDEX_MODEL was deliberately pointed elsewhere.
    if not settings.RAGINDEX_ENABLED:
        rag_encoder = None
    elif encoder is not None and settings.RAGINDEX_MODEL == settings.SEMCACHE_MODEL:
        rag_encoder = encoder
    else:
        rag_encoder = ragindex.load_encoder(settings.RAGINDEX_MODEL)
    ragindex.configure(
        RagDeps(
            index=RagIndex(
                rag_encoder,
                min_score=settings.RAGINDEX_MIN_SCORE,
                max_entries=settings.RAGINDEX_MAX_ENTRIES,
            ),
            redis=redis_client,
            dump_key=settings.RAGINDEX_KEY,
        )
    )
    await ragindex.restore()

    # LangSmith stats: same posture as the semantic cache — share the existing
    # Redis for a rebuildable snapshot, restore it before the first cycle so a
    # restart doesn't leave the AI-performance page blank. GET /llm-stats only ever
    # reads that snapshot; the refresher task below is the only thing in the
    # process that talks to LangSmith.
    langsmith_stats.configure(redis_client)
    await langsmith_stats.restore()

    # Documentation index (docs RAG): the SECOND grounding channel. Built from
    # files on disk, not pushed up from Postgres, and NOT persisted — it rebuilds
    # from the corpus in seconds, so a Redis copy could only drift from the source
    # of truth. Reuses the same encoder again (fourth consumer, still one load).
    if not settings.DOCSINDEX_ENABLED:
        docs_encoder = None
    elif rag_encoder is not None and settings.DOCSINDEX_MODEL == settings.RAGINDEX_MODEL:
        docs_encoder = rag_encoder
    elif encoder is not None and settings.DOCSINDEX_MODEL == settings.SEMCACHE_MODEL:
        docs_encoder = encoder
    else:
        docs_encoder = semcache.load_encoder(settings.DOCSINDEX_MODEL)
    docs_deps = docsindex.build(settings.DOCSINDEX_DIR, docs_encoder)
    docsindex.configure(docs_deps)
    if docs_deps.error:
        # Never fatal: the assistant degrades to incident-only answers.
        print(f"[ai_service] docs index disabled — {docs_deps.error}", flush=True)

    poller = Poller(redis=redis_client, publisher=publisher, pipeline_deps=deps)

    # The summary API shares the same breaker + Redis; its model is the stronger
    # summary deployment (None with no creds → template fallback).
    # The summary API and grounded /chat share this breaker with each other (one
    # provider, one outage, one breaker — CLAUDE.md). chat_model() falls back to
    # the summary deployment when AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT is unset.
    api.configure(
        api.SummaryDeps(
            breaker=CircuitBreaker(redis_client),
            model=llm.summary_model(),
            chat=llm.chat_model(),
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(
            api.app, host=settings.API_HOST, port=settings.API_PORT, log_level="info"
        )
    )

    mode = "AI" if settings.llm_configured() else "FALLBACK (no Azure creds)"
    cache_mode = "on" if (encoder is not None) else "off"
    rag_mode = "on" if (rag_encoder is not None) else "off"
    docs_mode = f"{docs_deps.chunks} chunks" if docs_deps.chunks else "off"
    print(
        f"[ai_service] started — poll every {settings.POLL_INTERVAL}s, "
        f"window [-{settings.WINDOW_START_OFFSET}s, -{settings.WINDOW_END_OFFSET}s], "
        f"API on :8100, LLM mode: {mode}, semantic cache: {cache_mode}, "
        f"retrieval index: {rag_mode}, docs index: {docs_mode}",
        flush=True,
    )
    # An explicit task rather than a bare coroutine in the gather, so the finally
    # below can stop it BEFORE the Redis client it persists through is closed.
    refresher = asyncio.create_task(langsmith_stats.run_refresher())
    try:
        # Poller loop + summary API + llm-stats refresher on one event loop.
        await asyncio.gather(poller.run(), server.serve(), refresher)
    finally:
        # Cancelled first, and awaited so the cancellation has actually landed:
        # an in-flight persist() against an already-closed Redis client would log
        # a confusing error on every shutdown.
        refresher.cancel()
        with suppress(asyncio.CancelledError):
            await refresher
        await poller.aclose()
        await publisher.close()
        await redis_client.aclose()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover
        print("\n[ai_service] shutting down", flush=True)


if __name__ == "__main__":
    main()
