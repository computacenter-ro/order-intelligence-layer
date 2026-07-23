"""[3] AI Service — the two LangGraph node functions (CLAUDE.md [3]).

* :func:`explain` — LLM call 1: a plain-English explanation of a WARN/ERROR log
  for an IT-support agent (what happened, which service, likely cause).
* :func:`route`   — LLM call 2: pick one of the five :class:`Department` values
  plus a confidence in [0,1].

Both take a LangChain ``BaseChatModel`` (or ``None``) and are otherwise pure —
no breaker, no Redis, no queues here (the graph owns that). They raise
:class:`LLMError` on any provider failure OR unusable output, so the breaker
has exactly one exception type to count and the caller falls back cleanly.

Design rule (CLAUDE.md): the router must ALWAYS resolve to one of the five
departments. An answer we can't map to the enum is treated as a failure (→
fallback), never coerced into a wrong-but-valid department.
"""
from __future__ import annotations

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from ai_service.llm import LLMError
from shared.models import Department, LogLine, Severity

_DEPARTMENTS = ", ".join(d.value for d in Department)
_SEVERITIES = ", ".join(s.value for s in Severity)

_EXPLAIN_SYSTEM = (
    "You are an assistant for an IT-support engineer triaging logs from an "
    "order-management pipeline. Given one WARN or ERROR log line, explain in "
    "plain English, in 1-3 sentences: what happened, which service it came "
    "from, and the most likely cause. Do not speculate beyond the log. Reply "
    "with the explanation only."
)

_ROUTE_SYSTEM = (
    "You triage an IT-support alert for an order-management pipeline. Do two "
    "things for the single WARN/ERROR log line:\n"
    f"1. Route it to exactly one team. Choose from these departments ONLY: "
    f"{_DEPARTMENTS}.\n"
    f"2. Rate its technical severity as one of: {_SEVERITIES}. Judge how urgent "
    "THIS log is on its own (an ERROR that aborts or dead-letters an order is "
    "more severe than a benign/retryable WARN). Base it on the log only; do not "
    "consider business impact you cannot see.\n"
    'Reply with a single JSON object: {"department": "<one of the list>", '
    '"severity": "<one of the list>", "confidence": <0..1>}. '
    "No prose, no code fence."
)

_SUMMARY_SYSTEM = (
    "You summarize the end-to-end journey of ONE order through a microservice "
    "order-management pipeline, for an IT-support engineer. Given the journey's "
    "outcome and its ordered log lines, write 2-4 plain-English sentences: which "
    "services the order touched, where it stopped, and why. Do not speculate "
    "beyond the logs. Reply with the summary only."
)


def _log_brief(log: LogLine) -> str:
    """The log fields the LLM needs, as a compact prompt block."""
    return (
        f"level={log.level} app_name={log.app_name} logger={log.logger}\n"
        f"message={log.message}"
    )


async def explain(log: LogLine, model: BaseChatModel | None) -> str:
    """LLM call 1. Returns the explanation text; raises LLMError if unavailable."""
    if model is None:
        raise LLMError("no explainer model configured")
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=_EXPLAIN_SYSTEM), HumanMessage(content=_log_brief(log))]
        )
    except Exception as exc:  # provider/network error → let the breaker count it
        raise LLMError(f"explainer call failed: {exc}") from exc

    text = _content_text(resp).strip()
    if not text:
        raise LLMError("explainer returned empty text")
    return text


async def route(
    log: LogLine, explanation: str, model: BaseChatModel | None
) -> tuple[Department, Severity, float]:
    """LLM call 2. Returns (department, severity, confidence); raises LLMError otherwise.

    Both the department and the severity are validated against their enums — an
    answer outside the allowed values is an LLMError, never a silent wrong
    route/rating.
    """
    if model is None:
        raise LLMError("no router model configured")
    prompt = f"{_log_brief(log)}\nexplanation={explanation}"
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=_ROUTE_SYSTEM), HumanMessage(content=prompt)]
        )
    except Exception as exc:
        raise LLMError(f"router call failed: {exc}") from exc

    return _parse_route(_content_text(resp))


async def summarize_journey(
    outcome: str, logs: list[LogLine], model: BaseChatModel | None
) -> str:
    """LLM journey summary. Returns the summary text; raises LLMError otherwise.

    Builds a compact prompt from the journey outcome + its ordered log lines
    (app_name + message each, which is what the narrative needs). The caller
    (api.py) runs this under the shared breaker and falls back to a template.
    """
    if model is None:
        raise LLMError("no summary model configured")
    lines = "\n".join(f"{log.app_name}: {log.message}" for log in logs)
    prompt = f"outcome={outcome}\nlogs:\n{lines}"
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=_SUMMARY_SYSTEM), HumanMessage(content=prompt)]
        )
    except Exception as exc:
        raise LLMError(f"summary call failed: {exc}") from exc
    text = _content_text(resp).strip()
    if not text:
        raise LLMError("summary returned empty text")
    return text


def _parse_route(text: str) -> tuple[Department, Severity, float]:
    """Parse the router's JSON reply into a valid (Department, Severity, confidence).

    Tolerant of a stray code fence / surrounding prose (grabs the first {...}).
    Raises LLMError if the department or severity isn't one of the allowed values
    or the JSON is unusable.
    """
    raw = text.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError(f"router reply not JSON: {raw!r}")
    try:
        data = json.loads(raw[start : end + 1])
    except (ValueError, json.JSONDecodeError) as exc:
        raise LLMError(f"router reply not parseable JSON: {raw!r}") from exc

    dept_str = str(data.get("department", "")).strip().lower()
    try:
        department = Department(dept_str)
    except ValueError as exc:
        raise LLMError(f"router chose an unknown department: {dept_str!r}") from exc

    sev_str = str(data.get("severity", "")).strip().lower()
    try:
        severity = Severity(sev_str)
    except ValueError as exc:
        raise LLMError(f"router chose an unknown severity: {sev_str!r}") from exc

    confidence = _clamp_confidence(data.get("confidence"))
    return department, severity, confidence


def _clamp_confidence(value: object) -> float:
    """Coerce the model's confidence into [0,1]; default 0.5 if missing/bad."""
    try:
        conf = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, conf))


def _content_text(resp: object) -> str:
    """Extract text from a chat-model response (message .content may be a
    string or a list of content blocks in langchain-core 1.x)."""
    content = getattr(resp, "content", resp)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)
