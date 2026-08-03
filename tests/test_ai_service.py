"""Tests for the AI service [3].

Unit tests use fakes (breaker redis+clock, chat model, publisher channel) so the
default suite needs no LLM, RabbitMQ or Redis. (asyncio_mode=auto in pytest.ini
means async test functions run without an explicit marker.)

One live round-trip test against real RabbitMQ is gated behind the env flag
``AI_LIVE_RABBITMQ=1`` (bring the broker up with ``docker compose up -d
rabbitmq``); it is skipped by default.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from ai_service import settings
from ai_service.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from ai_service.graph import PipelineDeps, process
from ai_service import llm
from ai_service.llm import LLMError
from ai_service.nodes import route
from ai_service.publisher import Publisher
from shared.models import Department, LogLine, ProcessedAlert, Severity


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

    async def get(self, key: str):
        """Return the stored value (bytes, like real redis) or None."""
        value = self.keys.get(key)
        if value is None:
            return None
        return value if isinstance(value, bytes) else str(value).encode()


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
        router=_fake('{"department": "backend", "severity": "high"}'),
    )


# --- happy path: source="ai" -------------------------------------------------
async def test_pipeline_ai_alert_on_healthy_llm():
    alert = await process(_log(), _healthy_deps())
    assert alert.source == "ai"
    assert alert.explanation and "SPT" in alert.explanation
    assert alert.department == Department.backend
    assert alert.severity == Severity.high
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
        await route(
            _log(), "explained",
            _fake('{"department": "frontend", "severity": "high"}'),
        )


async def test_router_accepts_all_five_departments():
    for dept in Department:
        d, s = await route(
            _log(), "x",
            _fake(f'{{"department": "{dept.value}", "severity": "medium"}}'),
        )
        assert d == dept


def test_route_prompt_defines_every_department():
    """The ALLOWED list is generated from the enum; the definitions are not.

    Adding a Department therefore updates the prompt's list automatically and
    silently leaves it undefined — the model would then have to guess what the new
    label means, which is exactly the failure the guide exists to prevent. Fail
    here so the two stay in sync.
    """
    from ai_service.nodes import _DEPARTMENT_GUIDE

    missing = [d.value for d in Department if f"- {d.value}:" not in _DEPARTMENT_GUIDE]
    assert not missing, f"departments missing a prompt definition: {missing}"


def test_route_prompt_draws_the_backend_vs_general_line():
    # The misroute this prompt fixes: business-rule rejections landing on backend.
    # Assert the two load-bearing instructions survive future edits.
    from ai_service.nodes import _ROUTE_SYSTEM

    assert "NOT an engineering fault" in _ROUTE_SYSTEM
    assert "Reserve backend for an actual defect." in _ROUTE_SYSTEM


def test_route_prompt_examples_are_valid_enum_values():
    """Few-shot answers must be routable — a typo'd example teaches an LLMError."""
    import json
    import re

    from ai_service.nodes import _ROUTE_EXAMPLES

    payloads = re.findall(r'\{"department".*?\}', _ROUTE_EXAMPLES)
    assert len(payloads) >= 6, f"expected the full example set, found {len(payloads)}"
    for raw in payloads:
        obj = json.loads(raw)
        Department(obj["department"])  # raises if not a real department
        Severity(obj["severity"])
        # The router is asked for department + severity ONLY — an example that
        # still showed a confidence would teach a field the parser now ignores.
        assert set(obj) == {"department", "severity"}, f"unexpected keys in {raw}"


def test_route_prompt_examples_cover_general_and_technical_routes():
    # Paired by design: general-only examples would bias the model toward general.
    from ai_service.nodes import _ROUTE_EXAMPLES

    for dept in ("general", "networking", "database", "devops"):
        assert f'"department": "{dept}"' in _ROUTE_EXAMPLES


