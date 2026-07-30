"""Tests for ai_service/langsmith_stats.py + GET /llm-stats.

No LangSmith account and no network: ``langsmith.Client`` is replaced with a fake
recording what it was called with. The things worth pinning are

* the **exact filter string** per tag — a malformed filter doesn't error, it
  silently returns stats for EVERY run, which reads as plausible data rather than
  as a failure;
* the **fail-toward-empty** contract — no creds, no package, an API error, a
  timeout, a junk payload: each yields ``None`` for the affected node and never a
  500, and one node's failure must not blank the other three; and
* **LangSmith is off the read path** — ``node_stats`` may never issue a query, no
  matter how it is called. That is the rate-limit fix, so a test asserts the
  absence of calls directly.

The field mapping in ``_shape`` was checked against a live ``/runs/stats``
response; the payload in ``_STATS_PAYLOAD`` below is a trimmed copy of that real
response, so a future SDK rename shows up here as a zeroed field rather than as
an exception.

**Replaced when the TTL cache became a background snapshot.** These went away
because the behaviour they described no longer exists, not because they were
failing: ``test_default_ttl_is_three_minutes`` and
``test_ttl_is_read_from_settings_not_hardcoded`` (the knob is deleted);
``test_a_second_call_within_the_ttl_makes_no_langsmith_calls``,
``test_repeated_calls_within_the_ttl_still_cost_one_round``,
``test_the_cache_expires_and_is_not_permanent``,
``test_the_cache_expires_when_the_monotonic_clock_advances`` (there is no expiry —
reads never fetch, so "a second call within the TTL" is now every call);
``test_a_429_failure_is_cached_too_...`` and ``test_an_all_none_result_is_cached``
(caching a failure WAS the backoff; the refresher's fixed interval is now, and the
replacement discipline is that a failure must not overwrite a good value — see
``test_a_failing_tag_keeps_its_previous_good_value``); and the concurrency pair
``test_concurrent_callers_for_...`` (there is nothing left to race on a dict read).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ai_service import langsmith_stats, semcache, settings
from ai_service.api import app

# Captured at import — BEFORE the autouse fixture below zeroes it — so a test can
# still assert what the shipped default is.
_DEFAULT_STAGGER = settings.LLM_STATS_STAGGER_SECONDS


@pytest.fixture(autouse=True)
def _reset_snapshot():
    """Return the module to its cold-start state around every test in this file.

    Without this most tests here break in the worst possible way: each installs its
    OWN `_FakeClient`, so test #2 would silently read test #1's snapshot and never
    touch its fake at all. Assertions would then pass or fail for reasons unrelated
    to what they claim to check — quieter and more misleading than a crash.

    The Redis handle is cleared too, not just the data: a fake redis left installed
    by one test would have the next test's cycle persisting into it, and
    `restore()` would then resurrect data across tests.

    Cleared on the way in AND out: in, so an unrelated earlier test can't seed it;
    out, so this file leaves nothing behind for the rest of the suite.
    """
    langsmith_stats.clear_cache()
    langsmith_stats.configure(None)
    yield
    langsmith_stats.clear_cache()
    langsmith_stats.configure(None)


@pytest.fixture(autouse=True)
def _no_stagger_in_tests(monkeypatch):
    """Zero the inter-request pause for the whole file.

    A cycle is 12 requests, so the real 1.5s stagger costs ~16s per cycle; across
    this file that is many minutes of pure sleeping for no coverage. Zeroed here,
    and the tests that care about the spacing re-enable it explicitly with a small
    value (see test_the_stagger_applies_between_windows_not_just_between_tags).

    Coexists with _reset_snapshot — both autouse, both necessary, and independent:
    one controls timing, the other cross-test data bleed.
    """
    monkeypatch.setattr(settings, "LLM_STATS_STAGGER_SECONDS", 0.0)


# Trimmed from an observed /runs/stats response (fields this module reads, plus a
# couple it ignores, so the test also covers "extra keys are harmless").
_STATS_PAYLOAD = {
    "run_count": 385,
    "latency_p50": 1.502,
    "latency_p99": 4.93784,
    "total_tokens": 218486,
    "prompt_tokens": 196225,
    "error_rate": 0.0,
    "total_cost": 0.06707125,
    "cost_p50": 0.00020805,
}

# One full cycle: every window x every tag.
_CYCLE_CALLS = len(langsmith_stats.WINDOWS) * len(langsmith_stats.NODES)


class _FakeClient:
    """Records every get_run_stats call; returns `payload` (or raises `raises`)."""

    def __init__(self, payload=None, raises=None, per_tag=None):
        self.payload = payload if payload is not None else dict(_STATS_PAYLOAD)
        self.raises = raises
        self.per_tag = per_tag or {}
        self.calls: list[dict] = []
        # Monotonic timestamp per call, so a test can assert the SPACING between
        # requests rather than only the total elapsed time.
        self.at: list[float] = []

    def get_run_stats(self, **kwargs):
        self.calls.append(kwargs)
        self.at.append(time.monotonic())
        tag_filter = kwargs.get("filter", "")
        for tag, behaviour in self.per_tag.items():
            if f'"{tag}"' in tag_filter:
                if isinstance(behaviour, BaseException):
                    raise behaviour
                return behaviour
        if self.raises is not None:
            raise self.raises
        return self.payload


class _FakeRedis:
    """The two async methods persist()/restore() use, over a plain dict."""

    def __init__(self, initial: dict | None = None):
        self.store: dict = dict(initial or {})

    async def set(self, key, value):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


class _AngryRedis:
    """A Redis that fails every operation."""

    async def set(self, *args):
        raise RuntimeError("redis is down")

    async def get(self, *args):
        raise RuntimeError("redis is down")


@pytest.fixture
def configured(monkeypatch):
    """Creds present, so _client() proceeds to construct a client."""
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "fake-key")
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "fake-project")


@pytest.fixture
def fake_client(monkeypatch, configured):
    """Install a _FakeClient factory; the returned callable swaps its behaviour."""
    holder: dict[str, _FakeClient] = {}

    def _install(**kwargs) -> _FakeClient:
        client = _FakeClient(**kwargs)
        holder["client"] = client
        monkeypatch.setattr(langsmith_stats, "_client", lambda: client)
        return client

    _install()
    return _install


@pytest.fixture(autouse=True)
def _quiet_semcache(monkeypatch):
    """semcache.stats() touches Redis; stub it so these tests need no broker."""

    async def _stats():
        return {"hits": 0, "misses": 0, "total": 0, "hit_rate": 0.0, "size": 0, "enabled": False}

    monkeypatch.setattr(semcache, "stats", _stats)


# --- helpers -----------------------------------------------------------------


def _refresh() -> None:
    """Run one refresh cycle."""
    asyncio.run(langsmith_stats.refresh_once())


def _read(window: str = "24h") -> dict:
    """Read one window out of the snapshot."""
    return asyncio.run(langsmith_stats.node_stats(window))


def _collect(window: str = "24h") -> dict:
    """One cycle, then read `window` — the common "populate and inspect" pair."""

    async def _go():
        await langsmith_stats.refresh_once()
        return await langsmith_stats.node_stats(window)

    return asyncio.run(_go())


# --- window parsing ----------------------------------------------------------


@pytest.mark.parametrize("window, hours", [("1h", 1), ("24h", 24), ("7d", 168)])
def test_known_windows_resolve(window, hours):
    assert langsmith_stats.resolve_window(window).total_seconds() == hours * 3600


@pytest.mark.parametrize("window", ["", "30d", "nonsense", "1H", "24 h", None])
def test_unknown_window_falls_back_to_the_default_instead_of_raising(window):
    """A dashboard read: a typo'd param should show last-24h data, not a 422."""
    assert langsmith_stats.resolve_window(window) == langsmith_stats.resolve_window("24h")


