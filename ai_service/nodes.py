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
from dataclasses import dataclass

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

# Hand-written department semantics. The ALLOWED list above is generated from the
# Department enum, but these definitions are NOT — adding a department to the enum
# updates the list automatically and silently leaves this block stale. Keep the two
# in sync by hand.
#
# The load-bearing distinction is backend vs general: "the code misbehaved" vs "the
# code behaved correctly and rejected the order". Without it the model routes on
# surface association (log came from a service -> services are code -> backend), which
# put every business-rule rejection on the backend team's queue as phantom work.
_DEPARTMENT_GUIDE = (
    "- networking: connectivity between services — timeouts, unreachable hosts, "
    "HTTP transport failures, a downstream service not answering.\n"
    "- devops: message-queue and infrastructure plumbing — redeliveries, retry "
    "exhaustion, dead-letter (DLQ/_error) routing.\n"
    "- database: persistence failures — DB timeouts, connection pools, SQL errors.\n"
    "- backend: an application code or integration DEFECT — the service itself "
    "behaved wrongly (unexpected exception, bad payload it produced, broken logic).\n"
    "- general: NOT an engineering fault. The pipeline worked exactly as designed "
    "and correctly REJECTED an order because of a business rule, user-supplied "
    "data, reference data, or account configuration. Nobody needs to change any "
    "code. Route here even though the log came from a service, is level=ERROR, "
    "and says FAILED or aborted.\n"
)

# Examples are taken verbatim from the emitters' real message shapes so they match
# at inference. Deliberately paired: each business-rule 'general' case sits next to a
# genuine technical failure that looks similar on the surface, because the contrast
# is what teaches the boundary — a list of general-only examples would just bias the
# model toward general.
_ROUTE_EXAMPLES = (
    "Examples:\n"
    'message=Margin check FAILED for order ORD-6042: overall margin 8.10% below '
    'threshold 12.00% -> {"department": "general", "severity": "medium", '
    '"confidence": 0.9}  (the checker worked; the order is simply unprofitable)\n'
    "message=Validation failed: mandatory UDF 'costCenter' missing on line 2 -> "
    '{"department": "general", "severity": "medium", "confidence": 0.9}  '
    "(user-supplied data is incomplete; no defect)\n"
    "message=Authentication failed for user RFLORIA: account disabled in JAM -> "
    '{"department": "general", "severity": "medium", "confidence": 0.88}  '
    "(access administration, not code)\n"
    "message=Order processing aborted for order ORD-6108: SPT price list service "
    'unavailable after 3 attempt(s) -> {"department": "networking", "severity": '
    '"high", "confidence": 0.9}  (a real dependency outage)\n'
    # Paired against the timeout above: both are raw HTTP client lines, so the
    # transport framing is identical and only the STATUS separates them. A 403 means
    # the call reached the server and was refused — an authorization decision, not a
    # connectivity fault. Without this pair the model routes 403s to networking on
    # the strength of "HTTP/1.1" alone.
    "message=[JamClient#getUserProfileWithPrivilegesBySamAccountName] <--- "
    'HTTP/1.1 403 (250ms) -> {"department": "general", "severity": "medium", '
    '"confidence": 0.85}  (the call SUCCEEDED and was refused — an access '
    "decision; a 4xx authorization refusal is never a networking fault, whereas a "
    "timeout or connection error is)\n"
    "message=Order creation failed for event evt-1a2b: DB_TIMEOUT — no order was "
    'created -> {"department": "database", "severity": "critical", "confidence": '
    "0.92}\n"
    "message=Max redelivery attempts reached for event evt-1a2b; routing message "
    'to order.inbound.dlq -> {"department": "devops", "severity": "high", '
    '"confidence": 0.9}\n'
)

