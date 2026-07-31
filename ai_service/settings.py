"""[3] AI Service — configuration.

All env-driven knobs for the AI service in one place (CLAUDE.md "[3] AI Service"
and "Env defaults"). Plain ``os.getenv`` reads, matching the convention already
used across the project (``backend/db.py``, ``ai_service/poller.py``,
``shared/log_client.py``) rather than pulling in pydantic-settings.

On import this loads a project-root ``.env`` (if present) into the environment
first, so local secrets/config (e.g. the Azure creds) live in a gitignored
``.env`` instead of the shell or the code. ``.env`` never overrides values
already set in the real environment (``override=False``), so an explicit shell
export or a container's env still wins — and CI/prod, which set real env vars,
are unaffected. Missing ``.env`` is a no-op.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

# Load .env from the project root before any getenv below. override=False keeps
# real environment variables authoritative over the file.
load_dotenv(override=False)

# --- Poller / collector -------------------------------------------------------
ES_URL = os.getenv("ES_URL", "http://localhost:9200").rstrip("/")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
WINDOW_START_OFFSET = int(os.getenv("WINDOW_START_OFFSET", "25"))
WINDOW_END_OFFSET = int(os.getenv("WINDOW_END_OFFSET", "5"))
# The watermark makes consecutive windows contiguous (query [last_to, now-end]),
# so a slow poll cycle never skips wall-clock time. MAX_WINDOW_SPAN caps the
# look-back after a long stall/outage so we don't fetch an unbounded range in one
# shot (the collector holds ~an hour of logs; a huge catch-up window would be a
# thundering-herd read). Beyond the cap, the oldest un-fetched logs are skipped —
# acceptable, and far rarer than the every-run gap the watermark eliminates.
MAX_WINDOW_SPAN = int(os.getenv("MAX_WINDOW_SPAN", "120"))
# Bound on concurrent LLM alert processing so a burst can't open unbounded calls.
ALERT_CONCURRENCY = int(os.getenv("ALERT_CONCURRENCY", "4"))

# --- Redis (dedup + breaker state + poller watermark) -------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "3600"))
# Poller watermark: the `to` of the last fetched window (ISO-8601 string), so
# windows are contiguous across cycles and survive a restart.
WATERMARK_KEY = os.getenv("WATERMARK_KEY", "ai:last_to")

# --- API server (POST /summarize-journey, GET /health) ------------------------
# Default binds to loopback only — the endpoint is unauthenticated and calls the
# LLM (i.e. costs money), so it must not be reachable off-box by default. When
# the backend runs in a separate container/host, set API_HOST=0.0.0.0 AND keep
# the port on a trusted private network (do NOT publish it publicly without auth).
API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8100"))

# --- RabbitMQ output queues ---------------------------------------------------
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
RAW_EVENTS_QUEUE = os.getenv("RAW_EVENTS_QUEUE", "raw.events")
PROCESSED_ALERTS_QUEUE = os.getenv("PROCESSED_ALERTS_QUEUE", "processed.alerts")

# --- Circuit breaker ----------------------------------------------------------
# 3 consecutive LLM failures -> open for 60s -> half-open probe (CLAUDE.md [3]).
BREAKER_STATE_KEY = os.getenv("BREAKER_STATE_KEY", "ai:breaker:state")
BREAKER_FAIL_THRESHOLD = int(os.getenv("BREAKER_FAIL_THRESHOLD", "3"))
BREAKER_OPEN_SECONDS = int(os.getenv("BREAKER_OPEN_SECONDS", "60"))

# --- Semantic cache -----------------------------------------------------------
# Before the explainer+router LLM calls run for a WARN/ERROR log, a local
# semantic cache is consulted (CLAUDE.md [3]): normalize the message (mask
# volatile ids), exact-match the normalized text, else cosine-compare a local
# embedding against stored vectors. A hit >= SEMCACHE_THRESHOLD reuses the
# cached explanation/department/severity and skips both LLM calls.
# The corpus is highly repetitive, so the hit rate is high after warm-up.
SEMCACHE_ENABLED = os.getenv("SEMCACHE_ENABLED", "1") not in ("0", "false", "False", "")
# Cosine-similarity floor for a semantic (non-exact) hit. High by default so
# only near-identical logs collide; below it we miss and run the pipeline.
SEMCACHE_THRESHOLD = float(os.getenv("SEMCACHE_THRESHOLD", "0.95"))
# LRU capacity (distinct normalized log types kept in memory / persisted).
SEMCACHE_MAX_ENTRIES = int(os.getenv("SEMCACHE_MAX_ENTRIES", "500"))
# Local CPU embedding model loaded ONCE at startup (sentence-transformers).
SEMCACHE_MODEL = os.getenv("SEMCACHE_MODEL", "all-MiniLM-L6-v2")
# Redis keys: the persisted cache dump + the hit/miss counters (the demo number).
SEMCACHE_KEY = os.getenv("SEMCACHE_KEY", "ai:semcache")
SEMCACHE_HITS_KEY = os.getenv("SEMCACHE_HITS_KEY", "ai:semcache:hits")
SEMCACHE_MISSES_KEY = os.getenv("SEMCACHE_MISSES_KEY", "ai:semcache:misses")
# Divergence guard (cosine path only): a cosine candidate is rejected if the two
# normalized messages disagree on a SALIENT token — cosine can't see that a
# one-word flip ("succeeded" vs "failed") changes the meaning. Numbers/units are
# always salient; these outcome/polarity/negation words are too. Extend the word
# set for your own log vocabulary via SEMCACHE_SALIENT_EXTRA (comma-separated) —
# for a real (non-mock) log stream, seed it with your domain's error/status
# vocabulary. Set SEMCACHE_GUARD=0 to disable the guard (not recommended).
SEMCACHE_GUARD = os.getenv("SEMCACHE_GUARD", "1") not in ("0", "false", "False", "")
_SALIENT_DEFAULT = (
    "failed", "failure", "succeeded", "success", "passed", "pass", "aborted",
    "abort", "blocked", "denied", "deny", "rejected", "reject", "timeout",
    "timed", "unavailable", "unauthorized", "forbidden", "retry", "retrying",
    "final", "not", "no", "none", "unable", "cannot", "missing", "invalid",
    "disabled", "enabled", "up", "down",
)
_SALIENT_EXTRA = tuple(
    s.strip().lower() for s in os.getenv("SEMCACHE_SALIENT_EXTRA", "").split(",") if s.strip()
)
SEMCACHE_SALIENT_WORDS: frozenset[str] = frozenset(_SALIENT_DEFAULT + _SALIENT_EXTRA)

# --- Retrieval index (RAG phase 1: retrieval only, no LLM) --------------------
# Semantic search over incident history (processed alerts + completed journeys)
# backing POST /chat. Same embedding stack as the semantic cache — deliberately
# the SAME model, so one sentence-transformers load serves both and no new ML
# dependency is introduced.
RAGINDEX_ENABLED = os.getenv("RAGINDEX_ENABLED", "1") not in ("0", "false", "False", "")
# Reuses SEMCACHE_MODEL as its default: sharing the model is what keeps this to
# one CPU model load. Overridable only if you deliberately want a second model.
RAGINDEX_MODEL = os.getenv("RAGINDEX_MODEL", SEMCACHE_MODEL)
# Cosine floor for a retrieved record. LOW on purpose (0.30) and NOT related to
# SEMCACHE_THRESHOLD (0.95): the cache must fail toward a miss because a false hit
# serves a wrong AI-labelled answer, whereas retrieval wants RECALL — a marginal
# source is merely less useful to the agent, never wrong. This is a noise floor,
# not a precision gate. Do not "harmonize" the two numbers.
RAGINDEX_MIN_SCORE = float(os.getenv("RAGINDEX_MIN_SCORE", "0.30"))
# Capacity in records. An order of magnitude above the cache: the cache holds log
# TYPES (~500 distinct shapes), the index holds incident HISTORY (every alert and
# journey), which is the corpus retrieval searches.
RAGINDEX_MAX_ENTRIES = int(os.getenv("RAGINDEX_MAX_ENTRIES", "5000"))
# Redis key for the persisted index dump (rebuildable via backfill_rag).
RAGINDEX_KEY = os.getenv("RAGINDEX_KEY", "ai:ragindex")
# How much agent thumbs-up/down feedback nudges retrieval ranking:
#   final = cosine * (1 - w) + feedback * w
# Deliberately SMALL. Relevance must dominate, so no amount of likes can promote
# an irrelevant record (the relevance floor is applied to raw cosine, before the
# blend). 0 disables the feature entirely and is an exact no-op on ranking — the
# escape hatch if feedback ever turns out to hurt. See backend/feedback.py for how
# the score is computed and which voting biases it corrects for.
#
# 0.30 rather than a more timid 0.15, calibrated to THIS corpus: retrieved cosine
# scores cluster tightly (measured 0.5805..0.6263 — a 0.046 spread), so a 0.15
# weight shifts a final score by at most ~0.02 and could not cross the gap between
# neighbours. Feedback that cannot reorder anything is not a feature. Raise or
# lower it if the score spread changes; check with the numbers, not by feel.
RAGINDEX_FEEDBACK_WEIGHT = float(os.getenv("RAGINDEX_FEEDBACK_WEIGHT", "0.30"))

# --- Suppression list ---------------------------------------------------------
# Benign WARNs that must never become alerts (CLAUDE.md [3] "Suppression list").
# Case-sensitive substring match on the log ``message`` (the fixture wording is
# fixed and load-bearing). Data-driven: extend via SUPPRESS_EXTRA (comma-sep).
_SUPPRESS_DEFAULT = ("Not implemented", "No internal contracts found")
_SUPPRESS_EXTRA = tuple(
    s.strip() for s in os.getenv("SUPPRESS_EXTRA", "").split(",") if s.strip()
)
SUPPRESSED_SUBSTRINGS: tuple[str, ...] = _SUPPRESS_DEFAULT + _SUPPRESS_EXTRA

# --- Azure AI Foundry — read here, WIRED only in llm.py -----------------------
# The endpoint may be given as either a Foundry *project* endpoint
# (".../api/projects/<name>") or the OpenAI-compatible service URL
# (".../openai/v1"). llm.py normalizes to the service URL: the project endpoint
# requires Entra ID (TokenCredential) auth, whereas the /openai/v1 form accepts
# the API key — which is what these deployments (e.g. gpt-5.x) use.
AZURE_AI_FOUNDRY_ENDPOINT = os.getenv("AZURE_AI_FOUNDRY_ENDPOINT", "")
AZURE_AI_FOUNDRY_API_KEY = os.getenv("AZURE_AI_FOUNDRY_API_KEY", "")
# The GA v1 API uses a rolling "preview" version rather than a dated one; a wrong
# dated api-version is rejected with 400 "API version not supported".
AZURE_AI_FOUNDRY_API_VERSION = os.getenv("AZURE_AI_FOUNDRY_API_VERSION", "preview")
AZURE_DEPLOYMENT_EXPLAINER = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_EXPLAINER", "")
AZURE_DEPLOYMENT_ROUTER = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_ROUTER", "")
AZURE_DEPLOYMENT_SUMMARY = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_SUMMARY", "")
# Grounded chat composition (POST /chat). Optional: llm.chat_model() falls back
# to the SUMMARY deployment when this is unset, so /chat needs no new config to
# work — set it only to point chat at a different (e.g. cheaper) deployment.
AZURE_DEPLOYMENT_CHAT = os.getenv("AZURE_AI_FOUNDRY_DEPLOYMENT_CHAT", "")


def service_endpoint() -> str:
    """Normalize the configured endpoint to the OpenAI-compatible service URL.

    Accepts a project endpoint (".../api/projects/<name>") and rewrites it to
    ".../openai/v1"; an endpoint already ending in "/openai/v1" is returned
    unchanged. Empty string stays empty (no creds → fallback).
    """
    ep = AZURE_AI_FOUNDRY_ENDPOINT.rstrip("/")
    if not ep:
        return ep
    if "/api/projects/" in ep:
        ep = ep.split("/api/projects/")[0]
    if not ep.endswith("/openai/v1"):
        ep = ep + "/openai/v1"
    return ep


def llm_configured() -> bool:
    """True only if the minimum Azure creds are present.

    When False the service still runs fully — every LLM call short-circuits to
    the fallback path (CLAUDE.md: "useful with the LLM completely down"). This
    is the switch that lets the whole pipeline run without credentials.
    """
    return bool(AZURE_AI_FOUNDRY_ENDPOINT and AZURE_AI_FOUNDRY_API_KEY)


# --- LangSmith (tracing read-back, GET /llm-stats) ----------------------------
# langchain-core WRITES traces on its own from the standard LANGSMITH_* env vars,
# and langsmith.Client() likewise picks up LANGSMITH_API_KEY / LANGSMITH_ENDPOINT
# from the environment. These two are read here only so the service can answer
# "should we even try to query?" — nothing passes them to the SDK by hand.
#
# The project name is required for querying (run stats are per-project), which is
# why it counts toward "configured" even though tracing itself would default to
# a "default" project.
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "")

# --- LangSmith is OFF the read path: a background refresher owns the queries ---
#
# LangSmith rate-limits /runs/stats far harder than trace ingestion (analytics
# aggregation on a personal plan), and a Monitoring tab open in a browser draws on
# the same per-project budget. Observed in production as
# "Rate limit exceeded for /runs/stats. HTTPError('429 ...')".
#
# The root cause was that request volume was driven by USER CLICKS: each window
# was 4 queries, and 1h/24h/7d were three separate cache keys, so clicking through
# all three fired 12 requests within a few seconds no matter how long the TTL was.
# A TTL cache cannot fix that — it only helps the SECOND visit to a window.
#
# So the shape changed instead: one background task refreshes a snapshot of all 3
# windows x 4 tags, and GET /llm-stats only ever reads that snapshot. Request
# volume is now a constant function of time, completely independent of how many
# people are looking at the page or how fast they click.
#
# How often a full refresh cycle runs. Every cycle costs exactly 12 requests, so
# this is the ONLY knob that sets the average rate: 12 requests per interval.
# At 60s that is 0.2 req/s average — two orders of magnitude under the limit, with
# the page never more than ~a minute stale. The sleep happens AFTER the cycle, so
# the real period is interval + cycle duration (~76s at these defaults).
LLM_STATS_REFRESH_INTERVAL_SECONDS = float(
    os.getenv("LLM_STATS_REFRESH_INTERVAL_SECONDS", "60")
)

# Pause between EVERY pair of successive requests in a cycle — including across
# the window boundary, not just between the four tags of one window. That gap was
# the remaining burst: 12 requests fired back-to-back is the shape LangSmith
# rejects even when the total rate is low.
#
# The arithmetic that picks this number:
#   * one cycle = 3 windows x 4 tags = 12 requests
#   * cycle duration ~= 12 x stagger  (11 gaps, plus each round-trip)
#   * LangSmith's query endpoints allow roughly 10 requests / 10 seconds
# At 1.5s a cycle spans ~16.5s and issues 12 requests over it — about 0.73 req/s
# instantaneous, so even a burst-sensitive limiter sees headroom, and a Monitoring
# tab can share the budget. (The previous 0.3s was sized for 4 requests, not 12:
# it would have pushed 3.3 req/s and tripped the limit on its own.)
#
# Latency is no longer a reason to keep this small — nothing user-facing waits on
# a cycle any more, which is the whole point of the snapshot. 0 disables the
# spacing (the tests set this so the suite doesn't spend minutes sleeping).
LLM_STATS_STAGGER_SECONDS = float(os.getenv("LLM_STATS_STAGGER_SECONDS", "1.5"))

# Redis key holding the persisted snapshot. Same posture as SEMCACHE_KEY:
# rebuildable, safe to drop — the next cycle refills it. It exists so a restart
# (a deploy, a crash) doesn't leave the page blank until the first cycle lands;
# without it the first person to open the page after every deploy sees
# "Collecting…" instead of the numbers that were true a minute earlier.
LLM_STATS_SNAPSHOT_KEY = os.getenv("LLM_STATS_SNAPSHOT_KEY", "ai:llmstats:snapshot")


def langsmith_configured() -> bool:
    """True only if LangSmith can be queried for run stats.

    False is a normal state, not an error: ``GET /llm-stats`` then answers with
    nulls instead of failing, exactly as the pipeline treats missing Azure creds.
    """
    return bool(LANGSMITH_API_KEY and LANGSMITH_PROJECT)


# --- Suppression helper (pure) ------------------------------------------------
def is_suppressed(message: str) -> bool:
    """True if ``message`` contains any suppressed substring (case-sensitive)."""
    return any(sub in message for sub in SUPPRESSED_SUBSTRINGS)