def test_start_time_is_utc_aware():
    """CLAUDE.md: every datetime is UTC + timezone-aware, never naive."""
    start = langsmith_stats.start_time_for("1h")
    assert start.tzinfo is not None
    assert start.utcoffset().total_seconds() == 0


def test_every_window_is_refreshed_not_just_the_default():
    """WINDOWS is what the refresher walks; if it drifted from the windows the API
    accepts, the missing one would be permanently empty in the UI."""
    assert set(langsmith_stats.WINDOWS) == {"1h", "24h", "7d"}
    assert langsmith_stats.DEFAULT_WINDOW in langsmith_stats.WINDOWS


# --- the filter string -------------------------------------------------------


def test_tag_filter_is_the_exact_langsmith_syntax():
    assert langsmith_stats.tag_filter("explainer") == 'has(tags, "explainer")'


def test_each_node_is_queried_with_its_own_tag_filter(fake_client):
    client = fake_client()
    _refresh()

    # One query per (window, tag) pair — not one query reused, and not a single
    # unfiltered call whose numbers would be the project-wide total attributed to
    # every node.
    assert len(client.calls) == _CYCLE_CALLS
    assert all(call["filter"] for call in client.calls)
    per_window = sorted(call["filter"] for call in client.calls[: len(langsmith_stats.NODES)])
    assert per_window == sorted(
        langsmith_stats.tag_filter(tag) for tag in langsmith_stats.NODES
    )


def test_queries_are_scoped_to_the_project_and_to_llm_runs(fake_client):
    client = fake_client()
    _refresh()
    for call in client.calls:
        assert call["project_names"] == ["fake-project"]
        # run_type="llm" excludes the chain/prompt spans wrapping each call, which
        # would otherwise double-count every run.
        assert call["run_type"] == "llm"
        assert call["start_time"].endswith("+00:00")


def test_each_window_is_queried_with_its_own_start_time(fake_client):
    """A cycle covers all three windows, so the 1h and 7d spans must actually
    differ — one shared start_time would make every window show the same numbers."""
    client = fake_client()
    _refresh()
    sent = [datetime.fromisoformat(c["start_time"]) for c in client.calls]
    span = (max(sent) - min(sent)).total_seconds()
    # 7d is ~167h earlier than 1h.
    assert 160 * 3600 < span < 170 * 3600


def test_the_four_tags_of_one_window_share_a_start_time(fake_client):
    """All four numbers on a card describe the SAME interval. Recomputing the bound
    per request would smear it by however long the stagger took."""
    client = fake_client()
    _refresh()
    first_window = [
        datetime.fromisoformat(c["start_time"])
        for c in client.calls[: len(langsmith_stats.NODES)]
    ]
    assert len(set(first_window)) == 1


# --- shaping -----------------------------------------------------------------


def test_stats_are_mapped_onto_the_endpoint_contract(fake_client):
    fake_client()
    out = _collect("24h")
    assert out["explainer"] == {
        "run_count": 385,
        "latency_p50_s": 1.502,
        "latency_p99_s": 4.93784,
        "error_rate": 0.0,
        "total_cost_usd": 0.06707125,
        "total_tokens": 218486,
    }