_ROUTE_SYSTEM = (
    "You triage an IT-support alert for an order-management pipeline. Do two "
    "things for the single WARN/ERROR log line:\n"
    f"1. Route it to exactly one team. Choose from these departments ONLY: "
    f"{_DEPARTMENTS}.\n"
    f"{_DEPARTMENT_GUIDE}"
    "   Before choosing, ask: is anything actually BROKEN? If the service executed "
    "its logic correctly and the order was rejected on business grounds — margin "
    "thresholds, missing or invalid user input, disabled accounts, unmapped "
    "products — the answer is general, whatever the log level and whichever "
    "service emitted it. ERROR means the order stopped, not that code is at fault. "
    "Reserve backend for an actual defect.\n"
    f"2. Rate its technical severity as one of: {_SEVERITIES}. Judge how urgent "
    "THIS log is on its own (an ERROR that aborts or dead-letters an order is "
    "more severe than a benign/retryable WARN). Base it on the log only; do not "
    "consider business impact you cannot see.\n"
    f"{_ROUTE_EXAMPLES}"
    'Reply with a single JSON object: {"department": "<one of the list>", '
    '"severity": "<one of the list>", "confidence": <0..1>}. '
    "No prose, no code fence."
)

# Grounding rules for POST /chat. The whole point of this prompt is that the
# answer must be traceable to the retrieved records: the corpus is operational
# incident data, and an invented order id or a made-up "resolution" is worse than
# no answer — an agent would act on it. Hence: answer only from the context, say
# so when the context is silent, cite the record ids used.
_CHAT_SYSTEM = (
    "You answer questions from an IT-support engineer about an order-management "
    "pipeline, using ONLY the context provided below.\n"
    "Rules:\n"
    "1. Answer strictly from the provided context. It is the only thing you know "
    "about this system.\n"
    "2. If the context does not contain the answer, say so plainly and stop. Do "
    "NOT fall back on general knowledge about order systems.\n"
    "3. Never invent an order id, event id, outcome, timestamp, department or "
    "resolution. Every concrete detail must appear in the context verbatim.\n"
    "4. Cite the INCIDENT record ids you used, in square brackets, e.g. "
    "[alert-123]. Do NOT cite documentation ids: refer to documentation in prose "
    "as 'the official documentation'. A doc id names a chunk of an internal "
    "repository the reader cannot open, so quoting one is noise, not provenance.\n"
    "4b. The context comes in two clearly labelled kinds and they mean different "
    "things. 'incident history' is what HAPPENED in this system — real orders, "
    "real failures. 'system documentation' describes how a service WORKS in "
    "general; it is reference material, NOT evidence that anything happened. "
    "Never count documentation as incidents, and never say a documented rule or "
    "message was observed unless an incident record shows it.\n"
    "5. Be concise: 2-5 sentences. For a failure, give the cause and where it "
    "stopped; for a question about how the system works, just answer it.\n"
    "6. The context is the top semantic matches for the question, not the complete "
    "history — so never assert a total as if it were the whole picture, and never "
    "say something happened 'only once'. Do NOT discuss how many records you were "
    "given or whether the list is complete: that is reported separately, alongside "
    "your answer. Just answer the question from these records."
)

# NOTE — why there is no coverage sentence in the prompt any more.
#
# Two earlier attempts put the "n of k records shown" fact in the context and asked
# the model to surface it only for counting questions. Measured over 12 live calls
# it obeyed that about half the time in EACH direction: cause questions ("why did
# SAP submission fail") picked up a pointless caveat, and counting questions
# sometimes dropped it. "Mention this only sometimes" is a conditional instruction,
# and a small/fast deployment follows those unreliably.
#
# Coverage is a fact the SERVER already knows exactly — how many records were
# retrieved, what the limit was, whether the limit was hit. Asking an LLM to
# re-state a fact we can compute is strictly worse than returning it: it is
# non-deterministic, it costs tokens, and prose cannot be rendered as a UI badge.
# So it is now a structured field on the response (see api.ChatCoverage) and the
# model is told to leave the subject alone entirely.

_SUMMARY_SYSTEM = (
    "You summarize the end-to-end journey of ONE order through a microservice "
    "order-management pipeline, for an IT-support engineer. Given the journey's "
    "outcome and its ordered log lines, write 2-4 plain-English sentences: which "
    "services the order touched, where it stopped, and why. Do not speculate "
    "beyond the logs. Reply with the summary only."
)

