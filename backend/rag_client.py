"""[5] Core Backend — retrieval-index client (pushes to AI service [3] ``/index``).

Division of labour: the **backend owns the DB**, the **AI service owns the
encoder**. So the backend decides what is worth indexing (a persisted alert, a
completed journey) and what is worth filtering on, and ships the text to
``POST /index``; embedding and storage happen over there. No ML dependency is
added to the backend.

Modeled on ``backend/summarizer.py``, with one deliberate difference: this is
**fire-and-forget**. A summary at least has a return value the caller uses; an
index push has nothing to give back, and search being stale is not a reason to
fail alert persistence or journey completion. So every failure — unreachable,
timeout, non-2xx, malformed — is logged and swallowed, the same failure isolation
``backend/main.py`` applies to its fan-out sinks so one bad sink never stops
another.

Text-building is pure and separated from I/O (``alert_text`` / ``journey_text``),
so what gets embedded is unit-testable without a running AI service.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# Same default as backend/summarizer.py: the docker-compose service name, with
# localhost for native dev.
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://localhost:8100").rstrip("/")
# Deliberately short. An index push is best-effort and sits on the alert-persist
# and journey-completion paths — it must never be the reason those get slow.
RAG_INDEX_TIMEOUT = float(os.getenv("RAG_INDEX_TIMEOUT", "5"))
# Longer than the index timeout: /chat runs an LLM call with a user waiting on it,
# whereas an index push is a background write.
RAG_CHAT_TIMEOUT = float(os.getenv("RAG_CHAT_TIMEOUT", "30"))

# Shown when the AI service is unreachable. Says what happened rather than
# pretending nothing matched — an empty result and an outage are different facts.
_UNAVAILABLE = (
    "The chat service is currently unavailable, so no answer could be composed. "
    "Alerts and journeys are still searchable from the dashboard."
)


# --- pure text/metadata builders ---------------------------------------------
def alert_text(
    app_name: str, level: str, logger: str, message: str, explanation: str | None
) -> str:
    """The prose embedded for an alert.

    Includes the AI explanation when present: an agent searching "why did the
    margin check fail" is far more likely to match the explanation's wording than
    the raw log line's. Falls back to just the log fields for a ``fallback``
    alert (explanation is None there) rather than emitting a dangling separator.
    """
    head = f"{app_name} {level} {logger}: {message}"
    tail = (explanation or "").strip()
    return f"{head}. {tail}".strip() if tail else f"{head}."


def journey_text(outcome: str, summary: str | None) -> str:
    """The prose embedded for a completed journey."""
    return f"Journey {outcome}: {(summary or '').strip()}".strip()


def _clean(metadata: dict[str, Any]) -> dict[str, Any]:
    """Drop null metadata values.

    A record must not carry ``{"order_id": None}``: the AI service filters by
    equality on presence, so a null would make ``filters={"order_id": "ORD-1"}``
    compare against None instead of excluding the record.
    """
    return {k: v for k, v in metadata.items() if v is not None}


# --- I/O ----------------------------------------------------------------------
async def push(
    record_id: str,
    kind: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """POST one record to the AI service's ``/index``. Never raises.

    Returns True only on a confirmed ``{"indexed": true}``. False covers every
    other outcome — service down, timeout, non-2xx, or the index self-disabled
    because it has no encoder — and is informational only: no caller should
    branch on it for correctness.
    """
    url = f"{AI_SERVICE_URL}/index"
    body = {"id": record_id, "kind": kind, "text": text, "metadata": _clean(metadata or {})}
    try:
        if client is not None:
            resp = await client.post(url, json=body, timeout=RAG_INDEX_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=RAG_INDEX_TIMEOUT) as http:
                resp = await http.post(url, json=body)
        resp.raise_for_status()
        return bool(resp.json().get("indexed"))
    except Exception as exc:  # noqa: BLE001 — best-effort; never break the caller
        print(
            f"[rag_client] index push failed for {kind} {record_id} "
            f"({type(exc).__name__}: {exc})",
            flush=True,
        )
        return False


async def ask(
    query: str,
    k: int = 5,
    filters: dict | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """POST a question to the AI service's ``/chat``; return its response body.

    Unlike the index pushes above this is NOT fire-and-forget — a user is waiting
    for the answer — so it uses a longer timeout (an LLM call, not a write) and
    returns a well-formed degraded body rather than raising. The backend route
    then always has something to serve, and the failure reads to the agent the
    same way an LLM outage does: no narrative, no sources, but not an error page.
    """
    url = f"{AI_SERVICE_URL}/chat"
    body = {"query": query, "k": k, "filters": filters}
    try:
        if client is not None:
            resp = await client.post(url, json=body, timeout=RAG_CHAT_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=RAG_CHAT_TIMEOUT) as http:
                resp = await http.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()
        # Defensive: a malformed body must not propagate half-shapes into the API.
        return {
            "answer": str(data.get("answer") or _UNAVAILABLE),
            "sources": list(data.get("sources") or []),
            "mode": str(data.get("mode") or "retrieval-only"),
            "coverage": dict(data.get("coverage") or {}),
        }
    except Exception as exc:  # noqa: BLE001 — degrade, never 500 the dashboard
        print(f"[rag_client] chat unavailable ({type(exc).__name__}: {exc})", flush=True)
        # Nothing was retrieved, so nothing was truncated — an unreachable service
        # must not look like a capped result set.
        return {
            "answer": _UNAVAILABLE,
            "sources": [],
            "mode": "retrieval-only",
            "coverage": {"shown": 0, "limit": k, "truncated": False},
        }


async def index_alert(alert, *, client: httpx.AsyncClient | None = None) -> bool:
    """Index a persisted ``ProcessedAlert`` (fire-and-forget)."""
    log = alert.log
    return await push(
        alert.alert_id,
        "alert",
        alert_text(log.app_name, log.level, log.logger, log.message, alert.explanation),
        {
            "department": alert.department.value if alert.department is not None else None,
            "severity": alert.severity.value if alert.severity is not None else None,
            "source": alert.source,
            "app_name": log.app_name,
            "level": log.level,
            "order_id": log.orderId,
            "event_id": log.eventId,
            "ts": alert.emitted_at.isoformat() if alert.emitted_at is not None else None,
        },
        client=client,
    )


async def index_journey(
    completion, summary: str | None, *, client: httpx.AsyncClient | None = None
) -> bool:
    """Index a completed journey + its summary (fire-and-forget).

    Skips a journey with no summary: ``"Journey SUCCESS:"`` with nothing after it
    embeds almost no signal and would sit in the index as a near-duplicate of
    every other outcome-only record, diluting retrieval.
    """
    if not (summary or "").strip():
        return False
    journey = completion.journey
    return await push(
        completion.journey_id,
        "journey",
        journey_text(completion.outcome, summary),
        {
            "outcome": completion.outcome,
            "status": completion.status.value,
            "journey_id": completion.journey_id,
            "order_id": journey.order_id,
            "event_id": journey.event_id,
            "ts": journey.last_ts.isoformat() if journey.last_ts is not None else None,
        },
        client=client,
    )