def test_missing_or_renamed_upstream_fields_become_zero_not_an_error():
    """The response is a raw API dict; a renamed field must not raise inside an
    observability endpoint."""
    assert langsmith_stats._shape({}) == {
        "run_count": 0,
        "latency_p50_s": 0.0,
        "latency_p99_s": 0.0,
        "error_rate": 0.0,
        "total_cost_usd": 0.0,
        "total_tokens": 0,
    }
    # Renamed latency field -> zeroed, everything else still read.
    shaped = langsmith_stats._shape({"run_count": 3, "latency_median": 9.9})
    assert shaped["run_count"] == 3 and shaped["latency_p50_s"] == 0.0


def test_shape_coerces_the_types_the_api_and_sdk_can_hand_back():
    from decimal import Decimal

    shaped = langsmith_stats._shape(
        {
            "run_count": "12",                      # numeric string
            "latency_p50": timedelta(seconds=2.5),  # parsed by a future SDK
            "total_cost": Decimal("0.25"),          # Decimal in the pydantic model
            "total_tokens": 10.0,                   # float where an int is expected
            "error_rate": None,
        }
    )
    assert shaped["run_count"] == 12
    assert shaped["latency_p50_s"] == 2.5
    assert shaped["total_cost_usd"] == 0.25
    assert shaped["total_tokens"] == 10
    assert shaped["error_rate"] == 0.0


def test_a_non_dict_payload_yields_none_not_a_crash(fake_client):
    fake_client(payload=["not", "a", "dict"])
    out = _collect("24h")
    assert all(value is None for value in out.values())


# --- graceful degradation ----------------------------------------------------


def test_no_creds_means_every_node_is_none(monkeypatch):
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "")
    out = _collect("24h")
    assert out == {tag: None for tag in langsmith_stats.NODES}


def test_project_alone_is_not_configured(monkeypatch):
    """Both halves are required — a project name with no key can't be queried."""
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "fake-project")
    assert settings.langsmith_configured() is False


def test_a_failing_api_call_yields_none_for_every_node(fake_client):
    fake_client(raises=RuntimeError("langsmith is down"))
    out = _collect("24h")
    assert out == {tag: None for tag in langsmith_stats.NODES}


def test_one_nodes_failure_does_not_blank_the_others(fake_client):
    """The reason the try/except is per node rather than around the loop."""
    fake_client(per_tag={"router": RuntimeError("just this tag")})
    out = _collect("24h")
    assert out["router"] is None
    for tag in ("explainer", "summary", "chat"):
        assert out[tag] is not None, tag
        assert out[tag]["run_count"] == 385


def test_a_failing_tag_does_not_stop_the_ones_after_it(fake_client):
    """The sequential cycle adds an ordering risk concurrency didn't have: an early
    tag blowing up must not abort the loop and blank every later tag — including the
    tags of the windows that come after it."""
    # explainer is FIRST in NODES, so this is the worst-case position.
    client = fake_client(per_tag={"explainer": RuntimeError("429")})
    _refresh()

    assert len(client.calls) == _CYCLE_CALLS  # nothing was skipped
    for window in langsmith_stats.WINDOWS:
        out = _read(window)
        assert out["explainer"] is None, window
        for tag in ("router", "summary", "chat"):
            assert out[tag] is not None, (window, tag)


def test_a_timeout_yields_none_rather_than_hanging(fake_client, monkeypatch):
    monkeypatch.setattr(langsmith_stats, "_TIMEOUT_S", 0.01)

    def _slow(**kwargs):
        time.sleep(0.5)
        return dict(_STATS_PAYLOAD)

    client = fake_client()
    monkeypatch.setattr(client, "get_run_stats", _slow)
    out = _collect("24h")
    assert out == {tag: None for tag in langsmith_stats.NODES}


def test_the_per_tag_timeout_is_not_a_shared_budget(fake_client, monkeypatch):
    """The 8s ceiling is per tag, applied inside _one_node. Walking the tags
    sequentially must not turn it into one budget for the whole cycle."""
    monkeypatch.setattr(langsmith_stats, "_TIMEOUT_S", 0.01)
    monkeypatch.setattr(settings, "LLM_STATS_STAGGER_SECONDS", 0.0)

    attempts: list[dict] = []

    def _slow(**kwargs):
        # Counts here rather than relying on client.calls: this replaces the method
        # that does the recording, so client.calls stays empty either way.
        attempts.append(kwargs)
        time.sleep(0.1)
        return dict(_STATS_PAYLOAD)

    client = fake_client()
    monkeypatch.setattr(client, "get_run_stats", _slow)
    _refresh()
    # Every request was still attempted, each timing out on its own. A shared
    # budget would have aborted the cycle after the first tag.
    assert len(attempts) == _CYCLE_CALLS
    assert _read("24h") == {tag: None for tag in langsmith_stats.NODES}


def test_client_construction_failure_degrades(monkeypatch, configured):
    """A bad LANGSMITH_ENDPOINT etc. must not take the endpoint down."""
    monkeypatch.setattr(langsmith_stats, "_client", lambda: None)
    assert _collect("24h") == {tag: None for tag in langsmith_stats.NODES}


# --- LangSmith is OFF the read path ------------------------------------------
#
# The whole point of the change. Request volume used to be driven by clicks: 4
# queries per window, and 1h/24h/7d being three separate cache keys meant a user
# clicking through all three fired 12 requests in a few seconds — which no TTL can
# prevent, because it is always the FIRST visit to each window.