# The few-shot examples are only worth anything if they look like what the
# emitters actually produce. v7 is captured from the emitters, so it is the
# oracle: an example whose shape has drifted out of the corpus is teaching the
# model a line the pipeline no longer emits, and it drifts SILENTLY — nothing
# else in the suite reads the prompt against the fixture. Two real staleness bugs
# motivated this: a DLQ example still naming "order.inbound.dlq" (renamed to
# "<queue>_error" by the realignment) and a creation-failure example inventing a
# fused "DB_TIMEOUT — no order was created" line no emitter ever wrote.
_FIXTURE = Path(__file__).resolve().parent.parent / "pipeline" / "data" / "mock-order-flows-v8.json"


def _alertable_messages() -> list[str]:
    """Every WARN/ERROR message in the captured corpus.

    The fixture is a list of flow objects, each holding its lines under ``events``.
    """
    flows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return [
        log["message"]
        for flow in flows
        for log in flow.get("events", [])
        if log.get("level") in ("WARN", "ERROR")
    ]


def _mask_ids(text: str) -> str:
    """Mask the volatile values so an example matches the corpus by SHAPE.

    The three id shapes the stitcher mines and semcache normalizes — the examples
    carry their own illustrative ids (ORD-6042, evt-1a2b), which will never equal
    a captured one.

    Plus two values the EMITTERS randomize per run: the computed margin
    percentage (checker.py) and Feign call latencies. They are not ids, so
    semcache deliberately leaves them alone, but here they would make an example
    match only the one capture it was copied from — which is why re-capturing the
    fixture used to "break" a prompt that had not changed. The threshold is NOT
    masked: it is fixed, and it is the part of the sentence that carries meaning.
    """
    for pattern, token in (
        (r"evt-[0-9a-f-]{8,}", "<EVT>"),
        (r"\bORD-\d+\b", "<ORD>"),
        (r"\b\d{19}\b", "<CART>"),
        (r"\b\d{8}\b", "<ACC>"),
        (r"margin \d+\.\d+%", "margin <PCT>%"),
        (r"\(\d+ms\)", "(<MS>)"),
    ):
        text = re.sub(pattern, token, text)
    return text


@pytest.mark.skipif(not _FIXTURE.exists(), reason="mock-order-flows-v8.json not captured")
def test_route_prompt_examples_match_the_fixture_corpus():
    """Every `message=` in the few-shot must exist in the fixture, up to volatile values.

    Failing here means either the prompt drifted or an emitter's message changed —
    both need a human, because the examples are what steer the routing.
    """
    from ai_service.nodes import _ROUTE_EXAMPLES

    corpus = {_mask_ids(m) for m in _alertable_messages()}
    assert corpus, "fixture produced no WARN/ERROR logs"

    # Each example line is `message=<text> -> {json}`; take the text between them.
    examples = re.findall(r"message=(.*?) -> \{", _ROUTE_EXAMPLES, re.DOTALL)
    assert len(examples) >= 10, f"expected the full example set, found {len(examples)}"

    stale = [ex for ex in examples if _mask_ids(ex.strip()) not in corpus]
    assert not stale, (
        "few-shot examples no longer match any message the emitters produce "
        f"(update them from {_FIXTURE.name}): {stale}"
    )


@pytest.mark.skipif(not _FIXTURE.exists(), reason="mock-order-flows-v8.json not captured")
def test_route_prompt_covers_the_corpus_frequent_alert_types():
    """The recurring alert types must each be represented in the few-shot.

    Not every type needs an example — the prompt's fallback rule handles novel
    logs (the Settings anomalies deliberately have none). But a type that recurs
    and is genuinely ambiguous should be pinned, or a prompt edit can quietly drop
    the one example holding a whole class in the right department.
    """
    from ai_service.nodes import _ROUTE_EXAMPLES

    # Distinctive fragments of the classes most often misrouted, and why.
    required = {
        "margin": "below threshold",              # business rejection, not a defect
        "udf": "mandatory UDF",                   # user data, not a defect
        "sku": "No internal SKU mapping",         # reference data, not a defect
        "jam-403": "HTTP/1.1 403",                # authorization, not networking
        "spt-timeout": "SocketTimeoutException",  # transport fault
        "sap-rfc": "RFC_COMMUNICATION_FAILURE",   # transport, not integration logic
        "db": "SQLTimeoutException",              # persistence
        "dlq-init": "order.init_error",           # queue plumbing
        "dlq-sap": "order.create.sap_error",      # queue plumbing
        "retry": "(attempt 2/3)",                 # in-flight retry -> low severity
    }
    missing = [name for name, frag in required.items() if frag not in _ROUTE_EXAMPLES]
    assert not missing, f"few-shot lost coverage of: {missing}"


