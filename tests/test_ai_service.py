"""Tests for the AI service [3].

Unit tests use fakes (breaker redis+clock, chat model, publisher channel) so the
default suite needs no LLM, RabbitMQ or Redis. (asyncio_mode=auto in pytest.ini
means async test functions run without an explicit marker.)

One live round-trip test against real RabbitMQ is gated behind the env flag
``AI_LIVE_RABBITMQ=1`` (bring the broker up with ``docker compose up -d
rabbitmq``); it is skipped by default.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from ai_service import settings
from ai_service.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from ai_service.graph import PipelineDeps, process
from ai_service.llm import LLMError
from ai_service.nodes import route
from ai_service.publisher import Publisher
from shared.models import Department, LogLine, ProcessedAlert


# Manual wide/live test of the whole top-of-stack (real infra, LLM in fallback),
# each command in its own terminal:
#   1. collector:      python -m uvicorn pipeline.mock_es.app:app --port 9200
#   2. mock services:  python -m pipeline.services.run_all
#   3. AI service:     python -m ai_service.main   (prints "LLM mode: FALLBACK")
#   4. fire scenarios: python -m pipeline.injector.inject --all
# Then watch raw.events + processed.alerts fill up in the RabbitMQ UI (:15672).


# --- fakes -------------------------------------------------------------------
class FakeRedis:
    """Minimal async stand-in for redis.asyncio.Redis.

    Hash ops (breaker) + SETNX (poller dedup). ``set(..., nx=True)`` returns
    True only the first time a key is set, mirroring real SETNX.
    """

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.keys: dict[str, object] = {}

    async def hgetall(self, name: str) -> dict:
        return dict(self.store.get(name, {}))

    async def hset(self, name: str, mapping: dict) -> int:
        self.store.setdefault(name, {}).update(mapping)
        return len(mapping)

    async def set(self, key: str, value, nx: bool = False, ex: int | None = None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


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


# =============================================================================
# Publisher — unit tests with a fake channel (no broker)
# =============================================================================
class _FakeExchange:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, object]] = []

    async def publish(self, message, routing_key: str) -> None:
        # record (routing_key, body, delivery_mode) for assertions
        self.published.append((routing_key, message.body, message.delivery_mode))


class _FakeChannel:
    """Records declared queues and published messages; no I/O."""

    def __init__(self) -> None:
        self.declared: list[tuple[str, bool]] = []
        self.default_exchange = _FakeExchange()

    async def declare_queue(self, name: str, durable: bool = False):
        self.declared.append((name, durable))
        return object()


def _alert(source: str = "fallback") -> ProcessedAlert:
    return ProcessedAlert(
        alert_id=str(uuid.uuid4()),
        emitted_at=datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc),
        log=_log(),
        explanation=None if source == "fallback" else "explained",
        department=None if source == "fallback" else Department.backend,
        confidence=None if source == "fallback" else 0.7,
        source=source,
    )


async def test_connect_declares_both_queues_durable():
    ch = _FakeChannel()
    async with Publisher(channel=ch) as pub:
        assert ("raw.events", True) in ch.declared
        assert ("processed.alerts", True) in ch.declared


async def test_publish_raw_routes_loglinejson_to_raw_events():
    ch = _FakeChannel()
    async with Publisher(channel=ch) as pub:
        await pub.publish_raw(_log(message="hello"))
    routing_key, body, delivery_mode = ch.default_exchange.published[0]
    assert routing_key == "raw.events"
    payload = json.loads(body)
    assert payload["message"] == "hello" and payload["log_id"] == "log-1"
    # persistent delivery (at-least-once)
    from aio_pika import DeliveryMode

    assert delivery_mode == DeliveryMode.PERSISTENT


async def test_publish_alert_routes_processedalertjson_to_processed_alerts():
    ch = _FakeChannel()
    async with Publisher(channel=ch) as pub:
        await pub.publish_alert(_alert("ai"))
    routing_key, body, _dm = ch.default_exchange.published[0]
    assert routing_key == "processed.alerts"
    payload = json.loads(body)
    assert payload["source"] == "ai" and payload["department"] == "backend"
    # the full original log travels inside the alert
    assert payload["log"]["log_id"] == "log-1"


async def test_publish_uses_the_models_not_handbuilt_dicts():
    # ProcessedAlert JSON must round-trip back into the model (exact field set).
    ch = _FakeChannel()
    async with Publisher(channel=ch) as pub:
        await pub.publish_alert(_alert("fallback"))
    _rk, body, _dm = ch.default_exchange.published[0]
    restored = ProcessedAlert.model_validate_json(body)
    assert restored.source == "fallback" and restored.explanation is None


# =============================================================================
# Publisher — LIVE round-trip against real RabbitMQ (opt-in)
# =============================================================================
@pytest.mark.skipif(
    os.getenv("AI_LIVE_RABBITMQ") != "1",
    reason="live RabbitMQ round-trip; set AI_LIVE_RABBITMQ=1 (docker compose up -d rabbitmq)",
)
async def test_live_roundtrip_publish_then_consume():
    """Publish to both queues on a real broker, consume back, assert payloads.

    Uses unique per-run queue names so it never collides with a running AI
    service or a previous test run, and cleans them up afterward.
    """
    import aio_pika

    suffix = uuid.uuid4().hex[:8]
    raw_q = f"test.raw.events.{suffix}"
    alerts_q = f"test.processed.alerts.{suffix}"

    connection = await aio_pika.connect_robust(settings.RABBITMQ_URL)
    try:
        async with Publisher(
            url=settings.RABBITMQ_URL, raw_queue=raw_q, alerts_queue=alerts_q
        ) as pub:
            await pub.publish_raw(_log(message="live-raw"))
            await pub.publish_alert(_alert("ai"))

            # consume one message back from each queue on a fresh channel
            channel = await connection.channel()
            raw_queue = await channel.declare_queue(raw_q, durable=True)
            alerts_queue = await channel.declare_queue(alerts_q, durable=True)

            raw_msg = await raw_queue.get(timeout=5)
            alert_msg = await alerts_queue.get(timeout=5)
            await raw_msg.ack()
            await alert_msg.ack()

            raw_payload = LogLine.model_validate_json(raw_msg.body)
            alert_payload = ProcessedAlert.model_validate_json(alert_msg.body)
            assert raw_payload.message == "live-raw"
            assert alert_payload.source == "ai"
            assert alert_payload.log.log_id == "log-1"

            # cleanup
            await raw_queue.delete(if_unused=False, if_empty=False)
            await alerts_queue.delete(if_unused=False, if_empty=False)
    finally:
        await connection.close()


# =============================================================================
# Poller — routing/dedup/suppression logic with fakes (no collector/broker/LLM)
# =============================================================================
from ai_service.poller import Poller, needs_alert, poll_window  # noqa: E402


class FakePublisher:
    """Records what the poller publishes to each queue."""

    def __init__(self) -> None:
        self.raw: list[LogLine] = []
        self.alerts: list[ProcessedAlert] = []

    async def publish_raw(self, log: LogLine) -> None:
        self.raw.append(log)

    async def publish_alert(self, alert: ProcessedAlert) -> None:
        self.alerts.append(alert)


def _raw_dict(level: str = "INFO", message: str = "ok", log_id: str = "L1") -> dict:
    """A collector-shaped raw log dict (what fetch_logs returns)."""
    return {
        "log_id": log_id,
        "timestamp": "2026-07-14T08:00:00.000Z",
        "app_name": "cc-order-engine",
        "level": level,
        "logger": "c.c.orderengine.service.OrderService",
        "host": "CCECMEWEBT001",
        "process_id": "9201",
        "thread": "pool-3-thread-1",
        "orderId": "ORD-6001",
        "cartHeaderId": "1840927365018240001",
        "message": message,
    }


def _make_poller(window_logs: list[dict], *, healthy_llm: bool = False):
    """A Poller whose fetch_logs returns ``window_logs``; fake redis+publisher."""
    redis = FakeRedis()
    pub = FakePublisher()
    if healthy_llm:
        deps = _healthy_deps()
    else:
        deps = PipelineDeps(breaker=_breaker(redis, FakeClock()), explainer=None, router=None)
    poller = Poller(redis=redis, publisher=pub, pipeline_deps=deps, http=object())
    poller.fetch_logs = lambda f, t: _async(window_logs)  # type: ignore[method-assign]
    return poller, pub


async def _async(value):
    return value


# --- needs_alert gate --------------------------------------------------------
def test_needs_alert_only_warn_error_and_not_suppressed():
    assert needs_alert(_log(level="ERROR", message="boom"))
    assert needs_alert(_log(level="WARN", message="something odd"))
    assert not needs_alert(_log(level="INFO", message="boom"))
    assert not needs_alert(_log(level="DEBUG", message="boom"))
    # suppressed WARN → NOT an alert
    assert not needs_alert(_log(level="WARN", message="Not implemented"))


# --- fan-out: raw always, alerts conditionally -------------------------------
async def test_every_log_goes_to_raw_events():
    logs = [_raw_dict("INFO", "a", "L1"), _raw_dict("DEBUG", "b", "L2"),
            _raw_dict("ERROR", "c", "L3")]
    poller, pub = _make_poller(logs)
    n = await poller.poll_once()
    assert n == 3
    assert {l.log_id for l in pub.raw} == {"L1", "L2", "L3"}  # ALL levels → raw


async def test_only_warn_error_becomes_alert():
    logs = [_raw_dict("INFO", "a", "L1"), _raw_dict("ERROR", "boom", "L2")]
    poller, pub = _make_poller(logs)
    await poller.poll_once()
    assert len(pub.raw) == 2
    assert len(pub.alerts) == 1
    assert pub.alerts[0].log.log_id == "L2"


async def test_suppressed_warn_reaches_raw_but_not_alerts():
    logs = [_raw_dict("WARN", "Not implemented", "L1")]
    poller, pub = _make_poller(logs)
    await poller.poll_once()
    assert len(pub.raw) == 1          # still journey material
    assert len(pub.alerts) == 0       # but never an alert


# --- dedup -------------------------------------------------------------------
async def test_dedup_across_overlapping_windows():
    logs = [_raw_dict("ERROR", "boom", "L1")]
    poller, pub = _make_poller(logs)
    await poller.poll_once()          # first sight → processed
    n2 = await poller.poll_once()     # same log again (overlap) → skipped
    assert n2 == 0
    assert len(pub.raw) == 1          # not re-published
    assert len(pub.alerts) == 1


async def test_log_missing_id_is_skipped():
    bad = _raw_dict("ERROR", "boom", "L1")
    del bad["log_id"]
    poller, pub = _make_poller([bad])
    n = await poller.poll_once()
    assert n == 0 and not pub.raw


# --- alert content: fallback when LLM down -----------------------------------
async def test_alert_is_fallback_when_llm_down():
    poller, pub = _make_poller([_raw_dict("ERROR", "boom", "L1")])  # explainer=None
    await poller.poll_once()
    assert pub.alerts[0].source == "fallback"
    assert pub.alerts[0].explanation is None


async def test_alert_is_ai_when_llm_healthy():
    poller, pub = _make_poller([_raw_dict("ERROR", "boom", "L1")], healthy_llm=True)
    await poller.poll_once()
    assert pub.alerts[0].source == "ai"
    assert pub.alerts[0].department == Department.backend


# --- window helper -----------------------------------------------------------
def test_poll_window_spans_the_configured_offsets():
    from datetime import datetime, timezone

    now = datetime(2026, 7, 14, 8, 0, 30, tzinfo=timezone.utc)
    frm, to = poll_window(now, start_offset=25, end_offset=5)
    assert frm == "2026-07-14T08:00:05.000Z"  # now - 25s
    assert to == "2026-07-14T08:00:25.000Z"   # now - 5s
