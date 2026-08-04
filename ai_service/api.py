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

import re
from dataclasses import dataclass

from fastapi import FastAPI
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from ai_service import langsmith_stats, nodes, ragindex, semcache, settings
from ai_service import docsindex, nodes, ragindex, semcache
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
    # "There is grounding material in `query` itself." Set by the backend when it
    # prepended a scoped record's text (backend/api.py::build_scoped_query), which
    # is read LIVE from Postgres and is more authoritative than anything indexed.
    #
    # Without this flag, "retrieval found nothing" and "there is nothing to answer
    # from" are the same fact — true when the index was the only grounding channel,
    # false once a caller can supply its own. The failure it fixes: click a NOVEL
    # failure (nothing similar indexed, which is exactly when you need help), ask
    # "what does this mean?", and get "No related incidents found ... No sources
    # found in official documentation either" while the alert's full text sits in
    # this very request.
    self_grounded: bool = False


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
    "No sources found in official documentation either."
)


def _snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """First ``limit`` characters of ``text``, ellipsised on a word boundary."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "…"


# What a documentation citation becomes in the prose. A doc id names a chunk of a
# repository the support agent cannot open, so it is noise rather than provenance
# — the useful fact is only that the claim came from documentation rather than
# from log evidence. The UI collapses the doc sources into one "Official
# documentation" chip for the same reason.
DOC_CITATION_TEXT = "the official documentation"

# A trailing "Evidence:"/"Sources:" lead-in left with nothing after it once the
# ids are gone. Swallows the orphaned punctuation with it, so "Evidence: [a], [b]."
# disappears entirely rather than decaying to "Evidence: , ." and then "Evidence.".
#
# Anchored at end-of-string and requires the colon to be followed by punctuation
# ONLY: "Evidence: the margin was below threshold." keeps its real content.
_DANGLING_LEADIN = re.compile(
    r"\s*\b(?:evidence|sources?|citations?|references?|refs?)\b\s*:[\s.,;]*$",
    re.IGNORECASE,
)


def clean_answer_citations(
    answer: str, doc_ids: list[str], record_ids: list[str] | None = None
) -> str:
    """Remove source-record ids from the prose; the UI lists sources separately.

    Record ids are internal identifiers — ``[4d6e9f08-d1e2-4d40-b1f5-8e9d3668dd1d]``
    — of rows the reader cannot look up by id, shown beside the answer as chips
    anyway. In the prose they are pure noise.

    Two different treatments, because the two cases read differently:

    * **documentation ids** are SUBSTITUTED with a phrase, so "as described in
      [jam-ws#escalation]." stays a sentence instead of becoming "as described in ."
      The phrase also carries real meaning: this claim came from documentation
      rather than from log evidence.
    * **alert/journey ids** are DELETED, since no phrase would add anything — the
      answer is already about those records.

    BUSINESS identifiers (``ORD-6426``, ``evt-…``, cart header ids) are untouched:
    an agent works with those daily and they are the whole point of the answer.
    Only ids of retrieved records are removed, and only inside square brackets, so
    an order number can never be caught by this.

    The prompt already asks for no citations, but "do not do X" is a conditional
    instruction that a small/fast deployment follows unreliably — the same reason
    the coverage caveat became a computed field rather than a prompt rule. This is
    the deterministic backstop.
    """
    for doc_id in doc_ids or []:
        answer = answer.replace(f"[{doc_id}]", DOC_CITATION_TEXT)
    # Several doc chunks in one citation list collapse to one mention, so
    # "..., the official documentation, the official documentation" reads right.
    phrase = re.escape(DOC_CITATION_TEXT)
    answer = re.sub(rf"{phrase}(?:\s*(?:,|;|and)\s*{phrase})+", DOC_CITATION_TEXT, answer)

    for record_id in record_ids or []:
        answer = answer.replace(f"[{record_id}]", "")

    return _tidy_after_removal(answer)


def _tidy_after_removal(text: str) -> str:
    """Repair the punctuation a deleted citation leaves behind.

    "Evidence: [a], [b]." would otherwise end up as "Evidence: , ." — visibly
    broken in a way the original id never was.
    """
    text = re.sub(r"\[\s*\]", "", text)                  # emptied brackets
    text = re.sub(r"\(\s*[,;]*\s*\)", "", text)          # emptied parentheses
    # BEFORE the punctuation tidy: that step would eat the colon this depends on,
    # leaving a stranded "Evidence." nothing later can recognise.
    text = _DANGLING_LEADIN.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)               # doubled spaces
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)         # space before punctuation
    text = re.sub(r"([,;:])\s*(?=[.,;:])", "", text)     # stacked separators
    text = re.sub(r"([.!?])[\s.]*\1", r"\1", text)       # ".." from a removed clause
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip(" \t\n,;:")


def build_retrieval_answer(
    query: str, results: list[dict], docs: list[dict] | None = None
) -> str:
    """A deterministic answer assembled from the retrieved records.

    **No LLM here** (by design): the answer is a template listing what was
    retrieved, so the endpoint stays useful with the LLM completely down — which
    is why the response carries ``mode`` and the answer never claims more than
    "here is what I found".

    Documentation chunks are listed under their OWN heading, never merged into the
    incident count: "found 4 related incidents" when three of them are reference
    pages is exactly the confusion the labelled blocks exist to prevent.
    """
    docs = docs or []
    if not results:
        if not docs:
            return NO_RESULTS_ANSWER
        lines = [
            "No related incidents found in the indexed history, but the system "
            f"documentation has {len(docs)} relevant section(s) for: {query.strip()}"
        ]
        lines += _doc_lines(docs)
        return "\n".join(lines)
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
    if docs:
        lines.append(f"Related system documentation ({len(docs)} section(s)):")
        lines += _doc_lines(docs)
    return "\n".join(lines)


def _doc_lines(docs: list[dict]) -> list[str]:
    """One display line per documentation chunk, named by service and section."""
    lines = []
    for i, chunk in enumerate(docs, 1):
        meta = chunk.get("metadata") or {}
        where = " · ".join(str(meta[k]) for k in ("service", "heading") if meta.get(k))
        lines.append(
            f"{i}. [{chunk['id']}] ({where or 'documentation'}) "
            f"(score {chunk['score']:.2f}) {_snippet(chunk['text'])}"
        )
    return lines


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


@app.get("/docs/stats")
async def docs_stats() -> dict:
    """Documentation-index size, per-kind breakdown and why it is off if it is.

    ``enabled: false`` with an ``error`` is the normal shape of every failure mode
    here (no encoder, missing folder, unreadable corpus) — the service starts
    regardless and answers from incident history alone.
    """
    return docsindex.stats()


@app.post("/docs/reload")
async def docs_reload() -> dict:
    """Re-read ``knowledge/`` and rebuild the index without a restart.

    For editing a doc against a running service. In deployment the corpus ships
    inside the image, so changing a doc is a deploy and the documentation version
    always matches the code version (rag-plan.md D4).

    A failed rebuild keeps the previous index rather than emptying it — reloading
    a corpus you have just broken must not take the working one down with it.
    """
    docsindex.reload()
    return docsindex.stats()


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
    """Answer a question from incident history AND system documentation.

    Two grounding channels, retrieved from two separate indexes and kept separate
    all the way into the prompt:

    * **incident history** (``ragindex``) — what happened: alerts and journeys
    * **system documentation** (``docsindex``) — how it works: the service docs

    They are queried independently rather than sharing one top-k, which is the
    whole reason for a second index (rag-plan.md D1): with one shared ``k``, "what
    does the checker do?" loses its own documentation to five checker *failures*.
    Separate budgets GUARANTEE a mix instead of hoping for one.

    Degradation is layered so the endpoint is always useful:

    * any context + LLM ok     → ``mode="ai"``, a grounded narrative answer
    * breaker open / LLM error → ``mode="retrieval-only"``, a deterministic listing
    * nothing at all retrieved → ``mode="retrieval-only"``, says so plainly

    The ``sources`` are the same in every case — only the prose differs — so a UI
    can render citations without branching on mode. Documentation chunks appear in
    that same list with ``kind="doc"``; they carry no journey/order id, so the
    backend's link builder simply renders them without a dashboard link.
    """
    results = ragindex.retrieve(
        req.query, k=req.k, filters=req.filters, boosts=req.boosts
    )
    # Deliberately NOT feedback-blended and NOT filtered by `req.filters`: those
    # filters name incident columns (department, outcome, app_name) that a doc
    # chunk does not carry, so applying them would silently empty this channel.
    docs = docsindex.retrieve(req.query)
    sources = [
        ChatSource(
            id=r["id"],
            kind=r["kind"],
            score=r["score"],
            snippet=_snippet(r["text"]),
            metadata=r.get("metadata") or {},
        )
        for r in [*results, *docs]
    ]

    answer, mode = build_retrieval_answer(req.query, results, docs), RETRIEVAL_ONLY
    # Compose when there is ANY grounding material — incident records, documentation,
    # or context the caller put in the query itself (`self_grounded`). The guard
    # still holds for the case it was written for: a bare question that matched
    # nothing has genuinely nothing to answer from, and the template says so rather
    # than letting the model invent an incident.
    grounded = bool(results) or bool(docs) or req.self_grounded
    if grounded and _deps is not None:
        composed = await _deps.breaker.call(
            lambda: nodes.compose_chat_answer(
                req.query,
                results,
                _deps.chat,
                docs=docs,
                allow_empty_sources=req.self_grounded,
            ),
            fallback=None,
        )
        if composed:
            answer = clean_answer_citations(
                composed, [d["id"] for d in docs], [r["id"] for r in results]
            )
            mode = AI_COMPOSED

    return ChatResponse(
        answer=answer,
        sources=sources,
        mode=mode,
        # Computed, not generated: retrieval filling every slot it was allowed
        # means matches were almost certainly cut off beyond the limit.
        #
        # Describes the INCIDENT channel only, and deliberately so: "truncated"
        # answers "is there more history I did not see?", which is a real risk on
        # an unbounded, ever-growing record set. The docs corpus is small, fixed
        # and authored, so the same word would mean something quite different
        # there — folding both into one number would make the badge meaningless.
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