def test_route_prompt_states_the_severity_ladder():
    """Retry WARNs must not inherit the abort's severity.

    A single SPT outage emits two retry WARNs and an abort ERROR as three separate
    alerts; without the ladder they all rate high and one fault reads as three
    criticals in the feed.
    """
    from ai_service.nodes import _ROUTE_SYSTEM

    assert "Severity ladder" in _ROUTE_SYSTEM
    assert "retry still in flight" in _ROUTE_SYSTEM


def test_explain_prompt_grounds_business_rejections():
    """The explainer must agree with the router about what a rejection is.

    The router sends margin/UDF/account rejections to `general` ("nobody changes
    code"); an explainer calling the same log a system error puts a contradiction
    on one alert card.
    """
    from ai_service.nodes import _EXPLAIN_SYSTEM

    assert "correctly rejected" in _EXPLAIN_SYSTEM
    assert "do not name services it does not mention" in _EXPLAIN_SYSTEM


async def test_router_tolerates_code_fence_and_prose():
    d, s = await route(
        _log(), "x",
        _fake('Here you go:\n```json\n{"department": "database", "severity": "low"}\n```'),
    )
    assert d == Department.database and s == Severity.low


async def test_router_ignores_an_unrequested_confidence_key():
    """The router is no longer asked for a confidence, but a model may still
    volunteer one. That extra key must be IGNORED, not treated as bad output —
    rejecting it would turn a perfectly good route into a fallback."""
    d, s = await route(
        _log(), "x",
        _fake('{"department": "devops", "severity": "high", "confidence": 0.9}'),
    )
    assert d == Department.devops and s == Severity.high


# --- severity: validated against the enum, threaded onto the alert -----------
async def test_router_accepts_all_severities():
    for sev in Severity:
        d, s = await route(
            _log(), "x",
            _fake(f'{{"department": "backend", "severity": "{sev.value}"}}'),
        )
        assert s == sev


async def test_router_rejects_unknown_severity():
    import pytest

    with pytest.raises(LLMError):
        await route(
            _log(), "x",
            _fake('{"department": "backend", "severity": "apocalyptic"}'),
        )


# --- fallback paths: source="fallback", all null -----------------------------
def _assert_fallback(alert):
    assert alert.source == "fallback"
    assert alert.explanation is None
    assert alert.department is None
    assert alert.severity is None


async def test_pipeline_fallback_when_no_models():
    deps = PipelineDeps(breaker=_breaker(FakeRedis(), FakeClock()), explainer=None, router=None)
    _assert_fallback(await process(_log(), deps))


async def test_pipeline_fallback_when_breaker_open():
    b = _breaker(FakeRedis(), FakeClock())
    for _ in range(3):
        await b.record_failure()  # force open
    deps = PipelineDeps(breaker=b, explainer=_fake("expl"), router=_fake('{"department":"backend","severity":"low"}'))
    _assert_fallback(await process(_log(), deps))


async def test_pipeline_fallback_when_router_returns_bad_department():
    deps = PipelineDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        explainer=_fake("a clear explanation"),
        router=_fake('{"department": "nonsense"}'),
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
        severity=None if source == "fallback" else Severity.high,
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
    assert payload["severity"] == "high"
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


# --- watermark vs the collector's retention floor ----------------------------
# The collector's store is IN-MEMORY; this watermark lives in Redis and persists.
# So a collector restart (now that it has a restart policy) leaves the poller
# asking for a window whose logs are gone. Silently draining empty windows makes
# the affected journeys look like a correlation bug — they are swept as TIMED_OUT
# with no failure logs. These tests pin the loud-and-skip-forward behaviour.

