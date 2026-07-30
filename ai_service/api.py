"""[3] AI Service — journey summary API (CLAUDE.md [3] "Journey summary API").

``POST /summarize-journey`` — called by the backend **on journey completion**.
Body: journey meta + the journey's ordered raw logs. Returns an LLM-written
summary (services touched, where it stopped, why). Same shared circuit breaker
as the pipeline; when the LLM is down it returns a deterministic **template**
summary built from the journey meta, with ``source="fallback"`` — so a
completed journey always gets *some* summary, LLM or not.

This module OWNS the request/response contract (``SummaryRequest`` /
``SummaryResponse``). The backend must serialize its request to match
``SummaryRequest`` — this is the one forward-coupling between [3] and [5].

The FastAPI app takes its summary model + breaker from module-level dependency
holders so tests can inject fakes; ``main.py`` wires the real ones at startup.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from ai_service import langsmith_stats, nodes, ragindex, semcache, settings
from ai_service.breaker import CircuitBreaker
from shared.models import LogLine


# --- request/response contract (the seam with the backend) -------------------
class SummaryRequest(BaseModel):
    """What the backend POSTs on journey completion.

    ``logs`` are the journey's raw log lines in timestamp order. The id fields
    are the journey's accumulated aliases (any may be null — a pre-creation
    failure has only ``event_id``); they contextualize the summary.
    """

    journey_id: str
    outcome: str                       # SUCCESS / <FAILED subtype> / TIMED_OUT
    event_id: str | None = None
    order_id: str | None = None
    cart_header_id: str | None = None
    logs: list[LogLine]


class SummaryResponse(BaseModel):
    journey_id: str
    summary: str
    source: str                        # "ai" | "fallback"
    suggested_label: str | None = None


# --- retrieval contract (the seam with the backend + future chatbot) ----------
class IndexRequest(BaseModel):
    """One record for the retrieval index, POSTed by the backend.

    The backend owns the DB and decides what is worth indexing and filtering on;
    the AI service owns the encoder. ``metadata`` is free-form so a new filter
    key needs no change here.
    """

    id: str
    kind: str                          # "alert" | "journey"
    text: str
    metadata: dict = {}


class IndexResponse(BaseModel):
    indexed: bool


class ChatRequest(BaseModel):
    query: str
    k: int = 5
    filters: dict | None = None
    # Per-record feedback scores in (0,1), supplied by the CALLER. The backend owns
    # the votes (they are user data, in Postgres); this service owns the index and
    # must stay DB-free, so the counts arrive with the request. Absent/empty means
    # rank on relevance alone.
    boosts: dict[str, float] = {}


class ChatSource(BaseModel):
    """One retrieved record, trimmed for display.

    ``metadata`` is passed through from the index so the caller can build links
    and badges without a second lookup — the backend uses ``journey_id`` /
    ``order_id`` from here to attach a dashboard URL to each citation.
    """

    id: str
    kind: str
    score: float
    snippet: str
    metadata: dict = {}


class ChatCoverage(BaseModel):
    """How much of the history the answer is based on — computed, never generated.

    Top-k retrieval has no notion of coverage: several alerts about one incident
    can crowd out a second, distinct incident that also matched (measured — two
    journeys failed SAP submission, but at k=3 only one was retrieved, the other
    ranking 6th behind four alerts). An answer built from that sample can read as
    if it described the whole history.

    The server knows these numbers exactly, so it reports them instead of asking
    the model to. Two earlier prompt-based attempts were followed only about half
    the time in each direction — caveats appeared on cause questions where they
    were noise and vanished from counting questions where they mattered. A field
    is deterministic, costs no tokens, and a UI can render it as a badge.

    ``truncated`` is the one a caller should act on: True means the limit was hit
    and other matching incidents very likely exist beyond it.
    """

    shown: int                         # records returned to the caller
    limit: int                         # the k that was applied
    truncated: bool                    # shown == limit, so more may exist


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSource]
    # "ai"             — the answer was composed by the LLM, grounded in sources
    # "retrieval-only" — deterministic template (LLM down/absent, or no sources)
    # The sources are IDENTICAL either way; only the prose differs, so a caller
    # can always render citations regardless of mode.
    mode: str
    coverage: ChatCoverage


# --- injectable dependencies -------------------------------------------------
@dataclass
class SummaryDeps:
    breaker: CircuitBreaker
    model: BaseChatModel | None
    # Grounded-chat model for POST /chat. Defaulted to None so every existing
    # construction site (and test) keeps working and simply gets the
    # retrieval-only answer — the same degradation as an LLM outage.
    chat: BaseChatModel | None = None


_deps: SummaryDeps | None = None


def configure(deps: SummaryDeps) -> None:
    """Install the runtime dependencies (called by main.py / tests)."""
    global _deps
    _deps = deps


# --- template fallback (deterministic, no LLM) -------------------------------
def template_summary(req: SummaryRequest) -> str:
    """A plain summary built from journey meta when the LLM is unavailable.

    Deterministic and dependency-free — this is the "useful with the LLM
    completely down" guarantee applied to journey summaries.
    """
    services: list[str] = []
    for log in req.logs:
        if log.app_name not in services:
            services.append(log.app_name)
    touched = ", ".join(services) if services else "no services"
    ident = req.order_id or req.event_id or req.cart_header_id or req.journey_id
    return (
        f"Order {ident} ended with outcome {req.outcome}. "
        f"It touched {len(services)} service(s): {touched}. "
        f"({len(req.logs)} log line(s); LLM summary unavailable.)"
    )


# --- retrieval-only answer (deterministic, no LLM) ---------------------------
SNIPPET_CHARS = 200
RETRIEVAL_ONLY = "retrieval-only"
AI_COMPOSED = "ai"

NO_RESULTS_ANSWER = (
    "No related incidents found in the indexed history for that question. "
    "The index may not yet contain matching alerts or journeys — "
    "run the backfill (python -m backend.scripts.backfill_rag) if it looks empty."
)


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """First ``limit`` characters of ``text``, ellipsised on a word boundary."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