def test_node_stats_never_queries_langsmith(fake_client):
    """Asserted as an absence of calls, because that is the actual requirement."""
    client = fake_client()
    for window in ("1h", "24h", "7d", "banana", ""):
        _read(window)
    assert client.calls == []


def test_many_reads_cost_nothing(fake_client):
    """A hundred impatient clicks are a hundred dict lookups."""
    client = fake_client()
    _refresh()
    assert len(client.calls) == _CYCLE_CALLS
    for _ in range(100):
        _read("1h")
        _read("24h")
        _read("7d")
    assert len(client.calls) == _CYCLE_CALLS


def test_one_cycle_populates_every_window_and_tag(fake_client):
    """The property that makes reads free: after one cycle NOTHING is cold."""
    client = fake_client()
    _refresh()
    assert len(client.calls) == _CYCLE_CALLS
    for window in langsmith_stats.WINDOWS:
        out = _read(window)
        assert set(out) == set(langsmith_stats.NODES)
        assert all(out[tag] is not None for tag in langsmith_stats.NODES), window


def test_unrecognized_windows_read_the_default_entry(fake_client):
    """A typo shows 24h data rather than an empty card."""
    fake_client()
    _refresh()
    assert _read("banana") == _read("24h")
    assert _read("") == _read("24h")


def test_node_stats_returns_a_copy_not_the_snapshot_itself(fake_client):
    """A caller mutating what it got back must not corrupt the shared snapshot."""
    fake_client()
    _refresh()
    out = _read("24h")
    out["explainer"] = None
    out.pop("router")
    fresh = _read("24h")
    assert fresh["explainer"] is not None
    assert fresh["router"] is not None


def test_node_stats_always_has_every_tag_even_when_empty():
    """Cold, with no cycle ever run: a full key set of Nones, never a KeyError in
    the route or an undefined in the dashboard."""
    assert _read("24h") == {tag: None for tag in langsmith_stats.NODES}


# --- a failure must not overwrite a good value -------------------------------
#
# The replacement for "failures are cached too". A fixed refresh interval is now
# the backoff; the discipline that matters is that one 429 must not blank a card.


def test_a_failing_tag_keeps_its_previous_good_value(fake_client):
    fake_client()
    _refresh()
    assert _read("24h")["router"]["run_count"] == 385

    # Next cycle: router alone starts failing.
    fake_client(per_tag={"router": RuntimeError("429 Rate limit exceeded")})
    _refresh()
    out = _read("24h")
    assert out["router"] is not None, "a single 429 blanked the card"
    assert out["router"]["run_count"] == 385
    assert out["explainer"]["run_count"] == 385


def test_a_total_outage_keeps_the_whole_previous_round(fake_client):
    fake_client()
    _refresh()
    fake_client(raises=RuntimeError("LangSmith is down"))
    _refresh()
    out = _read("7d")
    assert all(out[tag] is not None for tag in langsmith_stats.NODES)


def test_a_recovered_tag_updates_rather_than_staying_stale(fake_client):
    """Carrying a value forward must not pin it: real new numbers win."""
    fake_client()
    _refresh()
    fake_client(payload=dict(_STATS_PAYLOAD, run_count=999))
    _refresh()
    assert _read("24h")["explainer"]["run_count"] == 999


def test_a_first_ever_failure_is_none_not_a_fabricated_zero(fake_client):
    """With no previous value there is nothing to carry, and the answer is "unknown"
    — never run_count 0, which would read as "this model was idle"."""
    fake_client(raises=RuntimeError("429"))
    out = _collect("24h")
    assert out == {tag: None for tag in langsmith_stats.NODES}


# --- fetched_at --------------------------------------------------------------


def test_cold_start_has_no_fetched_at_and_no_data():
    """The state the UI must render as "Collecting…" rather than "not configured"."""
    assert langsmith_stats.fetched_at() is None
    assert langsmith_stats.fetched_at_iso() is None
    assert _read("24h") == {tag: None for tag in langsmith_stats.NODES}


def test_a_cycle_stamps_fetched_at_utc_aware(fake_client):
    fake_client()
    before = datetime.now(timezone.utc)
    _refresh()
    stamp = langsmith_stats.fetched_at()

    assert stamp is not None
    # CLAUDE.md: aware UTC, never naive, never utcnow().
    assert stamp.tzinfo is not None
    assert stamp.utcoffset().total_seconds() == 0
    assert before <= stamp <= datetime.now(timezone.utc)
    assert langsmith_stats.fetched_at_iso() == stamp.isoformat()


def test_fetched_at_advances_with_each_cycle(fake_client):
    fake_client()
    _refresh()
    first = langsmith_stats.fetched_at()
    _refresh()
    assert langsmith_stats.fetched_at() >= first


def test_fetched_at_is_stamped_even_with_no_creds(monkeypatch):
    """"A cycle completed" is the claim, not "we got data". Stamping it here is
    what lets the dashboard say "not configured" instead of "Collecting…" forever
    when there is nothing to collect."""
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "")
    _refresh()
    assert langsmith_stats.fetched_at() is not None


def test_clear_cache_returns_to_cold_start(fake_client):
    fake_client()
    _refresh()
    assert langsmith_stats.fetched_at() is not None

    langsmith_stats.clear_cache()
    assert langsmith_stats.fetched_at() is None
    assert _read("24h") == {tag: None for tag in langsmith_stats.NODES}


# --- request staggering ------------------------------------------------------