def _ts(seconds: int) -> str:
    """A collector-format timestamp ``seconds`` BEFORE now.

    Relative to now, not a fixed date: ``window_from_watermark`` clamps a
    watermark older than MAX_WINDOW_SPAN (120s) forward to ``to - 120s``, so a
    fixed past date would never reach poll_once as written and these tests would
    exercise the clamp instead of the retention check.
    """
    from datetime import timedelta

    from ai_service.poller import _format_ts

    return _format_ts(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _poller_with_retention(
    *, oldest: str | None, windows: dict[str, list[dict]] | None = None
):
    """A Poller whose /health reports ``oldest`` and whose fetch_logs is per-``from``.

    ``windows`` maps a ``from`` value to the logs returned for it, so a test can
    show an empty first read and a populated re-read after the skip.
    """
    redis = FakeRedis()
    pub = FakePublisher()
    deps = PipelineDeps(breaker=_breaker(redis, FakeClock()), explainer=None, router=None)
    poller = Poller(redis=redis, publisher=pub, pipeline_deps=deps, http=object())

    calls: list[str] = []

    async def fetch_logs(from_iso, to_iso):
        calls.append(from_iso)
        return list((windows or {}).get(from_iso, []))

    async def fetch_oldest():
        return oldest

    poller.fetch_logs = fetch_logs  # type: ignore[method-assign]
    poller.fetch_oldest_timestamp = fetch_oldest  # type: ignore[method-assign]
    return poller, pub, calls


async def test_stale_watermark_is_skipped_forward_and_rereads(capsys):
    """Watermark behind the retention floor → warn, then re-read from the floor."""
    stale, floor = _ts(100), _ts(40)  # watermark 100s ago, floor only 40s ago
    recovered = _raw_dict("ERROR", "boom", "L1")
    poller, pub, calls = _poller_with_retention(
        oldest=floor, windows={floor: [recovered]}
    )
    await poller._write_watermark(stale)

    n = await poller.poll_once()

    # Re-read from the retention floor, so the cycle still does useful work.
    assert calls == [stale, floor]
    assert n == 1 and [l.log_id for l in pub.raw] == ["L1"]
    # And the loss is LOUD, not silent.
    out = capsys.readouterr().out
    assert "WARNING" in out and floor in out


async def test_empty_window_within_retention_is_not_treated_as_loss(capsys):
    """A genuinely quiet period must not warn or re-read."""
    watermark, floor = _ts(30), _ts(100)  # floor is OLDER → nothing was lost
    poller, pub, calls = _poller_with_retention(oldest=floor, windows={})
    await poller._write_watermark(watermark)

    n = await poller.poll_once()

    assert n == 0
    assert calls == [watermark]  # no second fetch
    assert "WARNING" not in capsys.readouterr().out


async def test_retention_check_is_skipped_when_the_window_had_logs():
    """The healthy path must not pay for an extra /health request."""
    # Bind once: _ts() is relative to now, so recomputing it would not match.
    watermark = _ts(30)
    poller, pub, calls = _poller_with_retention(
        oldest=_ts(100), windows={watermark: [_raw_dict("INFO", "ok", "L1")]}
    )
    await poller._write_watermark(watermark)

    checked = []

    async def spy():
        checked.append(True)
        return _ts(100)

    poller.fetch_oldest_timestamp = spy  # type: ignore[method-assign]

    await poller.poll_once()
    assert checked == []          # never consulted
    assert calls == [watermark]


async def test_cold_start_does_not_report_loss(capsys):
    """No watermark yet → nothing can have been lost."""
    poller, pub, calls = _poller_with_retention(oldest=_ts(50), windows={})
    n = await poller.poll_once()   # no watermark written
    assert n == 0
    assert "WARNING" not in capsys.readouterr().out


async def test_unavailable_retention_floor_degrades_silently(capsys):
    """An older collector (no oldest_timestamp) must poll exactly as before."""
    watermark = _ts(30)
    poller, pub, calls = _poller_with_retention(oldest=None, windows={})
    await poller._write_watermark(watermark)
    n = await poller.poll_once()
    assert n == 0
    assert calls == [watermark]    # no skip attempted
    assert "WARNING" not in capsys.readouterr().out


async def test_fetch_oldest_timestamp_never_raises():
    """It is a diagnostic: any transport/shape failure returns None."""
    redis = FakeRedis()
    deps = PipelineDeps(breaker=_breaker(redis, FakeClock()), explainer=None, router=None)
    poller = Poller(
        redis=redis, publisher=FakePublisher(), pipeline_deps=deps, http=object()
    )

    class Boom:
        async def get(self, *a, **k):
            raise RuntimeError("collector down")

    poller._http = Boom()  # type: ignore[assignment]
    assert await poller.fetch_oldest_timestamp() is None


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


# =============================================================================
# Poller — watermark: windows are contiguous, never skip wall-clock time
# =============================================================================
from datetime import datetime as _dt, timedelta as _td, timezone as _tz  # noqa: E402


def _wm_poller(window_logs: list[dict]):
    """A watermark-enabled poller with a fake redis we can inspect."""
    redis = FakeRedis()
    pub = FakePublisher()
    deps = PipelineDeps(breaker=_breaker(redis, FakeClock()), explainer=None, router=None)
    poller = Poller(redis=redis, publisher=pub, pipeline_deps=deps, http=object())
    calls: list[tuple[str, str]] = []

    async def _fetch(f, t):
        calls.append((f, t))
        return window_logs

    poller.fetch_logs = _fetch  # type: ignore[method-assign]
    return poller, pub, redis, calls


async def test_first_poll_uses_start_offset_when_no_watermark():
    """With no stored watermark, the first window falls back to now-start_offset
    (so we never replay all of history on a cold start)."""
    poller, pub, redis, calls = _wm_poller([])
    now = _dt(2026, 7, 14, 8, 0, 30, tzinfo=_tz.utc)
    await poller.poll_once(now=now)
    frm, to = calls[0]
    assert frm == "2026-07-14T08:00:05.000Z"   # now - 25s (start_offset)
    assert to == "2026-07-14T08:00:25.000Z"    # now - 5s  (end_offset)


async def test_watermark_makes_consecutive_windows_contiguous():
    """A slow cycle must not leave a gap: the next window starts exactly where
    the previous one ended, regardless of how much wall-clock elapsed."""
    poller, pub, redis, calls = _wm_poller([])
    t0 = _dt(2026, 7, 14, 8, 0, 30, tzinfo=_tz.utc)
    await poller.poll_once(now=t0)
    first_to = calls[0][1]

    # 40s later (far longer than any window overlap) — old code would skip the
    # 08:00:25 .. 08:01:05 span entirely; watermark must cover it.
    t1 = t0 + _td(seconds=40)
    await poller.poll_once(now=t1)
    second_from, second_to = calls[1]
    assert second_from == first_to             # contiguous — no gap
    assert second_to == "2026-07-14T08:01:05.000Z"  # t1 - 5s


async def test_watermark_persisted_across_poller_instances():
    """The watermark lives in Redis so a restart resumes where it left off."""
    logs = []
    redis = FakeRedis()
    pub = FakePublisher()
    deps = PipelineDeps(breaker=_breaker(redis, FakeClock()), explainer=None, router=None)

    p1 = Poller(redis=redis, publisher=pub, pipeline_deps=deps, http=object())
    seen1: list[tuple[str, str]] = []
    p1.fetch_logs = lambda f, t: _record(seen1, f, t, logs)  # type: ignore[method-assign]
    t0 = _dt(2026, 7, 14, 8, 0, 30, tzinfo=_tz.utc)
    await p1.poll_once(now=t0)

    # a fresh poller (restart) sharing the same redis picks up the watermark
    p2 = Poller(redis=redis, publisher=pub, pipeline_deps=deps, http=object())
    seen2: list[tuple[str, str]] = []
    p2.fetch_logs = lambda f, t: _record(seen2, f, t, logs)  # type: ignore[method-assign]
    t1 = t0 + _td(seconds=30)
    await p2.poll_once(now=t1)
    assert seen2[0][0] == seen1[0][1]  # p2's window starts at p1's window end


async def _record(sink, f, t, value):
    sink.append((f, t))
    return value


async def test_watermark_caps_lookback_after_long_stall():
    """After a very long stall, the window must not span an unbounded range;
    it is capped so we don't fetch hours of logs at once."""
    poller, pub, redis, calls = _wm_poller([])
    t0 = _dt(2026, 7, 14, 8, 0, 30, tzinfo=_tz.utc)
    await poller.poll_once(now=t0)
    # 1 hour later
    t1 = t0 + _td(hours=1)
    await poller.poll_once(now=t1)
    frm, to = calls[1]
    # span is capped (MAX_WINDOW_SPAN); not the full hour
    span = _dt.fromisoformat(to.replace("Z", "+00:00")) - _dt.fromisoformat(frm.replace("Z", "+00:00"))
    assert span <= _td(seconds=settings.MAX_WINDOW_SPAN)


# =============================================================================
# Poller — LLM off the critical path: raw.events published before/independent
# of the (slow) alert LLM processing
# =============================================================================
async def test_raw_events_published_before_alert_llm_runs():
    """raw.events for ALL logs must be published without waiting on the LLM.

    We make the alert pipeline block on an event; raw must already be published
    while the alert processing is still pending."""
    logs = [_raw_dict("INFO", "a", "L1"), _raw_dict("ERROR", "boom", "L2"),
            _raw_dict("INFO", "c", "L3")]
    redis = FakeRedis()
    pub = FakePublisher()

    gate = asyncio.Event()

    # a deps whose explainer blocks until we release the gate
    class _BlockingModel:
        async def ainvoke(self, messages):
            await gate.wait()
            from langchain_core.messages import AIMessage
            return AIMessage(content="explained")

    deps = PipelineDeps(
        breaker=_breaker(redis, FakeClock()),
        explainer=_BlockingModel(),
        router=_fake('{"department": "backend", "severity": "medium"}'),
    )
    poller = Poller(redis=redis, publisher=pub, pipeline_deps=deps, http=object())
    poller.fetch_logs = lambda f, t: _async(logs)  # type: ignore[method-assign]

    task = asyncio.create_task(poller.poll_once())
    # give the loop a moment to publish raw + kick off alert processing
    for _ in range(50):
        await asyncio.sleep(0)
        if len(pub.raw) == 3:
            break
    # all raw events are out even though the alert LLM is still blocked
    assert {l.log_id for l in pub.raw} == {"L1", "L2", "L3"}
    assert pub.alerts == []  # alert still pending on the gate

    gate.set()  # release the LLM
    await task
    assert len(pub.alerts) == 1 and pub.alerts[0].log.log_id == "L2"


# =============================================================================
# Journey summary API — TestClient, fake summary model / breaker-open fallback
# =============================================================================
from fastapi.testclient import TestClient  # noqa: E402

from ai_service import api  # noqa: E402


def _summary_request() -> dict:
    return {
        "journey_id": "J1",
        "outcome": "ENRICHMENT_FAILED",
        "event_id": "evt-1",
        "order_id": "ORD-6008",
        "cart_header_id": None,
        "logs": [
            _log(level="INFO", message="Get order by Order Number:ORD-6008").model_dump(mode="json"),
            _log(level="ERROR", message="Order processing aborted: SPT unavailable").model_dump(mode="json"),
        ],
    }


def test_health_reports_fallback_without_model():
    api.configure(api.SummaryDeps(breaker=_breaker(FakeRedis(), FakeClock()), model=None))
    client = TestClient(api.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "llm": "fallback"}


def test_summarize_journey_ai_path():
    api.configure(api.SummaryDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        model=_fake("Order ORD-6008 was created then failed at SPT enrichment."),
    ))
    client = TestClient(api.app)
    resp = client.post("/summarize-journey", json=_summary_request())
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "ai"
    assert body["journey_id"] == "J1"
    assert "SPT" in body["summary"]


