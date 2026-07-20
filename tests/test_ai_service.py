"""Tests for the AI service [3].

No live LLM, RabbitMQ or Redis: the breaker's redis client and clock are fakes,
so the whole state machine is exercised deterministically. (asyncio_mode=auto in
pytest.ini means async test functions run without an explicit marker.)
"""
from __future__ import annotations

from ai_service import settings
from ai_service.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker


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