def build_retrieval_answer(query: str, results: list[dict]) -> str:
    """A deterministic answer assembled from the retrieved records.

    **No LLM in this phase** (by design): the answer is a template listing what
    was retrieved, so the endpoint is useful and fully testable before any
    generation step exists. A later phase can replace this function with an LLM
    call and keep the request/response contract — which is why the response
    carries ``mode`` and the answer never claims more than "here is what I found".
    """
    if not results:
        return NO_RESULTS_ANSWER
    kinds = {"alert": 0, "journey": 0}
    for record in results:
        kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
    parts = [f"{kinds.get('alert', 0)} alert(s)", f"{kinds.get('journey', 0)} journey(s)"]
    lines = [
        f"Found {len(results)} related incident(s) ({', '.join(parts)}) for: {query.strip()}"
    ]
    for i, record in enumerate(results, 1):
        lines.append(
            f"{i}. [{record['kind']} {record['id']}] "
            f"(score {record['score']:.2f}) {_snippet(record['text'])}"
        )
    return "\n".join(lines)


# --- app ---------------------------------------------------------------------
app = FastAPI(title="AI Service — Journey Summary API")


@app.get("/health")
async def health() -> dict[str, str]:
    llm = "up" if (_deps and _deps.model is not None) else "fallback"
    return {"status": "ok", "llm": llm}


@app.get("/semcache/stats")
async def semcache_stats() -> dict:
    """Current semantic-cache hit/miss counters + hit rate (the demo number)."""
    return await semcache.stats()


@app.get("/ragindex/stats")
async def ragindex_stats() -> dict:
    """Retrieval-index size + enabled flag."""
    return await ragindex.stats()