def test_summarize_journey_falls_back_to_template_when_no_model():
    api.configure(api.SummaryDeps(breaker=_breaker(FakeRedis(), FakeClock()), model=None))
    client = TestClient(api.app)
    resp = client.post("/summarize-journey", json=_summary_request())
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    # deterministic template built from meta: outcome + services touched
    assert "ENRICHMENT_FAILED" in body["summary"]
    assert "ORD-6008" in body["summary"]
    assert "cc-spt-service" in body["summary"]  # the app_name from the logs


def _summary_request_unrecognized() -> dict:
    req = _summary_request()
    req["outcome"] = "UNRECOGNIZED_FAILURE"
    req["logs"] = [
        _log(level="INFO", message="Get order by Order Number:ORD-6015").model_dump(mode="json"),
        _log(level="ERROR", message="ConnectionPoolExhaustedError: settings cache unavailable").model_dump(mode="json"),
    ]
    return req


def test_summarize_journey_returns_suggested_label_for_unrecognized_failure():
    api.configure(api.SummaryDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        model=_fake('{"summary": "The order stalled at settings enrichment.", '
                    '"label": "Settings cache connection pool exhausted"}'),
    ))
    client = TestClient(api.app)
    resp = client.post("/summarize-journey", json=_summary_request_unrecognized())
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "ai"
    assert body["summary"] == "The order stalled at settings enrichment."
    assert body["suggested_label"] == "Settings cache connection pool exhausted"


