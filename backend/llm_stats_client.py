"""[5] Core Backend — LLM-stats client (reads AI service [3] ``/llm-stats``).

The dashboard never talks to the AI service directly: :8100 binds to loopback and
is unauthenticated by design, so every call goes through this API, which forwards
and adds the session guard. This module is the forwarding half for the
observability read, modeled on ``rag_client.ask()``.

Like ``ask()``, it **degrades instead of raising**. The AI service already answers
with an all-nulls body when LangSmith isn't configured, so an unreachable service
returns exactly that same shape — one code path in the dashboard covers both
"tracing not set up" and "AI service down", and neither shows the agent an error
page for a stats panel. A stats read that can 502 the dashboard is worse than one
that says "I don't know".

``AI_SERVICE_URL`` is imported from ``rag_client`` rather than re-read from the
environment: ``summarizer.py`` and ``rag_client.py`` already each declare their
own copy of that ``os.getenv`` line, and a third would be a third thing to keep in
step.
"""

from __future__ import annotations

import httpx

from backend.rag_client import AI_SERVICE_URL

# The logical models the AI service reports on (its ``langsmith_stats.NODES``).
# Duplicated deliberately: the backend must be able to build the degraded body
# WITHOUT reaching the AI service — importing the tuple from over there is exactly
# the dependency this service boundary exists to avoid. The AI service's own tests
# pin its side; a divergence here shows up as an extra/missing null key, not as a
# wrong number.
NODES = ("explainer", "router", "summary", "chat")

# Deliberately short. This is a dashboard panel read, not a user-waiting LLM call
# (contrast RAG_CHAT_TIMEOUT=30s): a stalled AI service must not hold the request
# open, and an empty panel beats a hanging one.
LLM_STATS_TIMEOUT = 5.0

# Fallback refresh period reported when the AI service can't be asked for its own.
# The dashboard divides by this to say "next update in ~Xs" and multiplies it to
# decide "the refresher looks dead", so 0/None would be a divide-by-nothing and a
# permanently-stale verdict. It only ever applies on the degraded path, where the
# numbers are all null anyway — it exists to keep the arithmetic well-formed, not to
# be accurate.
DEFAULT_REFRESH_INTERVAL_S = 60.0


def degraded(window: str) -> dict:
    """The "nothing to report" body — identical in shape to a successful reply.

    Matches what the AI service itself returns when LangSmith is unconfigured, so
    the dashboard has one shape to render and null genuinely means "unknown"
    rather than "zero". Counters are NOT faked to 0 here: this side has no idea
    what the semantic cache did, and a fabricated 0 would read as a measurement.

    ``fetched_at`` / ``langsmith_configured`` are present and null/false for the
    same reason every other field is: the shape must not change between paths, or
    the field vanishes silently on exactly the path where the dashboard most needs
    to know it has nothing. Note the small inaccuracy this accepts —
    ``langsmith_configured: false`` for an unreachable AI service means "we cannot
    say it is configured", not "we checked and it isn't, so the page reads
    "not configured" when the truth is "AI service down". That is the pre-existing
    behaviour for this path and beats the alternative, which is a permanent
    "Collecting…" for a service that will never collect anything.
    """
    return {
        "window": window,
        "nodes": {tag: None for tag in NODES},
        "fetched_at": None,
        "refresh_interval_s": DEFAULT_REFRESH_INTERVAL_S,
        "langsmith_configured": False,
        "cache_savings": {
            "hits": None,
            "misses": None,
            "hit_rate": None,
            "estimated_saved_usd": None,
        },
    }


async def fetch_llm_stats(window: str, *, client: httpx.AsyncClient | None = None) -> dict:
    """GET the AI service's ``/llm-stats`` for ``window``; never raise.

    Any failure — connection refused, timeout, non-2xx, unparseable body — is
    logged and turned into :func:`degraded`. ``client`` is injectable so tests can
    drive this without a live service (same seam as ``rag_client.ask``).
    """
    url = f"{AI_SERVICE_URL}/llm-stats"
    params = {"window": window}
    try:
        if client is not None:
            resp = await client.get(url, params=params, timeout=LLM_STATS_TIMEOUT)
        else:
            async with httpx.AsyncClient(timeout=LLM_STATS_TIMEOUT) as http:
                resp = await http.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError(f"expected an object, got {type(data).__name__}")
        # Reshaped rather than passed through, so a half-formed body can't reach the
        # dashboard as a missing key. The window is echoed from OUR request: the
        # panel labels itself with what it asked for, not with whatever came back.
        nodes = data.get("nodes")
        savings = data.get("cache_savings")
        stamp = data.get("fetched_at")
        interval = data.get("refresh_interval_s")
        # Must be a POSITIVE number: the dashboard adds it to a timestamp and
        # multiplies it for its staleness threshold, so 0, a negative, or a string
        # would turn "next update in ~Xs" into nonsense and pin the label to
        # "the refresh may have stopped" forever. bool is excluded explicitly —
        # it is an int subclass in Python, and True would sail through as 1.
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            interval = DEFAULT_REFRESH_INTERVAL_S
        return {
            "window": window,
            "nodes": nodes if isinstance(nodes, dict) else {tag: None for tag in NODES},
            # Type-checked like the sections above: the dashboard feeds this to
            # `new Date(...)`, so a number or an object here would render as an
            # "Invalid Date" age rather than as the honest "nothing collected yet".
            "fetched_at": stamp if isinstance(stamp, str) else None,
            "refresh_interval_s": float(interval),
            "langsmith_configured": bool(data.get("langsmith_configured")),
            "cache_savings": (
                savings
                if isinstance(savings, dict)
                else degraded(window)["cache_savings"]
            ),
        }
    except Exception as exc:  # noqa: BLE001 — degrade, never 500 the dashboard
        print(
            f"[llm_stats_client] llm-stats unavailable ({type(exc).__name__}: {exc})",
            flush=True,
        )
        return degraded(window)