@app.get("/llm-stats")
async def llm_stats(window: str = langsmith_stats.DEFAULT_WINDOW) -> dict:
    """Per-logical-model LangSmith stats + what the semantic cache saved.

    ``window`` is one of ``1h`` / ``24h`` / ``7d``; anything else is treated as the
    default rather than rejected (see ``langsmith_stats.resolve_window``), so a
    typo'd param still renders a dashboard.

    **A pure snapshot read.** LangSmith is queried by a background refresher, never
    by this handler, so no amount of clicking can produce a request to the provider
    (that click-driven volume was what triggered its rate limit).

    Never 500s: with no LangSmith creds every node is ``None`` and
    ``estimated_saved_usd`` is ``null``, which is the honest answer rather than an
    error or a fabricated figure.

    ``fetched_at`` and ``langsmith_configured`` exist so a null node can be
    EXPLAINED rather than guessed at. The three reasons a node is null are
    different facts and the server is the only side that knows which applies:
    nothing collected yet (``fetched_at`` null → "Collecting…"), no credentials
    (``langsmith_configured`` false → "not configured"), or a cycle ran and that
    tag's query failed. Reporting all of them as "not configured yet" — which is
    what the dashboard did before — was a lie in two cases out of three.

    ``refresh_interval_s`` is the configured refresh period. The dashboard needs it
    for two things it cannot otherwise know: telling the reader when the next update
    is due, and deciding when a timestamp is old enough that the background
    refresher has probably died. Hardcoding a guess in the frontend would silently
    break the health check the moment the interval is retuned — which is exactly
    what someone does after a rate-limit incident.
    """
    nodes_stats = await langsmith_stats.node_stats(window)
    return {
        "window": window,
        "nodes": nodes_stats,
        "fetched_at": langsmith_stats.fetched_at_iso(),
        "refresh_interval_s": settings.LLM_STATS_REFRESH_INTERVAL_SECONDS,
        "langsmith_configured": settings.langsmith_configured(),
        # Passing the already-fetched stats keeps this to ONE LangSmith round of
        # queries per request, and guarantees the savings estimate is computed
        # over the same window the caller asked for.
        "cache_savings": await langsmith_stats.cache_savings(nodes_stats),
    }


@app.post("/index", response_model=IndexResponse)
async def index(req: IndexRequest) -> IndexResponse:
    """Embed + store one incident record (upsert by id).

    ``indexed=false`` is a normal outcome, not an error: the index self-disables
    when the encoder is unavailable, and the backend pushes fire-and-forget, so a
    200 with ``false`` lets the caller carry on without special-casing failure.
    """
    stored = await ragindex.index_record(req.id, req.kind, req.text, req.metadata)
    return IndexResponse(indexed=stored)


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    """Answer a question from retrieved incident history, grounded by the LLM.

    Retrieve top-k (phase 1) → compose an answer from those records ONLY, under
    the SHARED circuit breaker. Degradation is layered so the endpoint is always
    useful:

    * sources + LLM ok        → ``mode="ai"``, a grounded narrative answer
    * breaker open / LLM error → ``mode="retrieval-only"``, the phase-1 template
    * nothing retrieved        → ``mode="retrieval-only"``, "no related incidents"

    The ``sources`` are the same in every case — only the prose differs — so a UI
    can render citations without branching on mode. This is the chatbot's share
    of the system-wide "useful with the LLM completely down" guarantee: an
    outage costs you the narrative, never the search.
    """
    results = ragindex.retrieve(
        req.query, k=req.k, filters=req.filters, boosts=req.boosts
    )
    sources = [
        ChatSource(
            id=r["id"],
            kind=r["kind"],
            score=r["score"],
            snippet=_snippet(r["text"]),
            metadata=r.get("metadata") or {},
        )
        for r in results
    ]

    answer, mode = build_retrieval_answer(req.query, results), RETRIEVAL_ONLY
    # Only attempt composition when there is something to ground in — with no
    # sources the model has nothing to answer from, and the template already says
    # so honestly. (compose_chat_answer also refuses, belt and braces.)
    if results and _deps is not None:
        composed = await _deps.breaker.call(
            lambda: nodes.compose_chat_answer(req.query, results, _deps.chat),
            fallback=None,
        )
        if composed:
            answer, mode = composed, AI_COMPOSED

    return ChatResponse(
        answer=answer,
        sources=sources,
        mode=mode,
        # Computed, not generated: retrieval filling every slot it was allowed
        # means matches were almost certainly cut off beyond the limit.
        coverage=ChatCoverage(
            shown=len(results), limit=req.k, truncated=len(results) >= req.k
        ),
    )


@app.post("/summarize-journey", response_model=SummaryResponse)
async def summarize_journey(req: SummaryRequest) -> SummaryResponse:
    """Summarize a completed journey (LLM, or template when the LLM is down)."""
    if _deps is None:  # pragma: no cover - guards misconfiguration
        raise RuntimeError("api.configure() must be called before serving")

    result = await _deps.breaker.call(
        lambda: nodes.summarize_journey(req.outcome, req.logs, _deps.model),
        fallback=None,
    )
    if result is None:
        return SummaryResponse(
            journey_id=req.journey_id, summary=template_summary(req), source="fallback"
        )
    return SummaryResponse(
        journey_id=req.journey_id, summary=result.summary, source="ai",
        suggested_label=result.suggested_label,
    )