def test_the_stagger_applies_between_windows_not_just_between_tags(fake_client, monkeypatch):
    """The gap the previous per-window staggering missed. Spacing the four tags of a
    window but starting the next window immediately still produces a 12-request
    burst, and burst SHAPE is what /runs/stats objects to, not just average rate.

    Asserted on every consecutive pair, so the window boundaries (indices 3->4 and
    7->8) are covered by the same check as the gaps inside a window.
    """
    stagger = 0.05  # small but real; the 1.5s default would make this test crawl
    monkeypatch.setattr(settings, "LLM_STATS_STAGGER_SECONDS", stagger)
    client = fake_client()
    _refresh()

    assert len(client.at) == _CYCLE_CALLS
    gaps = [b - a for a, b in zip(client.at, client.at[1:])]
    assert len(gaps) == _CYCLE_CALLS - 1
    # 0.8 tolerance for scheduler imprecision; a per-window-only stagger would leave
    # two of these gaps at ~0, which is nowhere near the bar.
    assert all(gap >= stagger * 0.8 for gap in gaps), gaps


def test_a_whole_cycle_is_staggered_end_to_end(fake_client, monkeypatch):
    """The total, as the arithmetic in settings.py states it: 11 gaps for 12
    requests."""
    stagger = 0.02
    monkeypatch.setattr(settings, "LLM_STATS_STAGGER_SECONDS", stagger)
    fake_client()

    started = time.monotonic()
    _refresh()
    elapsed = time.monotonic() - started
    assert elapsed >= (_CYCLE_CALLS - 1) * stagger


def test_staggering_does_not_drop_any_request(fake_client, monkeypatch):
    """Spacing changes WHEN each request goes out, never whether it goes out."""
    monkeypatch.setattr(settings, "LLM_STATS_STAGGER_SECONDS", 0.01)
    client = fake_client()
    _refresh()
    assert len(client.calls) == _CYCLE_CALLS


def test_requests_are_issued_in_a_deterministic_order(fake_client):
    """Sequential now, so the order is pinnable — a refactor back to gather() would
    scramble it, and with it the shared-start_time guarantee."""
    client = fake_client()
    _refresh()
    expected = [
        langsmith_stats.tag_filter(tag)
        for _ in langsmith_stats.WINDOWS
        for tag in langsmith_stats.NODES
    ]
    assert [c["filter"] for c in client.calls] == expected


def test_zero_stagger_means_no_added_delay(fake_client):
    """The escape hatch the autouse fixture relies on: 0 disables the spacing."""
    fake_client()  # stagger already 0 via _no_stagger_in_tests
    started = time.monotonic()
    _refresh()
    assert time.monotonic() - started < 1.0


# --- the refresher loop ------------------------------------------------------


def test_the_loop_runs_cycles_repeatedly(monkeypatch):
    cycles = []

    async def _cycle():
        cycles.append(1)

    monkeypatch.setattr(langsmith_stats, "refresh_once", _cycle)

    async def _go():
        task = asyncio.create_task(langsmith_stats.run_refresher(interval=0.01))
        for _ in range(500):  # bounded, so a dead loop fails instead of hanging
            if len(cycles) >= 3:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_go())
    assert len(cycles) >= 3


def test_a_cycle_that_raises_does_not_kill_the_loop(monkeypatch):
    """This task is the ONLY thing that queries LangSmith, so if the loop dies the
    page silently freezes at whatever it last held — a failure with no visible
    symptom except numbers that stop changing."""
    cycles = []

    async def _flaky():
        cycles.append(1)
        if len(cycles) <= 2:
            raise RuntimeError("cycle blew up")

    monkeypatch.setattr(langsmith_stats, "refresh_once", _flaky)

    async def _go():
        task = asyncio.create_task(langsmith_stats.run_refresher(interval=0.01))
        for _ in range(500):
            if len(cycles) >= 5:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_go())
    # Cycles kept happening after the two that raised.
    assert len(cycles) >= 5


def test_the_loop_refreshes_immediately_rather_than_after_one_interval(fake_client):
    """A restart must not leave the page empty for a whole interval."""
    fake_client()

    async def _go():
        task = asyncio.create_task(langsmith_stats.run_refresher(interval=300))
        for _ in range(500):
            if langsmith_stats.fetched_at() is not None:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_go())
    assert langsmith_stats.fetched_at() is not None
    assert _read("24h")["explainer"] is not None


def test_the_loop_is_cancellable(monkeypatch):
    """Shutdown must actually stop it: CancelledError is a BaseException and must
    NOT be swallowed by the blanket except that keeps the loop alive."""

    async def _cycle():
        return None

    monkeypatch.setattr(langsmith_stats, "refresh_once", _cycle)

    async def _go():
        task = asyncio.create_task(langsmith_stats.run_refresher(interval=0.01))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_go())


def test_the_interval_defaults_to_the_setting(monkeypatch):
    """No hardcoded period: the knob is the only thing that sets the request rate."""
    slept: list[float] = []

    async def _cycle():
        return None

    async def _sleep(seconds):
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(langsmith_stats, "refresh_once", _cycle)
    monkeypatch.setattr(settings, "LLM_STATS_REFRESH_INTERVAL_SECONDS", 1234.0)
    monkeypatch.setattr(langsmith_stats.asyncio, "sleep", _sleep)

    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(langsmith_stats.run_refresher())
    assert slept == [1234.0]


# --- persistence -------------------------------------------------------------


