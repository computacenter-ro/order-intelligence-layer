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

from ai_service import nodes
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


# --- injectable dependencies -------------------------------------------------
@dataclass
class SummaryDeps:
    breaker: CircuitBreaker
    model: BaseChatModel | None


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


# --- app ---------------------------------------------------------------------
app = FastAPI(title="AI Service — Journey Summary API")


@app.get("/health")
async def health() -> dict[str, str]:
    llm = "up" if (_deps and _deps.model is not None) else "fallback"
    return {"status": "ok", "llm": llm}


@app.post("/summarize-journey", response_model=SummaryResponse)
async def summarize_journey(req: SummaryRequest) -> SummaryResponse:
    """Summarize a completed journey (LLM, or template when the LLM is down)."""
    if _deps is None:  # pragma: no cover - guards misconfiguration
        raise RuntimeError("api.configure() must be called before serving")

    summary = await _deps.breaker.call(
        lambda: nodes.summarize_journey(req.outcome, req.logs, _deps.model),
        fallback=None,
    )
    if summary is None:
        return SummaryResponse(
            journey_id=req.journey_id, summary=template_summary(req), source="fallback"
        )
    return SummaryResponse(journey_id=req.journey_id, summary=summary, source="ai")