# UNRECOGNIZED_FAILURE is the one outcome journeys.py can't name — a real
# ERROR occurred but no known _FAILURE_RULES marker matched it. This is the
# ONLY outcome that asks for a label; every other outcome uses _SUMMARY_SYSTEM
# unchanged. Duplicated literal, not imported — ai_service and backend never
# cross-import (see this project's clustering-plan Global Constraints).
_UNRECOGNIZED_FAILURE_OUTCOME = "UNRECOGNIZED_FAILURE"

_SUMMARY_WITH_LABEL_SYSTEM = (
    "You summarize the end-to-end journey of ONE order through a microservice "
    "order-management pipeline, for an IT-support engineer. This order's "
    "journey ended in a way none of the system's known failure categories "
    "recognize, so do two things:\n"
    "1. Write 2-4 plain-English sentences: which services the order touched, "
    "where it stopped, and why. Do not speculate beyond the logs.\n"
    "2. Give a short label (3-6 words) naming the specific failure, based on "
    "the causal ERROR log — e.g. 'SPT connection pool exhausted'.\n"
    "Reply with ONLY a JSON object: {\"summary\": \"...\", \"label\": \"...\"}. "
    "No other text."
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


@dataclass(frozen=True)
class SummaryResult:
    summary: str
    suggested_label: str | None = None


async def summarize_journey(
    outcome: str, logs: list[LogLine], model: BaseChatModel | None
) -> SummaryResult:
    """LLM journey summary (+ a suggested failure label for the one
    unrecognized case). Returns a :class:`SummaryResult`; raises LLMError
    otherwise.

    For every outcome except UNRECOGNIZED_FAILURE this behaves exactly as
    before: one plain-text summary, no label, same prompt. That's the one
    case where backend/journeys.py couldn't name the failure — the same LLM
    call is also asked to name it, via a JSON reply instead of plain text.
    """
    if model is None:
        raise LLMError("no summary model configured")
    lines = "\n".join(f"{log.app_name}: {log.message}" for log in logs)
    prompt = f"outcome={outcome}\nlogs:\n{lines}"
    want_label = outcome == _UNRECOGNIZED_FAILURE_OUTCOME
    system = _SUMMARY_WITH_LABEL_SYSTEM if want_label else _SUMMARY_SYSTEM
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=prompt)]
        )
    except Exception as exc:
        raise LLMError(f"summary call failed: {exc}") from exc
    text = _content_text(resp).strip()
    if not text:
        raise LLMError("summary returned empty text")
    if not want_label:
        return SummaryResult(summary=text, suggested_label=None)
    return _parse_summary_with_label(text)