def test_a_cycle_persists_the_snapshot(fake_client):
    redis = _FakeRedis()
    langsmith_stats.configure(redis)
    _refresh()

    blob = redis.store[settings.LLM_STATS_SNAPSHOT_KEY]
    payload = json.loads(blob)
    assert payload["fetched_at"] == langsmith_stats.fetched_at_iso()
    assert set(payload["windows"]) == set(langsmith_stats.WINDOWS)
    assert payload["windows"]["24h"]["explainer"]["run_count"] == 385


def test_the_snapshot_survives_a_restart(fake_client):
    """Why this exists: without it the first person to open the page after every
    deploy sees "Collecting…" — and under the old design triggered 12 cold requests
    themselves."""
    redis = _FakeRedis()
    langsmith_stats.configure(redis)
    _refresh()
    stamp = langsmith_stats.fetched_at_iso()

    # Simulate a restart: the process forgets everything, Redis does not.
    langsmith_stats.clear_cache()
    assert langsmith_stats.fetched_at() is None

    asyncio.run(langsmith_stats.restore())
    assert langsmith_stats.fetched_at_iso() == stamp
    for window in langsmith_stats.WINDOWS:
        assert _read(window)["explainer"]["run_count"] == 385


def test_a_restored_timestamp_is_utc_aware(fake_client):
    """It goes through the ISO string and back, and CLAUDE.md's no-naive-datetimes
    rule applies on the way in as much as on the way out."""
    redis = _FakeRedis()
    langsmith_stats.configure(redis)
    _refresh()
    langsmith_stats.clear_cache()
    asyncio.run(langsmith_stats.restore())

    stamp = langsmith_stats.fetched_at()
    assert stamp is not None and stamp.tzinfo is not None
    assert stamp.utcoffset().total_seconds() == 0


def test_a_naive_persisted_timestamp_is_read_as_utc():
    """A hand-edited key or an older dump must not leak a naive datetime into the
    age arithmetic."""
    redis = _FakeRedis(
        {
            settings.LLM_STATS_SNAPSHOT_KEY: json.dumps(
                {"fetched_at": "2026-07-30T09:15:00", "windows": {}}
            )
        }
    )
    langsmith_stats.configure(redis)
    asyncio.run(langsmith_stats.restore())
    stamp = langsmith_stats.fetched_at()
    assert stamp is not None and stamp.tzinfo is not None


def test_persistence_is_a_no_op_without_redis(fake_client):
    """Unit tests and any deployment without Redis: no crash, just no warm start.
    Same degradation as semcache.persist/restore."""
    langsmith_stats.configure(None)
    _refresh()  # must not raise
    asyncio.run(langsmith_stats.restore())  # must not raise
    # And the in-memory snapshot still works.
    assert _read("24h")["explainer"] is not None


def test_a_broken_redis_costs_the_persistence_not_the_cycle(fake_client):
    """The snapshot is already updated in memory by the time persist() runs, so a
    Redis blip must not lose the cycle's data or raise into the loop."""
    langsmith_stats.configure(_AngryRedis())
    _refresh()
    assert _read("24h")["explainer"]["run_count"] == 385
    assert langsmith_stats.fetched_at() is not None


def test_a_broken_redis_does_not_break_restore():
    langsmith_stats.configure(_AngryRedis())
    asyncio.run(langsmith_stats.restore())  # must not raise
    assert _read("24h") == {tag: None for tag in langsmith_stats.NODES}


def test_an_empty_redis_key_leaves_the_cold_start_state():
    langsmith_stats.configure(_FakeRedis())
    asyncio.run(langsmith_stats.restore())
    assert langsmith_stats.fetched_at() is None


@pytest.mark.parametrize(
    "blob",
    [
        b"not json at all",
        json.dumps("a string"),
        json.dumps([1, 2, 3]),
        json.dumps({"windows": "not a dict"}),
        json.dumps({"no_windows_key": True}),
    ],
)
def test_a_corrupt_snapshot_is_ignored_wholesale(blob):
    """Fails to the cold-start state rather than half-trusting the payload: the
    snapshot rebuilds within one interval, whereas putting numbers of unknown
    provenance on this page would undermine the only thing it is for."""
    langsmith_stats.configure(_FakeRedis({settings.LLM_STATS_SNAPSHOT_KEY: blob}))
    asyncio.run(langsmith_stats.restore())
    assert langsmith_stats.fetched_at() is None
    assert _read("24h") == {tag: None for tag in langsmith_stats.NODES}


def test_restore_drops_windows_this_version_no_longer_serves():
    """The snapshot is keyed by what the CODE defines, not by what the blob holds."""
    langsmith_stats.configure(
        _FakeRedis(
            {
                settings.LLM_STATS_SNAPSHOT_KEY: json.dumps(
                    {
                        "fetched_at": "2026-07-30T09:15:00+00:00",
                        "windows": {
                            "24h": {"explainer": {"run_count": 7}},
                            "30d": {"explainer": {"run_count": 99}},
                        },
                    }
                )
            }
        )
    )
    asyncio.run(langsmith_stats.restore())
    assert _read("24h")["explainer"] == {"run_count": 7}
    # The unknown window is gone, and "30d" reads as the default window's data.
    assert _read("30d") == _read("24h")


def test_restore_fills_missing_tags_with_none():
    """A dump from a version with fewer tags must still produce a full key set."""
    langsmith_stats.configure(
        _FakeRedis(
            {
                settings.LLM_STATS_SNAPSHOT_KEY: json.dumps(
                    {
                        "fetched_at": "2026-07-30T09:15:00+00:00",
                        "windows": {"24h": {"explainer": {"run_count": 7}}},
                    }
                )
            }
        )
    )
    asyncio.run(langsmith_stats.restore())
    out = _read("24h")
    assert set(out) == set(langsmith_stats.NODES)
    assert out["chat"] is None