def test_summarize_journey_no_label_for_recognized_outcomes():
    # Every existing outcome (named subtypes, SUCCESS, plain TIMED_OUT) must
    # never get a label, regardless of what the model would say — the branch
    # only exists for UNRECOGNIZED_FAILURE.
    api.configure(api.SummaryDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        model=_fake("Order ORD-6008 was created then failed at SPT enrichment."),
    ))
    client = TestClient(api.app)
    resp = client.post("/summarize-journey", json=_summary_request())  # outcome=ENRICHMENT_FAILED
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("suggested_label") is None
    assert "SPT" in body["summary"]  # existing behavior, byte-for-byte unchanged


def test_summarize_journey_label_falls_back_to_none_when_breaker_open():
    api.configure(api.SummaryDeps(breaker=_breaker(FakeRedis(), FakeClock()), model=None))
    client = TestClient(api.app)
    resp = client.post("/summarize-journey", json=_summary_request_unrecognized())
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "fallback"
    assert body.get("suggested_label") is None
    assert "UNRECOGNIZED_FAILURE" in body["summary"]  # existing template, unchanged


def test_summarize_journey_degrades_gracefully_on_malformed_json_label_reply():
    # The model ignores the JSON instruction and replies with plain prose —
    # must still produce a usable summary, just no label. Never raises.
    api.configure(api.SummaryDeps(
        breaker=_breaker(FakeRedis(), FakeClock()),
        model=_fake("The settings cache could not be reached."),
    ))
    client = TestClient(api.app)
    resp = client.post("/summarize-journey", json=_summary_request_unrecognized())
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "ai"
    assert body["summary"] == "The settings cache could not be reached."
    assert body.get("suggested_label") is None


