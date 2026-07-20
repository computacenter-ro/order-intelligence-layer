"""Tests for the AI service [3].

No live LLM, RabbitMQ or Redis: the breaker's redis client and clock are fakes,
so the whole state machine is exercised deterministically. (asyncio_mode=auto in
pytest.ini means async test functions run without an explicit marker.)
"""
from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from ai_service import settings
from ai_service.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from ai_service.graph import PipelineDeps, process
from ai_service.llm import LLMError
from ai_service.nodes import route
from shared.models import Department, LogLine


# --- fakes -------------------------------------------------------------------
class FakeRedis:
    """Minimal async stand-in for redis.asyncio.Redis (hash ops only)."""

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}

    async def hgetall(self, name: str) -> dict:
        return dict(self.store.get(name, {}))

    async def hset(self, name: str, mapping: dict) -> int:
        self.store.setdefault(name, {}).update(mapping)
        return len(mapping)


class FakeClock:
    """A controllable monotonic clock for breaker cooldown tests."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(redis, clock):
    return CircuitBreaker(redis, key="test:breaker", threshold=3, open_seconds=60, clock=clock)


# --- suppression -------------------------------------------------------------
def test_suppresses_known_benign_warns():
    assert settings.is_suppressed("Not implemented")
    assert settings.is_suppressed("No internal contracts found for sales org: 8100")


def test_does_not_suppress_real_warnings():
    assert not settings.is_suppressed("Order blocked by margin check; submission halted")
    assert not settings.is_suppressed("Retrying order creation for event evt-1 (attempt 2/3)")


def test_suppression_is_case_sensitive():
    # Fixture wording is fixed; a different casing is NOT the benign message.
    assert not settings.is_suppressed("not implemented")


# --- breaker: closed path ----------------------------------------------------
async def test_closed_breaker_allows_calls():
    b = _breaker(FakeRedis(), FakeClock())
    assert await b.allows_call() is True


async def test_success_keeps_breaker_closed():
    r, b = FakeRedis(), None
    b = _breaker(r, FakeClock())
    await b.record_failure()
    await b.record_failure()
    await b.record_success()  # resets the streak
    state, failures, _ = await b._read()
    assert state == CLOSED and failures == 0


# --- breaker: opens after threshold consecutive failures ---------------------
async def test_opens_after_three_consecutive_failures():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)
    await b.record_failure()
    assert await b.allows_call() is True     # 1 failure, still closed
    await b.record_failure()
    assert await b.allows_call() is True     # 2 failures, still closed
    await b.record_failure()
    assert await b.allows_call() is False    # 3rd → OPEN, calls blocked
    state, _f, _o = await b._read()
    assert state == OPEN


# --- breaker: half-open probe after cooldown ---------------------------------
async def test_open_transitions_to_half_open_after_cooldown():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)
    for _ in range(3):
        await b.record_failure()
    assert await b.allows_call() is False    # still within cooldown
    clock.advance(60)
    assert await b.allows_call() is True      # cooldown elapsed → half-open probe
    state, _f, _o = await b._read()
    assert state == HALF_OPEN


async def test_half_open_success_closes_breaker():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)
    for _ in range(3):
        await b.record_failure()
    clock.advance(60)
    await b.allows_call()          # → half_open
    await b.record_success()       # probe succeeded
    state, failures, _ = await b._read()
    assert state == CLOSED and failures == 0


async def test_half_open_failure_reopens_breaker():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)
    for _ in range(3):
        await b.record_failure()
    clock.advance(60)
    await b.allows_call()          # → half_open
    await b.record_failure()       # probe failed → straight back to open
    assert await b.allows_call() is False
    state, _f, opened_at = await b._read()
    assert state == OPEN and opened_at == clock.now


# --- breaker: state survives a new instance (Redis persistence) --------------
async def test_state_persists_across_breaker_instances():
    r, clock = FakeRedis(), FakeClock()
    b1 = _breaker(r, clock)
    for _ in range(3):
        await b1.record_failure()
    # a fresh breaker (simulating a restart) reads the same Redis-backed state
    b2 = _breaker(r, clock)
    assert await b2.allows_call() is False


# --- breaker: call() wrapper never raises ------------------------------------
async def test_call_returns_fallback_when_open():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)
    for _ in range(3):
        await b.record_failure()

    async def boom():
        raise AssertionError("must not be called while open")

    assert await b.call(boom, fallback="FB") == "FB"


async def test_call_records_failure_and_returns_fallback_on_error():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)

    async def boom():
        raise RuntimeError("provider down")

    result = await b.call(boom, fallback="FB")
    assert result == "FB"
    _state, failures, _o = await b._read()
    assert failures == 1


async def test_call_returns_result_and_resets_on_success():
    r, clock = FakeRedis(), FakeClock()
    b = _breaker(r, clock)
    await b.record_failure()

    async def ok():
        return "value"

    assert await b.call(ok) == "value"
    state, failures, _o = await b._read()
    assert state == CLOSED and failures == 0


# =============================================================================
# Pipeline (nodes + graph) — fake chat models, no network/creds
# =============================================================================
def _log(level: str = "ERROR", message: str = "SPT price list unavailable") -> LogLine:
    return LogLine(
        log_id="log-1",
        timestamp=datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc),
        app_name="cc-spt-service",
        level=level,
        logger="c.c.spt.service.PriceListService",
        host="CCECMSRVT001",
        process_id="6340",
        thread="http-nio-8080-exec-8",
        orderId="ORD-6001",
        cartHeaderId="1840927365018240001",
        message=message,
    )


def _fake(text: str) -> GenericFakeChatModel:
    """A chat model that returns ``text`` once per invocation."""
    return GenericFakeChatModel(messages=iter([AIMessage(content=text)] * 50))


def _healthy_deps() -> PipelineDeps:
    return PipelineDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        explainer=_fake("SPT pricing service was unreachable; the order engine could not price the order."),
        router=_fake('{"department": "backend", "confidence": 0.82}'),
    )


# --- happy path: source="ai" -------------------------------------------------
async def test_pipeline_ai_alert_on_healthy_llm():
    alert = await process(_log(), _healthy_deps())
    assert alert.source == "ai"
    assert alert.explanation and "SPT" in alert.explanation
    assert alert.department == Department.backend
    assert alert.confidence == 0.82
    assert alert.log.log_id == "log-1"
    assert alert.emitted_at.tzinfo is not None  # tz-aware UTC


async def test_pipeline_alert_ids_are_unique():
    a1 = await process(_log(), _healthy_deps())
    a2 = await process(_log(), _healthy_deps())
    assert a1.alert_id != a2.alert_id


# --- router constrained to the 5 departments --------------------------------
async def test_router_rejects_unknown_department():
    # 'frontend' is not one of the 5 → LLMError (never a silent wrong route).
    import pytest

    with pytest.raises(LLMError):
        await route(_log(), "explained", _fake('{"department": "frontend", "confidence": 0.9}'))


async def test_router_accepts_all_five_departments():
    for dept in Department:
        d, c = await route(_log(), "x", _fake(f'{{"department": "{dept.value}", "confidence": 0.5}}'))
        assert d == dept


async def test_router_tolerates_code_fence_and_prose():
    d, c = await route(
        _log(), "x",
        _fake('Here you go:\n```json\n{"department": "database", "confidence": 0.7}\n```'),
    )
    assert d == Department.database and c == 0.7


async def test_router_clamps_out_of_range_confidence():
    d, c = await route(_log(), "x", _fake('{"department": "devops", "confidence": 5}'))
    assert c == 1.0


# --- fallback paths: source="fallback", all null -----------------------------
def _assert_fallback(alert):
    assert alert.source == "fallback"
    assert alert.explanation is None
    assert alert.department is None
    assert alert.confidence is None


async def test_pipeline_fallback_when_no_models():
    deps = PipelineDeps(breaker=_breaker(FakeRedis(), FakeClock()), explainer=None, router=None)
    _assert_fallback(await process(_log(), deps))


async def test_pipeline_fallback_when_breaker_open():
    b = _breaker(FakeRedis(), FakeClock())
    for _ in range(3):
        await b.record_failure()  # force open
    deps = PipelineDeps(breaker=b, explainer=_fake("expl"), router=_fake('{"department":"backend","confidence":0.5}'))
    _assert_fallback(await process(_log(), deps))


async def test_pipeline_fallback_when_router_returns_bad_department():
    deps = PipelineDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        explainer=_fake("a clear explanation"),
        router=_fake('{"department": "nonsense", "confidence": 0.9}'),
    )
    # explainer succeeds but router output is invalid → clean fallback, no partial AI alert.
    _assert_fallback(await process(_log(), deps))


async def test_pipeline_records_breaker_failure_on_llm_error():
    b = _breaker(FakeRedis(), FakeClock())
    deps = PipelineDeps(breaker=b, explainer=None, router=None)  # explain() raises LLMError
    await process(_log(), deps)
    _state, failures, _o = await b._read()
    assert failures >= 1  # the breaker counted the failure
