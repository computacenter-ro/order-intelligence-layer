"""Tests for backend/llm_stats_client.py — the forwarding half of GET /llm-stats.

No AI service and no network: an injected ``httpx.AsyncClient`` (the same seam
``rag_client.ask`` exposes) drives every path. The contract under test is that
this function NEVER raises — a stats panel must not be able to 5xx the dashboard —
and that a failure produces the same shape a success does, so the frontend has one
body to render whether tracing is unconfigured or the service is simply down.
"""

from __future__ import annotations

import httpx
import pytest

from backend import llm_stats_client
from backend.llm_stats_client import NODES, degraded, fetch_llm_stats


_GOOD_BODY = {
    "window": "24h",
    "fetched_at": "2026-07-30T09:15:00+00:00",
    # Deliberately NOT the DEFAULT_REFRESH_INTERVAL_S value, so every happy-path
    # assertion on this field proves it was forwarded rather than defaulted.
    "refresh_interval_s": 30.0,
    "langsmith_configured": True,
    "nodes": {
        "explainer": {
            "run_count": 385,
            "latency_p50_s": 1.502,
            "latency_p99_s": 4.93784,
            "error_rate": 0.0,
            "total_cost_usd": 0.06707125,
            "total_tokens": 218486,
        },
        "router": None,
        "summary": None,
        "chat": None,
    },
    "cache_savings": {
        "hits": 12,
        "misses": 4,
        "hit_rate": 0.75,
        "estimated_saved_usd": 0.42,
    },
}


def _client(handler) -> httpx.AsyncClient:
    """An AsyncClient whose transport is `handler` — no socket is opened."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(body=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.request = request  # type: ignore[attr-defined]
        return httpx.Response(status, json=_GOOD_BODY if body is None else body)

    return handler


# --- the happy path ----------------------------------------------------------


async def test_forwards_window_as_a_query_param():
    handler = _ok()
    async with _client(handler) as http:
        await fetch_llm_stats("7d", client=http)
    request = handler.request  # type: ignore[attr-defined]
    assert request.url.path == "/llm-stats"
    assert dict(request.url.params) == {"window": "7d"}
    assert request.method == "GET"


async def test_targets_the_ai_service_url():
    handler = _ok()
    async with _client(handler) as http:
        await fetch_llm_stats("24h", client=http)
    url = str(handler.request.url)  # type: ignore[attr-defined]
    assert url.startswith(llm_stats_client.AI_SERVICE_URL)


async def test_returns_the_body_on_success():
    async with _client(_ok()) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["nodes"]["explainer"]["run_count"] == 385
    assert out["cache_savings"]["estimated_saved_usd"] == 0.42


async def test_window_is_echoed_from_the_request_not_the_response():
    """The panel labels itself with what it asked for. A service echoing a
    different window must not make the label disagree with the query."""
    body = dict(_GOOD_BODY, window="something-else")
    async with _client(_ok(body)) as http:
        out = await fetch_llm_stats("1h", client=http)
    assert out["window"] == "1h"


# --- degradation: every failure mode -----------------------------------------


async def test_connection_error_degrades():
    """What a fully stopped AI service looks like from here."""

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(refused) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out == degraded("24h")


async def test_timeout_degrades():
    def slow(request):
        raise httpx.ReadTimeout("too slow", request=request)

    async with _client(slow) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out == degraded("24h")


@pytest.mark.parametrize("status", [400, 404, 422, 500, 502, 503])
async def test_non_2xx_degrades(status):
    async with _client(_ok(status=status)) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out == degraded("24h")


async def test_unparseable_body_degrades():
    def not_json(request):
        return httpx.Response(200, content=b"<html>gateway</html>")

    async with _client(not_json) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out == degraded("24h")


async def test_a_non_object_json_body_degrades():
    async with _client(_ok(body=["not", "an", "object"])) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out == degraded("24h")


async def test_partial_body_is_filled_in_rather_than_missing_keys():
    """A half-formed reply must not reach the dashboard as an absent key — the
    frontend reads `nodes` and `cache_savings` unconditionally."""
    async with _client(_ok(body={"window": "24h"})) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["nodes"] == {tag: None for tag in NODES}
    assert out["cache_savings"] == degraded("24h")["cache_savings"]
    assert out["fetched_at"] is None
    assert out["langsmith_configured"] is False
    assert out["refresh_interval_s"] == llm_stats_client.DEFAULT_REFRESH_INTERVAL_S


# --- fetched_at / langsmith_configured ---------------------------------------
#
# These two exist so the dashboard can tell "nothing collected yet" from "not
# configured" from "collected, but this model has no data". They are reshaped like
# every other field, so the interesting cases are the ones where they'd silently
# vanish or arrive as the wrong type.


async def test_fetched_at_is_forwarded():
    async with _client(_ok()) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["fetched_at"] == "2026-07-30T09:15:00+00:00"
    assert out["langsmith_configured"] is True
    assert out["refresh_interval_s"] == 30.0
    assert out["refresh_interval_s"] != llm_stats_client.DEFAULT_REFRESH_INTERVAL_S


async def test_fetched_at_survives_the_degraded_path():
    """The field must EXIST on every path. If it only appeared on success, the
    dashboard would read `undefined` exactly when it most needs to know the age of
    what it is showing — and `undefined` is not `null`, so the null-check that
    renders "Collecting…" would fall through to a broken age label."""

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(refused) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert "fetched_at" in out and out["fetched_at"] is None
    assert "langsmith_configured" in out and out["langsmith_configured"] is False


# --- refresh_interval_s ------------------------------------------------------
#
# The dashboard ADDS this to fetched_at (to say when the next update is due) and
# MULTIPLIES it (to decide the refresher has died). So unlike the other fields, a
# bad value here doesn't render as "unknown" — it renders as a confident wrong
# sentence, or as a permanent false alarm. Hence a positivity guard rather than a
# plain type check.


async def test_refresh_interval_is_forwarded():
    body = dict(_GOOD_BODY, refresh_interval_s=120.0)
    async with _client(_ok(body)) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["refresh_interval_s"] == 120.0


async def test_refresh_interval_survives_the_degraded_path():
    """Present AND usable on the degraded path: the frontend divides and multiplies
    by it unconditionally, so a missing key would be `undefined` in that arithmetic
    and produce NaN in the UI."""

    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(refused) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["refresh_interval_s"] == llm_stats_client.DEFAULT_REFRESH_INTERVAL_S
    assert out["refresh_interval_s"] > 0
    assert out == degraded("24h")


@pytest.mark.parametrize(
    "junk",
    [None, 0, 0.0, -30, "60", "", [], {}, True, False],
)
async def test_an_unusable_interval_falls_back_to_the_default(junk):
    """Zero is the dangerous one: it makes the next update permanently overdue, so
    the page would accuse a perfectly healthy refresher of having stopped. `True` is
    covered because bool is an int subclass in Python and would otherwise pass
    through as a 1-second interval."""
    async with _client(_ok(dict(_GOOD_BODY, refresh_interval_s=junk))) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["refresh_interval_s"] == llm_stats_client.DEFAULT_REFRESH_INTERVAL_S


async def test_the_interval_is_always_a_float():
    """One type for the frontend, whichever path produced it."""
    async with _client(_ok(dict(_GOOD_BODY, refresh_interval_s=45))) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert isinstance(out["refresh_interval_s"], float)
    assert out["refresh_interval_s"] == 45.0
    assert isinstance(degraded("24h")["refresh_interval_s"], float)


async def test_a_null_fetched_at_is_passed_through_as_null():
    """The AI service's own cold-start answer: configured, but no cycle has run."""
    body = dict(_GOOD_BODY, fetched_at=None, langsmith_configured=True)
    async with _client(_ok(body)) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["fetched_at"] is None
    # Still True — this is the state that renders "Collecting…" rather than
    # "not configured", and collapsing the two would undo the whole distinction.
    assert out["langsmith_configured"] is True