def _parse_summary_with_label(text: str) -> SummaryResult:
    """Split a JSON `{"summary": ..., "label": ...}` reply into a
    :class:`SummaryResult`. Tolerant of surrounding prose (grabs the first
    {...}, mirrors _parse_route's extraction). Never raises: a missing or
    malformed label degrades to ``suggested_label=None``, using the raw text
    as the summary — a bad label must never take down a perfectly good
    summary, matching this codebase's "always fail toward the safe default"
    rule (see ai_service/semcache.py).
    """
    raw = text.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            data = json.loads(raw[start : end + 1])
        except (ValueError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            summary = str(data.get("summary", "")).strip()
            label = data.get("label")
            label = str(label).strip() if label else None
            if summary:
                return SummaryResult(summary=summary, suggested_label=label or None)
    return SummaryResult(summary=raw, suggested_label=None)


def _incident_block(sources: list[dict]) -> str:
    blocks = []
    for record in sources:
        meta = record.get("metadata") or {}
        # Only the metadata worth reasoning about — a full dump would bury the
        # text and invite the model to quote internal keys back at the user.
        facts = " ".join(
            f"{key}={meta[key]}"
            for key in ("kind", "outcome", "department", "severity", "app_name", "order_id")
            if meta.get(key)
        )
        header = f"[{record['id']}] ({record.get('kind', 'record')}"
        header += f"; {facts})" if facts else ")"
        blocks.append(f"{header}\n{record.get('text', '')}")
    return "\n\n".join(blocks)


def _docs_block(docs: list[dict]) -> str:
    blocks = []
    for chunk in docs:
        meta = chunk.get("metadata") or {}
        where = " · ".join(str(meta[k]) for k in ("service", "heading") if meta.get(k))
        text = chunk.get("text", "")
        # The chunk text already opens with its own "[service · heading]" line
        # (knowledge_loader adds it so a fragment carries its identity anywhere).
        # Drop it here — the header below says the same thing, and paying twice
        # for it in every prompt is pure token waste.
        first, _, rest = text.partition("\n")
        if first.startswith("[") and first.endswith("]"):
            text = rest
        blocks.append(f"[{chunk['id']}] ({where})\n{text.strip()}")
    return "\n\n".join(blocks)


def build_chat_prompt(
    query: str, sources: list[dict], docs: list[dict] | None = None
) -> str:
    """The human-message block for :func:`compose_chat_answer` (pure, testable).

    Contains the question and the retrieved context — and NOTHING else. Kept
    separate from the I/O so a test can assert exactly what context the model was
    shown: with a grounding prompt, "no unrelated record leaked in" is a
    correctness property, not a style preference.

    **Two context channels, labelled separately and never merged.** Incident
    history says what HAPPENED; documentation says how things WORK. Earlier every
    block sat under one "incident records:" heading, and the model duly counted
    documentation chunks and reported them as incidents. The labels are what stop
    "the docs describe five validation strategies" turning into "five validations
    failed".

    Deliberately carries NO coverage/limit information: that is computed by the
    server and returned as a structured field (see the note above ``_CHAT_SYSTEM``).
    """
    parts = [f"question: {query}"]

    incidents = _incident_block(sources)
    if incidents:
        parts.append(f"related incident history (things that happened):\n{incidents}")

    documentation = _docs_block(docs or [])
    if documentation:
        parts.append(f"system documentation (how the system works):\n{documentation}")

    if not incidents and not documentation:
        # The model must be told to use the context carried in the question itself
        # (a scoped record's text), NOT that it has nothing — the latter reads as
        # "refuse", which is the behaviour this path exists to avoid.
        parts.append(
            "context: (nothing retrieved — answer from the context given in the "
            "question above)"
        )
    return "\n\n".join(parts)


async def compose_chat_answer(
    query: str,
    sources: list[dict],
    model: BaseChatModel | None,
    *,
    docs: list[dict] | None = None,
    allow_empty_sources: bool = False,
) -> str:
    """LLM chat composition grounded in ``sources``. Raises LLMError otherwise.

    Mirrors :func:`summarize_journey`: build a prompt, call the model, raise
    ``LLMError`` on a provider failure or empty output so the SHARED breaker has
    exactly one exception type to count. The caller (``api.chat``) runs this under
    that breaker and falls back to the deterministic retrieval-only answer, which
    is what keeps the chatbot useful with the LLM completely down.
    """
    if model is None:
        raise LLMError("no chat model configured")
    if not sources and not docs and not allow_empty_sources:
        # Refuse to compose with nothing to ground in — that is precisely the
        # situation where a model invents an answer. The caller already returns
        # the "nothing found" template for this case.
        #
        # ``allow_empty_sources`` is the caller asserting that grounding material
        # is in ``query`` itself (a scoped record's text, read live from the DB).
        # The rule is unchanged — compose only when grounded — but "grounded" is
        # no longer a synonym for "retrieval returned rows".
        raise LLMError("no sources to ground the answer in")
    prompt = build_chat_prompt(query, sources, docs)
    try:
        resp = await model.ainvoke(
            [SystemMessage(content=_CHAT_SYSTEM), HumanMessage(content=prompt)]
        )
    except Exception as exc:
        raise LLMError(f"chat call failed: {exc}") from exc
    text = _content_text(resp).strip()
    if not text:
        raise LLMError("chat returned empty text")
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