from ai_service.nodes import SummaryResult, _parse_summary_with_label  # noqa: E402


def test_parse_summary_with_label_happy_path():
    result = _parse_summary_with_label(
        '{"summary": "Settings cache was unreachable.", "label": "Settings cache down"}'
    )
    assert result == SummaryResult(summary="Settings cache was unreachable.", suggested_label="Settings cache down")


def test_parse_summary_with_label_tolerates_surrounding_prose():
    result = _parse_summary_with_label(
        'Sure, here you go:\n{"summary": "It failed.", "label": "X"}\nHope that helps!'
    )
    assert result.summary == "It failed."
    assert result.suggested_label == "X"


def test_parse_summary_with_label_degrades_when_not_json():
    result = _parse_summary_with_label("It just failed, no idea why.")
    assert result.summary == "It just failed, no idea why."
    assert result.suggested_label is None


def test_parse_summary_with_label_degrades_when_label_key_missing():
    result = _parse_summary_with_label('{"summary": "It failed."}')
    assert result.summary == "It failed."
    assert result.suggested_label is None


def test_template_summary_is_deterministic_and_llm_free():
    req = api.SummaryRequest(**_summary_request())
    s1 = api.template_summary(req)
    s2 = api.template_summary(req)
    assert s1 == s2
    assert str(len(req.logs)) in s1  # mentions the log count