@pytest.mark.parametrize("junk", [12345, {"iso": "..."}, ["2026-07-30"], True])
async def test_a_wrongly_typed_fetched_at_becomes_null(junk):
    """The dashboard feeds this to `new Date(...)`. A number would render as some
    instant in 1970 and a bool/object as "Invalid Date"; null renders honestly."""
    async with _client(_ok(dict(_GOOD_BODY, fetched_at=junk))) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["fetched_at"] is None


async def test_wrongly_typed_sections_are_replaced():
    body = {"window": "24h", "nodes": "nope", "cache_savings": 5}
    async with _client(_ok(body=body)) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["nodes"] == {tag: None for tag in NODES}
    assert out["cache_savings"]["hits"] is None


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.PoolTimeout("pool"),
        RuntimeError("something unexpected entirely"),
    ],
)
async def test_nothing_escapes_as_an_exception(exc):
    """The whole point: this function has no failure mode that reaches the route."""

    def boom(request):
        raise exc

    async with _client(boom) as http:
        out = await fetch_llm_stats("24h", client=http)
    assert out["nodes"] == {tag: None for tag in NODES}


# --- the degraded shape itself -----------------------------------------------


def test_degraded_covers_every_node():
    body = degraded("24h")
    assert set(body["nodes"]) == set(NODES)
    assert all(value is None for value in body["nodes"].values())


def test_degraded_nulls_the_counters_rather_than_zeroing_them():
    """This side never saw the semantic cache, so 0 would be a fabricated
    measurement. null says "unknown", which is the true statement."""
    savings = degraded("24h")["cache_savings"]
    assert savings == {
        "hits": None,
        "misses": None,
        "hit_rate": None,
        "estimated_saved_usd": None,
    }


def test_degraded_keeps_the_requested_window():
    assert degraded("7d")["window"] == "7d"


def test_degraded_matches_the_success_shape():
    """One body for the frontend to render, whichever path produced it."""
    assert set(degraded("24h")) == set(_GOOD_BODY)


def test_timeout_is_short_enough_for_a_panel_read():
    """Not a user-waiting LLM call — contrast rag_client's 30s chat timeout."""
    from backend.rag_client import RAG_CHAT_TIMEOUT

    assert llm_stats_client.LLM_STATS_TIMEOUT <= 5.0
    assert llm_stats_client.LLM_STATS_TIMEOUT < RAG_CHAT_TIMEOUT


def test_ai_service_url_is_shared_with_rag_client():
    """One source of truth for where the AI service lives."""
    from backend import rag_client

    assert llm_stats_client.AI_SERVICE_URL is rag_client.AI_SERVICE_URL
