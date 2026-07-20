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

import redis.asyncio as aioredis
import uvicorn

from ai_service import api, llm, settings
from ai_service.breaker import CircuitBreaker
from ai_service.graph import PipelineDeps
from ai_service.poller import Poller
from ai_service.publisher import Publisher


async def _run() -> None:
    redis_client = aioredis.from_url(settings.REDIS_URL)
    publisher = await Publisher().connect()
    deps = PipelineDeps(
        breaker=CircuitBreaker(redis_client),
        explainer=llm.explainer_model(),
        router=llm.router_model(),
    )
    poller = Poller(redis=redis_client, publisher=publisher, pipeline_deps=deps)

    # The summary API shares the same breaker + Redis; its model is the stronger
    # summary deployment (None with no creds → template fallback).
    api.configure(
        api.SummaryDeps(
            breaker=CircuitBreaker(redis_client), model=llm.summary_model()
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(api.app, host="0.0.0.0", port=8100, log_level="info")
    )

    mode = "AI" if settings.llm_configured() else "FALLBACK (no Azure creds)"
    print(
        f"[ai_service] started — poll every {settings.POLL_INTERVAL}s, "
        f"window [-{settings.WINDOW_START_OFFSET}s, -{settings.WINDOW_END_OFFSET}s], "
        f"API on :8100, LLM mode: {mode}",
        flush=True,
    )
    try:
        # Poller loop + summary API on one event loop.
        await asyncio.gather(poller.run(), server.serve())
    finally:
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