def test_restored_values_are_overwritten_by_a_real_cycle(fake_client):
    """Warm-start data is a stopgap, not a floor."""
    redis = _FakeRedis(
        {
            settings.LLM_STATS_SNAPSHOT_KEY: json.dumps(
                {
                    "fetched_at": "2020-01-01T00:00:00+00:00",
                    "windows": {"24h": {"explainer": {"run_count": 1}}},
                }
            )
        }
    )
    langsmith_stats.configure(redis)
    asyncio.run(langsmith_stats.restore())
    assert _read("24h")["explainer"]["run_count"] == 1

    _refresh()
    assert _read("24h")["explainer"]["run_count"] == 385


# --- the settings knobs ------------------------------------------------------


def test_the_dead_ttl_knob_is_gone():
    """The TTL cache was replaced, not supplemented. A leftover knob that no code
    reads is worse than no knob: someone will tune it and expect an effect."""
    assert not hasattr(settings, "LLM_STATS_CACHE_TTL_SECONDS")


def test_default_stagger_is_sized_for_a_whole_cycle():
    """A cycle is 12 requests, not 4. The old 0.3s default was sized for one
    window's fan-out and would push ~3.3 req/s across a full cycle — over
    LangSmith's ~10 requests/10s query budget on its own.

    Compared against the value captured at import, before the autouse fixture
    zeroes it for the rest of the file.
    """
    if "LLM_STATS_STAGGER_SECONDS" in os.environ:
        pytest.skip("stagger overridden in this environment")
    assert _DEFAULT_STAGGER == 1.5
    # The arithmetic from settings.py, asserted rather than just described.
    assert _CYCLE_CALLS / (_CYCLE_CALLS * _DEFAULT_STAGGER) < 1.0  # < 1 req/s


def test_default_refresh_interval_is_a_minute():
    """The only knob that sets the average rate: 12 requests per interval."""
    if "LLM_STATS_REFRESH_INTERVAL_SECONDS" in os.environ:
        pytest.skip("interval overridden in this environment")
    assert settings.LLM_STATS_REFRESH_INTERVAL_SECONDS == 60.0
    # Two orders of magnitude under a 10-requests-per-10-seconds budget.
    assert _CYCLE_CALLS / settings.LLM_STATS_REFRESH_INTERVAL_SECONDS < 1.0


def test_the_snapshot_key_is_namespaced_like_the_others():
    """Matches ai:semcache / ai:ragindex — one glance at the keyspace should show
    what belongs to the AI service and is safe to drop."""
    assert settings.LLM_STATS_SNAPSHOT_KEY.startswith("ai:")


# --- cache savings -----------------------------------------------------------


def _counters(hits: int, misses: int):
    async def _stats():
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "total": total,
            "hit_rate": (hits / total) if total else 0.0,
            "size": 0,
            "enabled": True,
        }

    return _stats


def test_cache_savings_multiplies_hits_by_both_skipped_calls(monkeypatch, fake_client):
    """A hit skips the explainer AND the router, so both averages count."""
    monkeypatch.setattr(semcache, "stats", _counters(hits=10, misses=5))
    fake_client(
        per_tag={
            # 2 runs, $1.00 total -> $0.50/run; 4 runs, $1.00 -> $0.25/run.
            "explainer": {"run_count": 2, "total_cost": 1.0},
            "router": {"run_count": 4, "total_cost": 1.0},
        }
    )
    _refresh()
    out = asyncio.run(langsmith_stats.cache_savings())
    assert out["hits"] == 10 and out["misses"] == 5
    assert out["hit_rate"] == pytest.approx(10 / 15)
    assert out["estimated_saved_usd"] == pytest.approx(10 * (0.50 + 0.25))


def test_cache_savings_reads_the_snapshot_not_langsmith(monkeypatch, fake_client):
    """Its only expensive input used to be a live query; now it is a dict read, so
    it cannot contribute to the rate limit either."""
    monkeypatch.setattr(semcache, "stats", _counters(hits=1, misses=1))
    client = fake_client()
    _refresh()
    before = len(client.calls)
    for _ in range(10):
        asyncio.run(langsmith_stats.cache_savings())
    assert len(client.calls) == before


def test_cache_savings_reports_counters_but_null_money_without_langsmith(monkeypatch):
    """Hits/misses come from Redis and are known; only the cost is unknown, and an
    unknown cost is null — never a fabricated 0."""
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "")
    monkeypatch.setattr(semcache, "stats", _counters(hits=7, misses=3))
    out = asyncio.run(langsmith_stats.cache_savings())
    assert out == {
        "hits": 7,
        "misses": 3,
        "hit_rate": pytest.approx(0.7),
        "estimated_saved_usd": None,
    }


def test_cache_savings_is_null_when_one_of_the_two_averages_is_missing(
    monkeypatch, fake_client
):
    monkeypatch.setattr(semcache, "stats", _counters(hits=10, misses=0))
    fake_client(
        per_tag={
            "explainer": {"run_count": 2, "total_cost": 1.0},
            "router": RuntimeError("router stats unavailable"),
        }
    )
    _refresh()
    out = asyncio.run(langsmith_stats.cache_savings())
    assert out["estimated_saved_usd"] is None


