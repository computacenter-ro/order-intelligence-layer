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

from ai_service import nodes
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

    Never raises: LLM/breaker problems degrade to a fallback alert.
    """
    app = build_pipeline(deps)
    state: _State = await app.ainvoke({"log": log})
    return _to_alert(log, state)


def _to_alert(log: LogLine, state: _State) -> ProcessedAlert:
    """Assemble the ProcessedAlert from the final pipeline state.

    AI only when we have BOTH an explanation and a department; otherwise a
    fully-null fallback pass-through.
    """
    explanation = state.get("explanation")
    department = state.get("department")
    severity = state.get("severity")
    confidence = state.get("confidence")
    is_ai = not state.get("failed") and explanation is not None and department is not None

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
    )
