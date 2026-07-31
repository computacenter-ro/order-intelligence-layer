"""[3] AI Service — LangSmith run stats, read back per logical model.

Answers ``GET /llm-stats``: how many runs each of the four logical models made in
a window, how slow they were, how often they failed, and what they cost. The
split is possible because ``llm.py`` tags every model it builds with its role
(``explainer`` / ``router`` / ``summary`` / ``chat``), so this module can query
one tag at a time instead of seeing four indistinguishable callers of the same
Azure endpoint.

**LangSmith is NOT on the read path.** ``GET /llm-stats`` reads a snapshot that a
background task refreshes; no HTTP request from a user ever reaches LangSmith.
That is a rate-limit fix, and it is structural rather than a tuning exercise: the
old TTL cache still let request volume be driven by CLICKS, because 1h/24h/7d are
three separate keys and the first visit to each one cost 4 queries — 12 requests
in a few seconds, which is exactly what /runs/stats rejects. No TTL can help the
first visit to a window. Moving the queries to a timer makes the load a constant
function of time instead: 12 requests per refresh interval, whether one person is
watching or fifty.

**Fails toward empty, never toward a 500** — the same philosophy as
``semcache.py``. No creds, no ``langsmith`` package, a network error, a slow API,
a renamed field: each of those yields ``None`` for the affected node while the
others still report. An observability endpoint that can take the service down is
worse than one that says "I don't know".

Isolation is deliberately PER NODE, not per cycle: one tag erroring (say a filter
the API rejects) must not blank the other three — and, since the refresher walks
the tags sequentially, must not abort the ones after it either.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ai_service import semcache, settings

log = logging.getLogger(__name__)

# The logical models, matching the tags llm.py attaches. Adding a tagged model
# there means adding it here — there is no way to enumerate tags from the API.
NODES = ("explainer", "router", "summary", "chat")

# Supported rolling windows. An unknown value falls back to the default rather
# than raising: this is a dashboard read, and a typo'd query param should show
# last-24h data, not a 422.
_WINDOWS: dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
DEFAULT_WINDOW = "24h"

# Every window the refresher keeps warm. All of them, every cycle — the point is
# that switching the window control is a snapshot read, so none of the three may
# be the "cold" one.
WINDOWS: tuple[str, ...] = tuple(_WINDOWS)

# Per-node ceiling on the LangSmith call. asyncio.wait_for cannot kill the
# underlying thread (the HTTP call finishes in the background and its result is
# dropped), but it does stop a slow LangSmith from holding the endpoint open.
_TIMEOUT_S = 8.0

# --- the snapshot ------------------------------------------------------------
#
# Canonical window -> {tag: shaped stats or None}. This is the ONLY thing
# ``node_stats`` reads and the only thing ``refresh_once`` writes.
_SNAPSHOT: dict[str, dict[str, dict | None]] = {}

# When the last refresh cycle FINISHED, or None if none has yet.
#
# WALL CLOCK, deliberately — the TTL cache this replaced used ``time.monotonic()``
# just as deliberately, for expiry arithmetic that no longer exists. A snapshot
# timestamp has two jobs monotonic cannot do: it is persisted to Redis (a
# monotonic reading from a previous process is meaningless there), and it is
# rendered in the UI as "last updated". UTC and tz-aware, per CLAUDE.md.
_FETCHED_AT: datetime | None = None

# Redis handle for persistence, installed by main.py. None (the default, and what
# unit tests get) makes persist/restore no-ops — same posture as semcache.
_redis: Any | None = None


def configure(redis: Any | None) -> None:
    """Install the Redis handle used to persist the snapshot (main.py / tests)."""
    global _redis
    _redis = redis


def clear_cache() -> None:
    """Empty the snapshot and forget when it was fetched.

    Kept under its old name as the test seam (a module-level store would
    otherwise leak one test's data into the next) and as the hook a manual "start
    over" would use. It no longer forces a re-query, because reads never query —
    it returns the module to its cold-start state, which is what the tests want
    and what the UI renders as "Collecting…".
    """
    global _FETCHED_AT
    _SNAPSHOT.clear()
    _FETCHED_AT = None


def fetched_at() -> datetime | None:
    """When the last refresh cycle finished, or None if none has."""
    return _FETCHED_AT


def fetched_at_iso() -> str | None:
    """:func:`fetched_at` as an ISO-8601 string for the JSON body, or None.

    ``None`` is a load-bearing value, not just an absence: it is how the dashboard
    tells "no cycle has run yet" (show "Collecting…") apart from "a cycle ran and
    found nothing" (show why nothing was found). Before this existed, the page
    reported an empty snapshot as "Not configured yet", which during the first few
    seconds of the service's life was simply untrue.
    """
    return _FETCHED_AT.isoformat() if _FETCHED_AT is not None else None


def canonical_window(window: str) -> str:
    """``window`` if recognized, else :data:`DEFAULT_WINDOW`.

    Used as the snapshot key so every unrecognized value collapses onto the entry
    it actually gets served, instead of each distinct typo looking like a window
    the refresher forgot to fill.
    """
    return window if window in _WINDOWS else DEFAULT_WINDOW


def resolve_window(window: str) -> timedelta:
    """The span for ``window``, defaulting to 24h for anything unrecognized."""
    return _WINDOWS.get(window, _WINDOWS[DEFAULT_WINDOW])


def start_time_for(window: str) -> datetime:
    """UTC-aware lower bound for the query (CLAUDE.md: never naive datetimes)."""
    return datetime.now(timezone.utc) - resolve_window(window)


def _as_float(value: Any) -> float:
    """Coerce a stats field to float; anything unusable becomes 0.0.

    The API's JSON carries durations as seconds and money as either a number or a
    decimal string, and a future SDK version could hand back ``timedelta`` /
    ``Decimal`` after parsing. All four are handled, and anything else degrades to
    0.0 rather than raising inside an observability endpoint.
    """
    if value is None:
        return 0.0
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _shape(raw: dict) -> dict:
    """Map a ``/runs/stats`` response onto this endpoint's stable field names.

    Every read is a ``.get`` so a renamed or absent upstream field yields a zero
    rather than a KeyError — the field names below are OUR contract with the
    dashboard, deliberately decoupled from the SDK's.

    Source names come from ``langsmith.schemas.TracerSessionResult`` (the model
    mirroring this response): ``run_count``, ``latency_p50``, ``latency_p99``,
    ``error_rate``, ``total_cost``, ``total_tokens``. NOTE: this mapping was read
    off the installed SDK's schema, not observed against a live LangSmith
    project — if a number here looks wrong, print the raw dict first.
    """
    return {
        "run_count": _as_int(raw.get("run_count")),
        "latency_p50_s": _as_float(raw.get("latency_p50")),
        "latency_p99_s": _as_float(raw.get("latency_p99")),
        "error_rate": _as_float(raw.get("error_rate")),
        "total_cost_usd": _as_float(raw.get("total_cost")),
        "total_tokens": _as_int(raw.get("total_tokens")),
    }


def tag_filter(tag: str) -> str:
    """The LangSmith filter selecting runs carrying ``tag``.

    Its own function so the test can assert the exact string the SDK is called
    with — a silently malformed filter returns stats for *every* run, which reads
    as plausible data rather than as a failure.
    """
    return f'has(tags, "{tag}")'


def _client() -> Any | None:
    """A LangSmith client, or None if unavailable.

    The import is deferred (like the Azure SDK in ``llm.py`` and the encoder in
    ``semcache.py``) so the service still starts when the optional package is
    missing. The client reads LANGSMITH_API_KEY / LANGSMITH_ENDPOINT from the
    environment itself; nothing is passed by hand.
    """
    if not settings.langsmith_configured():
        return None
    try:
        from langsmith import Client
    except Exception as exc:  # pragma: no cover - import guard
        log.warning("langsmith package unavailable, /llm-stats degraded: %s", exc)
        return None
    try:
        return Client()
    except Exception as exc:
        log.warning("could not construct a LangSmith client: %s", exc)
        return None


async def _one_node(client: Any, tag: str, start_time: datetime) -> dict | None:
    """Stats for one tag, or None on any failure. Never raises."""
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(
                client.get_run_stats,
                project_names=[settings.LANGSMITH_PROJECT],
                run_type="llm",
                start_time=start_time.isoformat(),
                filter=tag_filter(tag),
            ),
            timeout=_TIMEOUT_S,
        )
    except Exception as exc:
        # Includes asyncio.TimeoutError. Logged, not raised: the caller reports
        # None for this node and the other three are unaffected.
        log.warning("LangSmith stats for tag %r failed: %s", tag, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("LangSmith stats for tag %r returned %s, not a dict", tag, type(raw))
        return None
    return _shape(raw)


def _store(window: str, tag: str, entry: dict | None) -> None:
    """Write one tag's result into the snapshot, KEEPING a good previous value.

    A failed round must never overwrite data that worked. Otherwise a single 429 on
    a single tag blanks that card until the next cycle — which is precisely the
    symptom this whole design exists to remove, just moved from "every click" to
    "every cycle". ``_one_node`` already isolates failures per node; this is the
    same discipline applied over TIME rather than across tags.

    The trade-off, stated plainly: a card can show a number older than
    ``fetched_at`` implies. Stale-but-real beats blank on a panel whose job is to
    be glanceable, and a persistently failing tag shows up as a value that stops
    moving while the log fills with warnings. What it must never do is show a
    fabricated zero.
    """
    bucket = _SNAPSHOT.get(window)
    if bucket is None:
        bucket = _SNAPSHOT[window] = {node: None for node in NODES}
    if entry is None and bucket.get(tag) is not None:
        return  # carry the last good value forward
    bucket[tag] = entry


async def refresh_once() -> None:
    """One refresh cycle: every window x every tag, one request at a time.

    12 requests (3 windows x 4 tags), each separated by
    ``LLM_STATS_STAGGER_SECONDS`` — including ACROSS the window boundary, which is
    the part the previous per-window staggering missed. Spacing the four tags of a
    window but then starting the next window immediately still produced a
    12-request burst, and burst shape is what /runs/stats objects to, not just
    average rate.

    Never raises: ``_one_node`` swallows provider failures, and the extra guard
    below keeps a bug in it from aborting the 11 requests that follow. The cycle is
    sequential, so an early failure that escaped would blank every later tag —
    an ordering hazard the old concurrent ``gather`` did not have.

    ``fetched_at`` is stamped even when there is nothing to query (no creds): the
    field means "a cycle completed", so the dashboard can tell "still collecting"
    from "collected, and there is nothing to show".
    """
    global _FETCHED_AT

    client = _client()
    if client is not None:
        first = True
        for window in WINDOWS:
            # One start_time per window, shared by its four tags, so the four
            # numbers describe the SAME interval. Recomputing it per request would
            # smear each window's lower bound by however long the stagger took.
            start_time = start_time_for(window)
            for tag in NODES:
                if not first:
                    await asyncio.sleep(settings.LLM_STATS_STAGGER_SECONDS)
                first = False
                try:
                    entry = await _one_node(client, tag, start_time)
                except Exception as exc:  # noqa: BLE001 — see the docstring
                    # CancelledError is a BaseException and deliberately not caught:
                    # a cancelled shutdown must stay cancelled.
                    log.warning(
                        "LangSmith stats for %r/%r raised unexpectedly: %s",
                        window,
                        tag,
                        exc,
                    )
                    entry = None
                _store(window, tag, entry)

    _FETCHED_AT = datetime.now(timezone.utc)
    await persist()


async def run_refresher(*, interval: float | None = None) -> None:
    """Refresh the snapshot forever. Modeled on ``Poller.run()``.

    A failed cycle logs and the loop sleeps on, exactly as the poller does: this
    task is the ONLY thing in the process that queries LangSmith, so if it dies the
    page silently freezes at whatever it last held — a failure mode with no visible
    symptom except numbers that stop changing. Hence the blanket except.

    The first cycle runs IMMEDIATELY, before the first sleep, so a restart
    refreshes within seconds rather than after a full interval.
    """
    if interval is None:
        interval = settings.LLM_STATS_REFRESH_INTERVAL_SECONDS
    while True:
        try:
            await refresh_once()
        except Exception as exc:  # noqa: BLE001 — the loop must outlive any cycle
            log.warning("llm-stats refresh cycle failed (continuing): %s", exc)
        await asyncio.sleep(interval)


async def node_stats(window: str = DEFAULT_WINDOW) -> dict[str, dict | None]:
    """Per-logical-model run stats over ``window`` — a PURE SNAPSHOT READ.

    No lock, no expiry check, no network. Whatever the last refresh cycle put in
    the snapshot is what this returns, which is what keeps user traffic off
    LangSmith entirely.

    Always returns a key for every entry in :data:`NODES`; the value is ``None``
    when that node has no data yet (no cycle has run, no creds, or its query has
    failed every time). A fresh dict is returned so a caller cannot mutate the
    snapshot in place.

    Still ``async`` despite doing no I/O: it is awaited by ``api.py`` and by
    :func:`cache_savings`, and making it sync would churn every call site and test
    to buy nothing. The name is unchanged for the same reason — the contract
    (window in, full node dict out) is identical; only where the data comes from
    changed.
    """
    stored = _SNAPSHOT.get(canonical_window(window)) or {}
    return {tag: stored.get(tag) for tag in NODES}


# --- persistence (Redis; rebuildable, safe to drop) --------------------------


def _dump() -> str:
    """The snapshot as a JSON string, timestamp included."""
    return json.dumps({"fetched_at": fetched_at_iso(), "windows": _SNAPSHOT})


async def persist() -> None:
    """Write the snapshot to Redis. No-op without a handle; never raises.

    Called at the end of every cycle. A Redis blip must cost the persistence, not
    the cycle — the in-memory snapshot is already updated by this point, so
    swallowing the error loses nothing but a restart's worth of warm data.
    """
    if _redis is None:
        return
    try:
        await _redis.set(settings.LLM_STATS_SNAPSHOT_KEY, _dump())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not persist the llm-stats snapshot: %s", exc)


def _parse_stamp(value: Any) -> datetime | None:
    """Parse a persisted ``fetched_at``, or None if it is unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive value (a hand-edited key, an older dump) is READ AS UTC rather than
    # dropped — CLAUDE.md forbids naive datetimes downstream, and this is the
    # boundary where one could sneak in.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


async def restore() -> None:
    """Load a persisted snapshot at startup. No-op without a handle; never raises.

    Without this, every restart makes the first visitor wait out a full cycle
    looking at "Collecting…" — and worse, the old design made them trigger 12 cold
    requests themselves. Anything unexpected in the blob is IGNORED WHOLESALE
    rather than partially trusted: the snapshot is rebuildable within one interval,
    so failing to the cold-start state costs almost nothing, while half-reading a
    corrupt payload would put numbers of unknown provenance on a page whose only
    job is to be trustworthy.
    """
    global _FETCHED_AT
    if _redis is None:
        return
    try:
        blob = await _redis.get(settings.LLM_STATS_SNAPSHOT_KEY)
        if not blob:
            return
        payload = json.loads(blob)
        if not isinstance(payload, dict) or not isinstance(payload.get("windows"), dict):
            log.warning("ignoring a malformed llm-stats snapshot in Redis")
            return
        restored: dict[str, dict[str, dict | None]] = {}
        for window, bucket in payload["windows"].items():
            # A window this version no longer serves is dropped, not carried: the
            # snapshot is keyed by something the code defines, not the blob.
            if window not in _WINDOWS or not isinstance(bucket, dict):
                continue
            restored[window] = {
                tag: bucket[tag] if isinstance(bucket.get(tag), dict) else None
                for tag in NODES
            }
        _SNAPSHOT.clear()
        _SNAPSHOT.update(restored)
        _FETCHED_AT = _parse_stamp(payload.get("fetched_at"))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not restore the llm-stats snapshot: %s", exc)


def _avg_cost(entry: dict | None) -> float | None:
    """Mean USD per run, or None when it can't be computed.

    ``run_count == 0`` gives None, not 0.0: "no runs in this window" is not the
    same claim as "these runs were free", and the caller must not multiply by a
    made-up zero.
    """
    if not entry:
        return None
    runs = entry.get("run_count") or 0
    cost = entry.get("total_cost_usd")
    if runs <= 0 or not cost:
        return None
    return cost / runs


async def cache_savings(nodes: dict[str, dict | None] | None = None) -> dict:
    """Semantic-cache counters plus an estimate of the LLM spend they avoided.

    A cache hit skips BOTH the explainer and the router call, so the estimate is
    ``hits x (avg explainer cost + avg router cost)``. The averages come from the
    runs that *did* happen, which is exactly the right unit price: LangSmith only
    ever saw the cache misses.

    ``estimated_saved_usd`` is ``None`` — never a number — when either average is
    unavailable (no LangSmith, no runs in the window, no cost data). The hit/miss
    counters come from Redis and are reported regardless, since they are what the
    cache itself measures; only the money is uncertain.

    ``nodes`` may be passed in by a caller that has already fetched them, so one
    request doesn't query LangSmith twice (and so both halves of the response
    describe the same window).

    Nothing here is cached, and nothing needs to be: the hit/miss counters are a
    cheap local Redis read and ``node_stats()`` is now a dict lookup. The
    ``nodes is None`` branch (direct callers, tests) reads the same snapshot, so it
    cannot cost a LangSmith query either.
    """
    counters = await semcache.stats()
    hits = _as_int(counters.get("hits"))
    misses = _as_int(counters.get("misses"))
    total = hits + misses

    if nodes is None:
        nodes = await node_stats()
    avg_explainer = _avg_cost(nodes.get("explainer"))
    avg_router = _avg_cost(nodes.get("router"))

    estimated: float | None = None
    if avg_explainer is not None and avg_router is not None:
        estimated = hits * (avg_explainer + avg_router)

    return {
        "hits": hits,
        "misses": misses,
        "hit_rate": (hits / total) if total else 0.0,
        "estimated_saved_usd": estimated,
    }
