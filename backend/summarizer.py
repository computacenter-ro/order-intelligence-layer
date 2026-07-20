"""[5] Core Backend — journey summary client (calls AI service [3]).

On journey completion the backend asks the AI service for a plain-English
summary of the whole journey (CLAUDE.md [5]: "request LLM summary from AI service
``POST /summarize-journey``"). This module is the thin client for that call.

Design mirrors ``backend/teams.py``: pure request-building separated from I/O,
an injectable ``httpx`` client for tests, and — crucially — it **never crashes
journey completion**. If the AI service is slow, down, or errors, we log and
return ``None``; the journey still completes with ``summary=None`` (and the AI
service itself already returns a template ``source="fallback"`` summary when its
LLM is down, so a reachable service always yields *some* text).

The request shape matches the AI service's ``SummaryRequest`` contract
(``ai_service/api.py``) exactly — that contract is the seam between [5] and [3].
"""

from __future__ import annotations

import os

import httpx

from backend.journeys import Completion

# Default matches the docker-compose service name; localhost for native dev.
AI_SERVICE_URL = os.getenv("AI_SERVICE_URL", "http://localhost:8100").rstrip("/")
SUMMARY_TIMEOUT = float(os.getenv("SUMMARY_TIMEOUT", "20"))


def build_request(completion: Completion) -> dict:
    """Build the ``/summarize-journey`` request body from a completion.

    Matches ``ai_service.api.SummaryRequest``: journey meta + the journey's raw
    logs in timestamp order (the assembler already keeps ``journey.logs`` sorted).
    """
    journey = completion.journey
    return {
        "journey_id": completion.journey_id,
        "outcome": completion.outcome,
        "event_id": journey.event_id,
        "order_id": journey.order_id,
        "cart_header_id": journey.cart_header_id,
        "logs": [log.model_dump(mode="json") for log in journey.logs],
    }


async def fetch_summary(
    completion: Completion, *, client: httpx.AsyncClient | None = None
) -> str | None:
    """POST the completed journey to the AI service; return its summary text.

    Returns ``None`` on any failure (unreachable, timeout, non-2xx, malformed
    body) — a summary is best-effort and must never break journey completion.
    ``client`` may be injected (tests / connection reuse); otherwise a
    short-lived client is used.
    """
    body = build_request(completion)
    url = f"{AI_SERVICE_URL}/summarize-journey"
    try:
        if client is not None:
            resp = await client.post(url, json=body, timeout=SUMMARY_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=SUMMARY_TIMEOUT) as http:
                resp = await http.post(url, json=body)
        resp.raise_for_status()
        summary = resp.json().get("summary")
        return summary or None
    except Exception as exc:  # noqa: BLE001 — best-effort; never break completion
        print(
            f"[summarizer] journey {completion.journey_id} summary unavailable "
            f"({type(exc).__name__}: {exc})",
            flush=True,
        )
        return None