def test_zero_runs_gives_null_not_a_free_estimate(monkeypatch, fake_client):
    """"No runs in this window" is not the same claim as "these runs were free"."""
    monkeypatch.setattr(semcache, "stats", _counters(hits=10, misses=0))
    fake_client(payload={"run_count": 0, "total_cost": 0.0})
    _refresh()
    out = asyncio.run(langsmith_stats.cache_savings())
    assert out["estimated_saved_usd"] is None


def test_cache_savings_reuses_passed_in_node_stats(monkeypatch, fake_client):
    """The route hands over what it already read, so both halves of the response
    describe the same window."""
    monkeypatch.setattr(semcache, "stats", _counters(hits=4, misses=0))
    client = fake_client()
    nodes = {
        "explainer": {"run_count": 1, "total_cost_usd": 0.10},
        "router": {"run_count": 1, "total_cost_usd": 0.05},
        "summary": None,
        "chat": None,
    }
    out = asyncio.run(langsmith_stats.cache_savings(nodes))
    assert out["estimated_saved_usd"] == pytest.approx(4 * 0.15)
    assert client.calls == []


# --- the route ---------------------------------------------------------------


def test_llm_stats_route_shape(fake_client):
    fake_client()
    _refresh()
    body = TestClient(app).get("/llm-stats").json()
    assert set(body) == {
        "window",
        "nodes",
        "fetched_at",
        "refresh_interval_s",
        "langsmith_configured",
        "cache_savings",
    }
    assert body["window"] == "24h"
    assert set(body["nodes"]) == set(langsmith_stats.NODES)
    assert body["nodes"]["explainer"]["run_count"] == 385
    assert body["fetched_at"] == langsmith_stats.fetched_at_iso()
    assert body["langsmith_configured"] is True


def test_the_route_reports_the_configured_refresh_interval(fake_client, monkeypatch):
    """The dashboard cannot know the period any other way, and it needs it both to
    say when the next update is due and to decide the refresher has died. A
    hardcoded frontend guess would break silently the moment this is retuned —
    which is what someone does right after a rate-limit incident."""
    monkeypatch.setattr(settings, "LLM_STATS_REFRESH_INTERVAL_SECONDS", 90.0)
    fake_client()
    body = TestClient(app).get("/llm-stats").json()
    assert body["refresh_interval_s"] == 90.0


def test_fetched_at_is_stamped_after_the_cycle_not_before(fake_client, monkeypatch):
    """A cycle takes ~18s at the shipped stagger. Stamped at the START, the
    dashboard's "next update" estimate would count from a moment when the numbers
    were still being gathered, and the page would claim data it did not yet have."""
    monkeypatch.setattr(settings, "LLM_STATS_STAGGER_SECONDS", 0.01)
    seen: list[datetime | None] = []

    real_one_node = langsmith_stats._one_node

    async def _spy(client, tag, start_time):
        # What the timestamp looks like from INSIDE the cycle.
        seen.append(langsmith_stats.fetched_at())
        return await real_one_node(client, tag, start_time)

    monkeypatch.setattr(langsmith_stats, "_one_node", _spy)
    fake_client()
    _refresh()

    assert len(seen) == _CYCLE_CALLS
    # No request saw a stamp from this cycle: it did not exist until the end.
    assert all(stamp is None for stamp in seen)
    assert langsmith_stats.fetched_at() is not None


def test_route_reads_never_query_langsmith(fake_client):
    """End to end through the endpoint: clicking the window control is free."""
    client = fake_client()
    http = TestClient(app)
    for window in ("1h", "24h", "7d", "24h", "1h"):
        assert http.get("/llm-stats", params={"window": window}).status_code == 200
    assert client.calls == []


def test_route_reports_the_cold_start_honestly(fake_client):
    """Configured, but no cycle yet: fetched_at null and configured true is exactly
    the pair the dashboard renders as "Collecting…". Reporting it as
    "not configured" (the only option before this field existed) told the reader to
    go fix an env var that was already correct."""
    fake_client()
    body = TestClient(app).get("/llm-stats").json()
    assert body["fetched_at"] is None
    assert body["langsmith_configured"] is True
    assert body["nodes"] == {tag: None for tag in langsmith_stats.NODES}


def test_llm_stats_route_is_200_with_nulls_when_unconfigured(monkeypatch):
    """The acceptance criterion: no creds is a 200 full of nulls, never a 500."""
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "")
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "")
    r = TestClient(app).get("/llm-stats")
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"] == {tag: None for tag in langsmith_stats.NODES}
    assert body["cache_savings"]["estimated_saved_usd"] is None
    # The signal that separates "not configured" from "not collected yet".
    assert body["langsmith_configured"] is False


def test_llm_stats_route_survives_a_broken_langsmith(fake_client):
    fake_client(raises=RuntimeError("boom"))
    _refresh()
    r = TestClient(app).get("/llm-stats")
    assert r.status_code == 200
    assert r.json()["nodes"] == {tag: None for tag in langsmith_stats.NODES}


def test_llm_stats_route_passes_the_window_through(fake_client):
    fake_client()
    _refresh()
    body = TestClient(app).get("/llm-stats", params={"window": "7d"}).json()
    assert body["window"] == "7d"
    assert body["nodes"]["explainer"]["run_count"] == 385


def test_llm_stats_route_accepts_a_junk_window(fake_client):
    fake_client()
    _refresh()
    r = TestClient(app).get("/llm-stats", params={"window": "banana"})
    assert r.status_code == 200
    # Echoed back as asked, but the data read was the 24h default's.
    assert r.json()["window"] == "banana"
    assert r.json()["nodes"]["explainer"] is not None
