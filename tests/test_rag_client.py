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


def test_human_time_is_readable_and_utc_labelled():
    """Regression: raw ISO timestamps were injected into the prompt, and the model
    quoted them verbatim — an agent got "2026-07-27T16:26:21.815188+00:00" in the
    middle of a sentence. A UI formatter cannot reach inside generated prose, so
    the fix is to format on the way IN."""
    from backend.rag_client import human_time

    assert human_time(BASE) == "20 Jul 2026 08:00:00 UTC"
    # No ISO date/time separator and no microseconds — that shape is what made the
    # quoted value unreadable. ("T" alone would match the T in "UTC".)
    assert "2026-07-20T" not in human_time(BASE)
    assert ".000000" not in human_time(BASE)
    assert "+00:00" not in human_time(BASE)
    assert "UTC" in human_time(BASE)            # timezone stated, never implied


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 7, 27, 16, 26, 21, 815188, tzinfo=timezone.utc),
        "2026-07-27T16:26:21.815188+00:00",
        "2026-07-27T16:26:21Z",
        datetime(2026, 7, 27, 16, 26, 21),  # naive -> assumed UTC
    ],
)
def test_human_time_accepts_datetimes_and_iso_strings(value):
    from backend.rag_client import human_time

    assert human_time(value) == "27 Jul 2026 16:26:21 UTC"


def test_human_time_renders_in_a_supplied_timezone():
    """The browser sends its IANA zone, so a scoped answer quotes a time the reader
    recognises rather than UTC."""
    from backend.rag_client import human_time

    utc = datetime(2026, 7, 27, 16, 26, 21, tzinfo=timezone.utc)
    assert human_time(utc) == "27 Jul 2026 16:26:21 UTC"
    assert human_time(utc, "Europe/Bucharest") == "27 Jul 2026 19:26:21 EEST"
    assert human_time(utc, "America/New_York") == "27 Jul 2026 12:26:21 EDT"
    # Crossing midnight must roll the date, not just the clock.
    assert human_time(utc, "Asia/Tokyo") == "28 Jul 2026 01:26:21 JST"


def test_human_time_falls_back_to_utc_for_an_unknown_zone():
    """An invalid zone degrades to LABELLED UTC. Never raise, and never emit an
    unlabelled time the reader would assume is local."""
    from backend.rag_client import human_time

    utc = datetime(2026, 7, 27, 16, 26, 21, tzinfo=timezone.utc)
    assert human_time(utc, "Mars/Olympus") == "27 Jul 2026 16:26:21 UTC"
    assert human_time(utc, "") == "27 Jul 2026 16:26:21 UTC"


def test_human_time_always_labels_the_zone():
    """Regression guard: an unlabelled local-looking time is worse than UTC."""
    from backend.rag_client import human_time

    utc = datetime(2026, 7, 27, 16, 26, 21, tzinfo=timezone.utc)
    for tz in (None, "Europe/Bucharest", "Asia/Tokyo", "bogus"):
        assert human_time(utc, tz).split()[-1].isalpha()


def test_human_time_passes_none_and_unparseable_through():
    """None in, None out, so a caller drops the fact rather than emitting an empty
    one; an unparseable value survives rather than being silently lost."""
    from backend.rag_client import human_time

    assert human_time(None) is None
    assert human_time("") is None
    assert human_time("not-a-date") == "not-a-date"


def test_indexed_text_carries_a_readable_time_not_iso():
    from backend.rag_client import human_time

    text = alert_text("a", "ERROR", "l", "m", None, ts=human_time(BASE))
    assert "at=20 Jul 2026 08:00:00 UTC" in text
    assert "T08:00:00" not in text


async def test_indexed_text_is_readable_but_metadata_stays_iso():
    """Only the LLM-facing text is humanized. Metadata ``ts`` must stay ISO-8601 —
    it is for equality filters and ordering, not for reading."""
    client = _FakeClient()
    await index_alert(_alert(), client=client)
    _url, body = client.calls[0]
    assert "at=20 Jul 2026 08:00:00 UTC" in body["text"]   # readable in the text
    assert body["metadata"]["ts"] == BASE.isoformat()      # machine-readable in metadata


def test_alert_text_appends_ids_and_timestamp():
    """The ids/time go in the TEXT, not just metadata: the AI service only ever
    shows the model ``text``, so "which journey is this?" / "when did it happen?"
    were unanswerable while those values lived in metadata alone."""
    text = alert_text(
        "cc-checker-service", "ERROR", "c.c.M", "Margin FAILED", "Blocked.",
        order_id="ORD-1", journey_id="J1", ts="2026-07-27T08:00:00+00:00",
    )
    assert "order_id=ORD-1" in text
    assert "journey_id=J1" in text
    assert "at=2026-07-27T08:00:00+00:00" in text
    # The prose still leads — the fact clause is appended, not prepended, so it
    # cannot outweigh the message in the embedding.
    assert text.startswith("cc-checker-service ERROR")


def test_alert_text_omits_absent_facts_without_empty_brackets():
    assert alert_text("a", "WARN", "l", "m", None) == "a WARN l: m."


def test_journey_text_appends_ids_and_window():
    text = journey_text(
        "SAP_SUBMISSION_FAILED", "SAP was unreachable.",
        order_id="ORD-2", journey_id="J2",
        started="2026-07-27T08:00:00+00:00", ended="2026-07-27T08:00:09+00:00",
    )
    assert "order_id=ORD-2" in text and "journey_id=J2" in text
    assert "started=2026-07-27T08:00:00+00:00" in text
    assert "ended=2026-07-27T08:00:09+00:00" in text


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
    # The prose leads; ids/timestamps are appended as a fact clause so the
    # composer can read them (see test_journey_text_appends_ids_and_window).
    assert body["text"].startswith(
        "Journey MARGIN_CHECK_FAILED: It was blocked by the margin check."
    )
    assert "order_id=ORD-6042" in body["text"]
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
    from backend.journeys import JourneyAssembler

    async def _summarizer(_completion):
        return "the summary"

    async def _indexer(_completion, _summary):
        raise RuntimeError("index exploded")

    assembler = JourneyAssembler(summarizer=_summarizer, indexer=_indexer)
    summaries = await assembler._summaries_for([_completion()])
    assert summaries == {"J1": "the summary"}  # completion unaffected


async def test_assembler_passes_the_summary_to_the_indexer():
    from backend.journeys import JourneyAssembler

    seen: list[tuple[str, str | None]] = []

    async def _summarizer(_completion):
        return "the summary"

    async def _indexer(completion, summary):
        seen.append((completion.journey_id, summary))

    assembler = JourneyAssembler(summarizer=_summarizer, indexer=_indexer)
    await assembler._summaries_for([_completion()])
    assert seen == [("J1", "the summary")]


async def test_assembler_without_an_indexer_is_unchanged():
    from backend.journeys import JourneyAssembler

    async def _summarizer(_completion):
        return "s"

    assembler = JourneyAssembler(summarizer=_summarizer)  # no indexer
    assert await assembler._summaries_for([_completion()]) == {"J1": "s"}
