"""Tests for backend/rag_client.py — the retrieval-index push client.

No network: ``push`` takes an injected httpx-like client, so the success path, the
AI-service-down path, and the request shape are all exercised without a running
AI service. Fakes follow the ``_FakeClient``/``_FakeResp`` style of
``tests/test_summarizer.py``.

The central property under test is **failure isolation**: an index push is a
best-effort side channel on the alert-persist and journey-completion paths, so it
must swallow every failure rather than propagate. A raise here would nack a
message whose row is already committed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.journeys import Completion, JourneyStatus
from backend.rag_client import (
    alert_text,
    index_alert,
    index_journey,
    journey_text,
    push,
)
from backend.stitching import StitchedJourney
from shared.models import Department, LogLine, ProcessedAlert, Severity

BASE = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)


def _log(**over) -> LogLine:
    base = dict(
        log_id="log-1", timestamp=BASE, app_name="cc-checker-service", level="ERROR",
        logger="c.c.checker.service.MarginService", host="H", process_id="1", thread="t",
        message="Margin check FAILED for order ORD-6042", orderId="ORD-6042",
        cartHeaderId="1840927365018240042",
    )
    base.update(over)
    return LogLine(**base)


def _alert(**over) -> ProcessedAlert:
    base = dict(
        alert_id="alert-1", emitted_at=BASE, log=_log(),
        explanation="The order was blocked because its margin fell below the threshold.",
        department=Department.general, severity=Severity.medium, confidence=0.9, source="ai",
    )
    base.update(over)
    return ProcessedAlert(**base)


def _completion(summary_ready: bool = True) -> Completion:
    journey = StitchedJourney(journey_id="J1")
    journey.event_id = "evt-1"
    journey.order_id = "ORD-6042"
    journey.cart_header_id = "1840927365018240042"
    # first_ts/last_ts are derived from `logs`, not settable — so the last log's
    # timestamp is what index_journey reads for its "ts" metadata.
    journey.logs = [
        _log(level="INFO", message="Received inbound order event evt-1"),
        _log(log_id="log-2", timestamp=BASE + timedelta(seconds=5)),
    ]
    return Completion(
        journey_id="J1", journey=journey, status=JourneyStatus.FAILED,
        outcome="MARGIN_CHECK_FAILED",
    )


# --- fakes -------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")


class _FakeClient:
    """Records posts; optionally raises (service down) or returns a bad status."""

    def __init__(self, resp=None, boom=False):
        self._resp = resp if resp is not None else _FakeResp({"indexed": True})
        self._boom = boom
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self._boom:
            raise RuntimeError("connection refused")
        return self._resp


# =============================================================================
# pure text builders
# =============================================================================
def test_alert_text_includes_the_explanation():
    text = alert_text(
        "cc-checker-service", "ERROR", "c.c.checker.M", "Margin check FAILED", "It was blocked."
    )
    assert text == "cc-checker-service ERROR c.c.checker.M: Margin check FAILED. It was blocked."


def test_alert_text_without_an_explanation_has_no_dangling_separator():
    # A source="fallback" alert has explanation=None.
    text = alert_text("cc-spt-service", "WARN", "c.c.spt.S", "Retrying", None)
    assert text == "cc-spt-service WARN c.c.spt.S: Retrying."
    assert not text.endswith(". ")


def test_journey_text_shape():
    assert journey_text("SUCCESS", "All good.") == "Journey SUCCESS: All good."


# =============================================================================
# push — the failure-isolation contract
# =============================================================================
async def test_push_success_returns_true_and_posts_the_contract():
    client = _FakeClient()
    assert await push("a1", "alert", "some text", {"level": "ERROR"}, client=client) is True
    url, body = client.calls[0]
    assert url.endswith("/index")
    assert body == {"id": "a1", "kind": "alert", "text": "some text",
                    "metadata": {"level": "ERROR"}}


async def test_push_swallows_a_connection_failure():
    """The AI service being down must NOT raise — this is the whole point."""
    client = _FakeClient(boom=True)
    assert await push("a1", "alert", "t", {}, client=client) is False  # no exception


async def test_push_swallows_a_non_2xx():
    client = _FakeClient(_FakeResp({}, status=500))
    assert await push("a1", "alert", "t", {}, client=client) is False


async def test_push_swallows_a_malformed_body():
    class _Bad:
        def json(self):
            raise ValueError("not json")

        def raise_for_status(self):
            return None

    assert await push("a1", "alert", "t", {}, client=_FakeClient(_Bad())) is False


async def test_push_reports_false_when_the_index_declined():
    client = _FakeClient(_FakeResp({"indexed": False}))
    assert await push("a1", "alert", "t", {}, client=client) is False


async def test_push_drops_null_metadata():
    # A null would be compared against a filter value instead of excluding the
    # record, so nulls must never reach the index.
    client = _FakeClient()
    await push("a1", "alert", "t", {"order_id": None, "level": "ERROR"}, client=client)
    assert client.calls[0][1]["metadata"] == {"level": "ERROR"}


# =============================================================================
# index_alert / index_journey
# =============================================================================
async def test_index_alert_builds_text_and_metadata():
    client = _FakeClient()
    assert await index_alert(_alert(), client=client) is True
    _url, body = client.calls[0]
    assert body["id"] == "alert-1"
    assert body["kind"] == "alert"
    assert "Margin check FAILED" in body["text"]
    assert "margin fell below the threshold" in body["text"]
    assert body["metadata"]["department"] == "general"
    assert body["metadata"]["severity"] == "medium"
    assert body["metadata"]["app_name"] == "cc-checker-service"
    assert body["metadata"]["order_id"] == "ORD-6042"
    assert body["metadata"]["ts"] == BASE.isoformat()


async def test_index_alert_handles_a_fallback_alert():
    """A fallback alert has null enrichment; those keys must be dropped, not null."""
    client = _FakeClient()
    alert = _alert(explanation=None, department=None, severity=None, confidence=None,
                   source="fallback")
    assert await index_alert(alert, client=client) is True
    metadata = client.calls[0][1]["metadata"]
    assert "department" not in metadata and "severity" not in metadata
    assert metadata["source"] == "fallback"


async def test_index_alert_never_raises_when_the_service_is_down():
    assert await index_alert(_alert(), client=_FakeClient(boom=True)) is False


async def test_index_journey_builds_text_and_metadata():
    client = _FakeClient()
    assert await index_journey(_completion(), "It was blocked by the margin check.",
                               client=client) is True
    _url, body = client.calls[0]
    assert body["id"] == "J1"
    assert body["kind"] == "journey"
    assert body["text"] == "Journey MARGIN_CHECK_FAILED: It was blocked by the margin check."
    assert body["metadata"]["outcome"] == "MARGIN_CHECK_FAILED"
    assert body["metadata"]["status"] == "FAILED"
    assert body["metadata"]["order_id"] == "ORD-6042"


@pytest.mark.parametrize("summary", [None, "", "   "])
async def test_index_journey_skips_a_summaryless_journey(summary):
    """"Journey SUCCESS:" with nothing after it carries no retrievable signal."""
    client = _FakeClient()
    assert await index_journey(_completion(), summary, client=client) is False
    assert client.calls == []  # not even attempted


async def test_index_journey_never_raises_when_the_service_is_down():
    assert await index_journey(_completion(), "s", client=_FakeClient(boom=True)) is False


# =============================================================================
# assembler integration — a failing indexer must not break completion
# =============================================================================
async def test_assembler_completion_survives_an_exploding_indexer():
    """The indexer is wired into _summaries_for; if it raises, journey completion
    must still return its summaries (backend/journeys.py isolates the sink)."""
    from backend.journeys import JourneyAssembler, SummaryResult

    async def _summarizer(_completion):
        return SummaryResult(summary="the summary")

    async def _indexer(_completion, _summary):
        raise RuntimeError("index exploded")

    assembler = JourneyAssembler(summarizer=_summarizer, indexer=_indexer)
    summaries = await assembler._summaries_for([_completion()])
    assert summaries == {"J1": SummaryResult(summary="the summary")}  # completion unaffected


async def test_assembler_passes_the_summary_to_the_indexer():
    from backend.journeys import JourneyAssembler, SummaryResult

    seen: list[tuple[str, str | None]] = []

    async def _summarizer(_completion):
        return SummaryResult(summary="the summary")

    async def _indexer(completion, summary):
        seen.append((completion.journey_id, summary))

    assembler = JourneyAssembler(summarizer=_summarizer, indexer=_indexer)
    await assembler._summaries_for([_completion()])
    assert seen == [("J1", "the summary")]


async def test_assembler_without_an_indexer_is_unchanged():
    from backend.journeys import JourneyAssembler, SummaryResult

    async def _summarizer(_completion):
        return SummaryResult(summary="s")

    assembler = JourneyAssembler(summarizer=_summarizer)  # no indexer
    assert await assembler._summaries_for([_completion()]) == {"J1": SummaryResult(summary="s")}
