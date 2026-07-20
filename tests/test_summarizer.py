"""Tests for backend/summarizer.py — the journey-summary client for the AI service.

No network: fetch_summary takes an injected httpx-like client, so we exercise the
success path, the AI-service-down path, and the request-contract shape without a
running AI service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend.journeys import Completion, JourneyStatus
from backend.stitching import StitchedJourney
from backend.summarizer import build_request, fetch_summary
from shared.models import LogLine

BASE = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)


def _log(offset_s, message, **ids):
    return LogLine(
        log_id=f"log-{offset_s}", timestamp=BASE + timedelta(seconds=offset_s),
        app_name="cc-order-engine", level="INFO", logger="c.c.test.L",
        host="H", process_id="1", thread="t", message=message, **ids,
    )


def _completion() -> Completion:
    journey = StitchedJourney(journey_id="J1")
    journey.event_id = "evt-1"
    journey.order_id = "ORD-1"
    journey.cart_header_id = "C1"
    journey.logs = [
        _log(0, "Received inbound order event evt-1", eventId="evt-1"),
        _log(5, "Registered order ORD-1 for tracking", orderId="ORD-1", cartHeaderId="C1"),
    ]
    return Completion(
        journey_id="J1", journey=journey, status=JourneyStatus.SUCCESS, outcome="SUCCESS"
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
    def __init__(self, resp=None, boom=False):
        self._resp = resp
        self._boom = boom
        self.calls = []

    async def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self._boom:
            raise ConnectionError("AI service unreachable")
        return self._resp


# --- build_request matches the AI service's SummaryRequest contract ----------
def test_build_request_shape():
    body = build_request(_completion())
    assert body["journey_id"] == "J1"
    assert body["outcome"] == "SUCCESS"
    assert body["event_id"] == "evt-1"
    assert body["order_id"] == "ORD-1"
    assert body["cart_header_id"] == "C1"
    # logs are serialized LogLine dicts, in order
    assert [l["message"] for l in body["logs"]][0].startswith("Received inbound")
    assert len(body["logs"]) == 2


def test_build_request_validates_against_ai_service_contract():
    # The AI service owns SummaryRequest — the backend's body must deserialize.
    from ai_service.api import SummaryRequest

    req = SummaryRequest(**build_request(_completion()))
    assert req.journey_id == "J1" and isinstance(req.logs[0], LogLine)


# --- fetch_summary happy + failure paths -------------------------------------
async def test_fetch_summary_returns_text_on_success():
    client = _FakeClient(_FakeResp({"journey_id": "J1", "summary": "All good.", "source": "ai"}))
    result = await fetch_summary(_completion(), client=client)
    assert result == "All good."
    assert client.calls[0][0].endswith("/summarize-journey")


async def test_fetch_summary_returns_none_when_ai_service_down():
    client = _FakeClient(boom=True)  # connection error
    assert await fetch_summary(_completion(), client=client) is None


async def test_fetch_summary_returns_none_on_http_error():
    client = _FakeClient(_FakeResp({}, status=500))
    assert await fetch_summary(_completion(), client=client) is None


async def test_fetch_summary_returns_none_on_empty_summary():
    client = _FakeClient(_FakeResp({"journey_id": "J1", "summary": "", "source": "fallback"}))
    assert await fetch_summary(_completion(), client=client) is None
