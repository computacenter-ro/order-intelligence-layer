"""[3] AI Service — the LangGraph pipeline (CLAUDE.md [3]).

Wires ``input -> explainer -> router -> ProcessedAlert``. Each LLM call runs
under the shared circuit breaker; if the breaker is open OR a node raises
``LLMError``, the log passes straight through as a ``source="fallback"`` alert
(unexplained, unrouted). This is a **pass-through, not rule-based
classification** — there is deliberately no keyword routing anywhere.

Contract (matches ``ProcessedAlert``): ``source="ai"`` means BOTH LLM calls
succeeded (explanation + a valid department). Anything less — no explainer
model, explainer failure, breaker open, router failure, or a router answer
outside the five departments — yields a clean fallback alert with
``explanation=department=confidence=None``. There are no partial AI alerts.

The chat models and the breaker are injected so the graph is exercised in tests
with a fake model and a fake-redis breaker (no network, no creds).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from ai_service import nodes, semcache
from ai_service.breaker import CircuitBreaker
from shared.models import Department, LogLine, ProcessedAlert, Severity


@dataclass(frozen=True)
class PipelineDeps:
    """Everything the graph needs from the outside world (all injectable)."""

    breaker: CircuitBreaker
    explainer: BaseChatModel | None
    router: BaseChatModel | None


class _State(TypedDict, total=False):
    log: LogLine
    explanation: str | None
    department: Department | None
    severity: Severity | None
    confidence: float | None
    failed: bool          # set once any LLM step fails / is skipped → fallback


def build_pipeline(deps: PipelineDeps):
    """Compile the explainer→router graph bound to ``deps``. Returns a compiled
    app whose ``ainvoke({"log": log})`` yields the final ``_State``."""

    async def explainer_node(state: _State) -> _State:
        log = state["log"]
        explanation = await deps.breaker.call(
            lambda: nodes.explain(log, deps.explainer), fallback=None
        )
        if explanation is None:
            return {"failed": True, "explanation": None}
        return {"explanation": explanation}

    async def router_node(state: _State) -> _State:
        # If the explainer already failed, don't route — go straight to fallback.
        if state.get("failed"):
            return {"department": None, "severity": None, "confidence": None}
        log, explanation = state["log"], state["explanation"]
        result = await deps.breaker.call(
            lambda: nodes.route(log, explanation, deps.router), fallback=None
        )
        if result is None:
            return {"failed": True, "department": None, "severity": None, "confidence": None}
        department, severity, confidence = result
        return {"department": department, "severity": severity, "confidence": confidence}

    graph = StateGraph(_State)
    graph.add_node("explainer", explainer_node)
    graph.add_node("router", router_node)
    graph.add_edge(START, "explainer")
    graph.add_edge("explainer", "router")
    graph.add_edge("router", END)
    return graph.compile()


async def process(log: LogLine, deps: PipelineDeps) -> ProcessedAlert:
    """Run one WARN/ERROR log through the pipeline → a ``ProcessedAlert``.

    Semantic cache short-circuit (CLAUDE.md [3]): BEFORE the breaker/LLM runs,
    look up a near-identical, already-processed log by its normalized message.
    On a hit we build the alert from the cached AI answer (ids re-filled) and
    skip BOTH LLM calls. On a miss we run the pipeline exactly as before, then
    store a successful AI result for reuse. The cache holds only AI answers, so
    a miss while the breaker is open still falls back exactly as today.

    Single flight: the cache is only populated after the LLM returns, so
    concurrent identical logs (the poller runs alerts concurrently, and a
    failure burst emits byte-identical lines) would each miss and each call the
    LLM. The first caller for a normalized key computes; the rest await its
    payload and re-fill ids from their OWN log. Followers whose leader produced
    no reusable answer run the pipeline themselves — the cache still fails
    toward a miss.

    Never raises: LLM/breaker problems degrade to a fallback alert.
    """
    cached_alert = await _try_cache(log)
    if cached_alert is not None:
        await semcache.record_hit()
        return cached_alert

    deps_sc = semcache.get()
    key = semcache.normalize(log.message) if deps_sc is not None else None
    follow = deps_sc.inflight.leader(key) if key is not None else None
    if follow is not None:
        # A task is already computing this exact log type — await its answer
        # instead of duplicating both LLM calls.
        payload = await follow
        alert = _alert_from_payload(log, payload) if payload is not None else None
        if alert is not None:
            await semcache.record_hit()
            return alert
        # Leader had no reusable answer (fallback/breaker/error) → do it myself.
        return await _run_pipeline(log, deps, store=False)

    try:
        return await _run_pipeline(log, deps, store=True)
    finally:
        if key is not None:
            # Publish the leader's OWN stored payload (peek_exact, never the
            # fuzzy cosine path — a lookalike neighbour is not this leader's
            # answer). None when nothing reusable was produced, which sends
            # followers to the pipeline rather than to a wrong answer.
            done = deps_sc.cache.peek_exact(log.message) if deps_sc.cache.enabled else None
            deps_sc.inflight.resolve(key, done)


async def _run_pipeline(log: LogLine, deps: PipelineDeps, *, store: bool) -> ProcessedAlert:
    """Run the explainer→router graph for ``log`` and count it as a cache miss."""
    app = build_pipeline(deps)
    state: _State = await app.ainvoke({"log": log})
    alert = _to_alert(log, state)

    await semcache.record_miss()
    if store:
        await _maybe_store(log, alert)
    return alert


async def _try_cache(log: LogLine) -> ProcessedAlert | None:
    """Build a ProcessedAlert from a cache hit, or ``None`` on a miss.

    The cached department is re-validated against the ``Department`` enum
    defensively (a corrupt persisted payload must never yield an invalid
    alert); an unusable payload is treated as a miss. The explanation's masked
    ids are re-filled with THIS log's ids so the reused text reads correctly.
    """
    deps = semcache.get()
    if deps is None:
        return None
    payload = deps.cache.lookup(log.message)
    if payload is None:
        return None
    return _alert_from_payload(log, payload)


def _alert_from_payload(log: LogLine, payload: semcache.CachePayload) -> ProcessedAlert | None:
    """Build a cached-hit alert for ``log`` from a reusable payload.

    Shared by the cache-hit path and the single-flight follower path — both
    reuse another log's answer, and both must re-fill ids from THIS log. The
    department is re-validated against the enum defensively (a corrupt persisted
    payload must never yield an invalid alert); ``None`` means "unusable, run
    the pipeline instead".
    """
    try:
        department = Department(payload.department)
    except (ValueError, TypeError):
        return None  # corrupt payload → miss, run the pipeline
    severity = None
    if payload.severity is not None:
        try:
            severity = Severity(payload.severity)
        except (ValueError, TypeError):
            severity = None
    explanation = semcache.refill(payload.normalized_explanation, log)
    deps_sc = semcache.get()
    embedding = deps_sc.cache.embed(log.message) if deps_sc is not None else None
    return ProcessedAlert(
        alert_id=str(uuid.uuid4()),
        emitted_at=datetime.now(timezone.utc),
        log=log,
        explanation=explanation,
        department=department,
        severity=severity,
        confidence=payload.confidence,
        source="ai",       # a cache hit is still an AI answer (routing unchanged)
        cached=True,
        embedding=embedding,
    )


async def _maybe_store(log: LogLine, alert: ProcessedAlert) -> None:
    """Store a successful (non-cached) AI alert for later reuse.

    Only ``source="ai"`` results are cached — fallback pass-throughs are not an
    answer worth reusing. The explanation is stored in NORMALIZED form (ids
    masked) so :func:`semcache.refill` can substitute each future log's ids.
    """
    deps = semcache.get()
    if deps is None or alert.source != "ai" or alert.cached:
        return
    if alert.explanation is None or alert.department is None:
        return
    payload = semcache.CachePayload(
        normalized_explanation=semcache.normalize(alert.explanation),
        department=alert.department.value,
        severity=alert.severity.value if alert.severity is not None else None,
        confidence=alert.confidence,
    )
    deps.cache.store(log.message, payload)
    await semcache.persist()


def _to_alert(log: LogLine, state: _State) -> ProcessedAlert:
    """Assemble the ProcessedAlert from the final pipeline state.

    AI only when we have BOTH an explanation and a department; otherwise a
    fully-null fallback pass-through. The embedding is attached on BOTH
    branches — it's entirely local (no LLM involved), so it must keep working
    even when everything else fell back (CLAUDE.md: "must remain useful with
    the LLM completely down").
    """
    explanation = state.get("explanation")
    department = state.get("department")
    severity = state.get("severity")
    confidence = state.get("confidence")
    is_ai = not state.get("failed") and explanation is not None and department is not None

    deps_sc = semcache.get()
    embedding = deps_sc.cache.embed(log.message) if deps_sc is not None else None

    if is_ai:
        return ProcessedAlert(
            alert_id=str(uuid.uuid4()),
            emitted_at=datetime.now(timezone.utc),
            log=log,
            explanation=explanation,
            department=department,
            severity=severity,
            confidence=confidence,
            source="ai",
            embedding=embedding,
        )
    return ProcessedAlert(
        alert_id=str(uuid.uuid4()),
        emitted_at=datetime.now(timezone.utc),
        log=log,
        explanation=None,
        department=None,
        severity=None,
        confidence=None,
        source="fallback",
        embedding=embedding,
    )
