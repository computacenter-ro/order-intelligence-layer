"""[3] AI Service — configuration.

All env-driven knobs for the AI service in one place (CLAUDE.md "[3] AI Service"
and "Env defaults"). Plain ``os.getenv`` reads, matching the convention already
used across the project (``backend/db.py``, ``ai_service/poller.py``,
``shared/log_client.py``) rather than pulling in pydantic-settings.

Nothing here does I/O — importing this module just resolves env vars to typed
module-level constants, so tests can monkeypatch the env and re-read, and every
other AI-service module imports its config from here.
"""
from __future__ import annotations

import os

# --- Poller / collector -------------------------------------------------------
ES_URL = os.getenv("ES_URL", "http://localhost:9200").rstrip("/")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
WINDOW_START_OFFSET = int(os.getenv("WINDOW_START_OFFSET", "25"))
WINDOW_END_OFFSET = int(os.getenv("WINDOW_END_OFFSET", "5"))

# --- Redis (dedup + breaker state) --------------------------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "3600"))

# --- RabbitMQ output queues ---------------------------------------------------
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
RAW_EVENTS_QUEUE = os.getenv("RAW_EVENTS_QUEUE", "raw.events")
PROCESSED_ALERTS_QUEUE = os.getenv("PROCESSED_ALERTS_QUEUE", "processed.alerts")

# --- Circuit breaker ----------------------------------------------------------
# 3 consecutive LLM failures -> open for 60s -> half-open probe (CLAUDE.md [3]).
BREAKER_STATE_KEY = os.getenv("BREAKER_STATE_KEY", "ai:breaker:state")
BREAKER_FAIL_THRESHOLD = int(os.getenv("BREAKER_FAIL_THRESHOLD", "3"))
BREAKER_OPEN_SECONDS = int(os.getenv("BREAKER_OPEN_SECONDS", "60"))

# --- Suppression list ---------------------------------------------------------
# Benign WARNs that must never become alerts (CLAUDE.md [3] "Suppression list").
# Case-sensitive substring match on the log ``message`` (the fixture wording is
# fixed and load-bearing). Data-driven: extend via SUPPRESS_EXTRA (comma-sep).
_SUPPRESS_DEFAULT = ("Not implemented", "No internal contracts found")
_SUPPRESS_EXTRA = tuple(
    s.strip() for s in os.getenv("SUPPRESS_EXTRA", "").split(",") if s.strip()
)
SUPPRESSED_SUBSTRINGS: tuple[str, ...] = _SUPPRESS_DEFAULT + _SUPPRESS_EXTRA

# --- Azure AI Foundry (Claude) — read here, WIRED only in llm.py --------------
AZURE_AI_FOUNDRY_ENDPOINT = os.getenv("AZURE_AI_FOUNDRY_ENDPOINT", "")
AZURE_AI_FOUNDRY_API_KEY = os.getenv("AZURE_AI_FOUNDRY_API_KEY", "")
AZURE_DEPLOYMENT_EXPLAINER = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER", "")
AZURE_DEPLOYMENT_ROUTER = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_ROUTER", "")
AZURE_DEPLOYMENT_SUMMARY = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_SUMMARY", "")


def llm_configured() -> bool:
    """True only if the minimum Azure creds are present.

    When False the service still runs fully — every LLM call short-circuits to
    the fallback path (CLAUDE.md: "useful with the LLM completely down"). This
    is the switch that lets the whole pipeline run without credentials.
    """
    return bool(AZURE_AI_FOUNDRY_ENDPOINT and AZURE_AI_FOUNDRY_API_KEY)


# --- Suppression helper (pure) ------------------------------------------------
def is_suppressed(message: str) -> bool:
    """True if ``message`` contains any suppressed substring (case-sensitive)."""
    return any(sub in message for sub in SUPPRESSED_SUBSTRINGS)