def test_summary_request_contract_shape():
    # The backend must match this contract — assert the fields it depends on.
    req = api.SummaryRequest(**_summary_request())
    assert req.journey_id and req.outcome
    assert isinstance(req.logs[0], LogLine)  # logs deserialize to the model


# --- llm.py: per-model LangSmith tags ----------------------------------------
#
# Each factory tags its model with the logical role it plays so runs land in
# LangSmith already labelled, instead of as N indistinguishable calls to the same
# Azure endpoint. Asserted here because nothing downstream can see the tags —
# nodes.py only ever awaits `.ainvoke()`.


@pytest.fixture
def _llm_configured(monkeypatch):
    """Point llm.py at a fake Azure endpoint so _build() actually constructs.

    The provider model builds offline (no network call at construction), so this
    exercises the real code path without creds or a request.
    """
    monkeypatch.setattr(settings, "AZURE_AI_FOUNDRY_ENDPOINT", "https://fake.invalid/openai/v1")
    monkeypatch.setattr(settings, "AZURE_AI_FOUNDRY_API_KEY", "fake-key")
    for name in (
        "AZURE_DEPLOYMENT_EXPLAINER",
        "AZURE_DEPLOYMENT_ROUTER",
        "AZURE_DEPLOYMENT_SUMMARY",
        "AZURE_DEPLOYMENT_CHAT",
    ):
        monkeypatch.setattr(settings, name, "fake-deployment")


def test_explainer_model_is_tagged_explainer(_llm_configured):
    model = llm.explainer_model()
    assert model is not None
    # with_config stores tags on the RunnableBinding's `config` dict — there is no
    # `.tags` attribute in langchain-core 1.x, so read it the way the API exposes it.
    assert "explainer" in model.config.get("tags", [])


@pytest.mark.parametrize(
    "factory, tag",
    [
        ("explainer_model", "explainer"),
        ("router_model", "router"),
        ("summary_model", "summary"),
        ("chat_model", "chat"),
    ],
)
def test_every_factory_tags_its_logical_role(_llm_configured, factory, tag):
    model = getattr(llm, factory)()
    assert model is not None
    assert model.config.get("tags") == [tag]


def test_tagged_model_still_supports_ainvoke(_llm_configured):
    """The contract nodes.py depends on: a Runnable you can await, whose result
    carries `.content`. with_config wraps the model in a RunnableBinding, so this
    pins that the wrapper stays duck-type compatible."""
    model = llm.explainer_model()
    assert hasattr(model, "ainvoke")


def test_chat_falls_back_to_summary_deployment_but_keeps_the_chat_tag(monkeypatch):
    """The tag names the JOB, not the deployment — which is the whole point when
    chat and summary share one deployment."""
    monkeypatch.setattr(settings, "AZURE_AI_FOUNDRY_ENDPOINT", "https://fake.invalid/openai/v1")
    monkeypatch.setattr(settings, "AZURE_AI_FOUNDRY_API_KEY", "fake-key")
    monkeypatch.setattr(settings, "AZURE_DEPLOYMENT_CHAT", "")
    monkeypatch.setattr(settings, "AZURE_DEPLOYMENT_SUMMARY", "summary-deployment")

    model = llm.chat_model()
    assert model is not None
    assert model.config.get("tags") == ["chat"]


def test_factories_still_return_none_without_creds(monkeypatch):
    """Tagging must not disturb the creds-free path — None means "take the
    fallback", and every factory has to keep saying it."""
    monkeypatch.setattr(settings, "AZURE_AI_FOUNDRY_ENDPOINT", "")
    monkeypatch.setattr(settings, "AZURE_AI_FOUNDRY_API_KEY", "")
    assert llm.explainer_model() is None
    assert llm.router_model() is None
    assert llm.summary_model() is None
    assert llm.chat_model() is None


def test_build_without_tags_returns_the_bare_model(_llm_configured):
    """`with_config(tags=None)` is a pydantic ValidationError, so an untagged
    build must not go through it. Guards the optional-parameter default."""
    model = llm._build("fake-deployment")
    assert model is not None
    assert not hasattr(model, "config")  # not wrapped in a RunnableBinding
    assert hasattr(model, "ainvoke")
