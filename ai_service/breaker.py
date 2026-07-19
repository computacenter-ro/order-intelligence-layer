"""[3] AI Service — circuit breaker (CLAUDE.md "[3] ... Circuit breaker").

Wraps the LLM calls so a provider outage degrades gracefully instead of
stalling the pipeline: **3 consecutive failures → open for 60s → half-open
probe**. While open, callers skip the LLM entirely and emit a fallback alert.

State lives in Redis (hash ``ai:breaker:state``) so it survives an AI-service
restart — a crash mid-outage must not reset the breaker to closed and hammer a
dead provider. The three fields are:

* ``state``       — ``closed`` | ``open`` | ``half_open``
* ``failures``    — consecutive-failure count (only meaningful while closed)
* ``opened_at``   — epoch seconds the breaker last opened (drives the 60s probe)

The Redis client and the clock are injected so the breaker is exercised in unit
tests with a fake redis + a controllable clock (no real Redis, no real time).
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Protocol

from ai_service import settings

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class _RedisLike(Protocol):
    """The tiny slice of redis.asyncio.Redis the breaker needs."""

    async def hgetall(self, name: str) -> dict: ...
    async def hset(self, name: str, mapping: dict) -> int: ...


class CircuitBreaker:
    """A single shared breaker (CLAUDE.md: one ``ai:breaker:state`` key).

    Used across the explainer, router and summary calls — they all hit the same
    provider, so one outage should trip one breaker.
    """

    def __init__(
        self,
        redis: _RedisLike,
        *,
        key: str = settings.BREAKER_STATE_KEY,
        threshold: int = settings.BREAKER_FAIL_THRESHOLD,
        open_seconds: int = settings.BREAKER_OPEN_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._key = key
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._clock = clock

    # --- state access ---------------------------------------------------------
    async def _read(self) -> tuple[str, int, float]:
        """Return (state, failures, opened_at); defaults for an unset key."""
        raw = await self._redis.hgetall(self._key)
        data = {_dec(k): _dec(v) for k, v in raw.items()} if raw else {}
        state = data.get("state", CLOSED)
        failures = int(data.get("failures", 0) or 0)
        opened_at = float(data.get("opened_at", 0) or 0)
        return state, failures, opened_at

    async def _write(self, state: str, failures: int, opened_at: float) -> None:
        await self._redis.hset(
            self._key,
            mapping={"state": state, "failures": failures, "opened_at": opened_at},
        )

    # --- gate -----------------------------------------------------------------
    async def allows_call(self) -> bool:
        """True if an LLM call may be attempted right now.

        ``closed`` → yes. ``open`` → yes only once the 60s cooldown has elapsed
        (transition to ``half_open`` to let a single probe through). ``half_open``
        → yes (the probe itself; success/failure decides where we go next).
        """
        state, failures, opened_at = await self._read()
        if state == OPEN:
            if self._clock() - opened_at >= self._open_seconds:
                await self._write(HALF_OPEN, failures, opened_at)
                return True
            return False
        return True  # closed or half_open

    # --- outcomes -------------------------------------------------------------
    async def record_success(self) -> None:
        """A call succeeded → reset to fully closed."""
        await self._write(CLOSED, 0, 0.0)

    async def record_failure(self) -> None:
        """A call failed.

        In ``half_open`` a single failure re-opens immediately (the probe told us
        the provider is still down). In ``closed``, increment the consecutive
        count and open once it reaches the threshold.
        """
        state, failures, _opened_at = await self._read()
        if state == HALF_OPEN:
            await self._write(OPEN, self._threshold, self._clock())
            return
        failures += 1
        if failures >= self._threshold:
            await self._write(OPEN, failures, self._clock())
        else:
            await self._write(CLOSED, failures, 0.0)

    # --- convenience wrapper --------------------------------------------------
    async def call(
        self, fn: Callable[[], Awaitable], *, fallback=None
    ):
        """Run ``fn`` under the breaker.

        Returns ``fn``'s result on success. If the breaker is open (call skipped)
        or ``fn`` raises, returns ``fallback`` and records the outcome. Never
        raises — the pipeline must keep moving even when the LLM is down.
        """
        if not await self.allows_call():
            return fallback
        try:
            result = await fn()
        except Exception:
            await self.record_failure()
            return fallback
        await self.record_success()
        return result


def _dec(value) -> str:
    """Decode a redis value that may come back as bytes or str."""
    return value.decode() if isinstance(value, bytes) else str(value)
