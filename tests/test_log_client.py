"""Tests for shared/log_client.py.

The client is exercised against an ``httpx.MockTransport`` that stands in for
the collector's ``POST /logs`` endpoint — no running server needed. One test
mirrors the collector's real 422 behavior so we prove the client surfaces it.
"""

import json
from datetime import datetime, timezone

import httpx
import pytest

from shared.log_client import DEFAULT_ES_URL, LogClient, _es_url, _serialize, emit_once
from shared.models import LogLine


def _line(i: int, **kw) -> LogLine:
    return LogLine(
        log_id=f"log-{i}",
        timestamp=datetime(2026, 7, 14, 8, 0, i, 0, tzinfo=timezone.utc),
        app_name="cc-inbound-service",
        level="INFO",
        logger="c.c.inbound.listener.OrderListener",
        host="CCECMETLT001",
        process_id="7412",
        thread="rabbit-listener-1",
        message=f"line {i}",
        **kw,
    )


def _collector(seen: list) -> httpx.MockTransport:
    """A fake ``POST /logs`` matching the real collector's contract."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/logs"
        payload = json.loads(request.content)
        logs = payload if isinstance(payload, list) else [payload]
        for log in logs:
            if not log.get("log_id") or not log.get("timestamp"):
                return httpx.Response(422, json={"detail": "missing log_id/timestamp"})
        seen.extend(logs)
        return httpx.Response(200, json={"ingested": len(logs)})

    return httpx.MockTransport(handler)


# --- serialization -----------------------------------------------------------

def test_serialize_single_is_object():
    payload = _serialize(_line(1))
    assert isinstance(payload, dict)
    # canonical LogLine timestamp: fixed-width, ms precision, Z suffix
    assert payload["timestamp"] == "2026-07-14T08:00:01.000Z"


def test_serialize_batch_is_array():
    payload = _serialize([_line(1), _line(2)])
    assert isinstance(payload, list) and len(payload) == 2


# --- config ------------------------------------------------------------------

def test_es_url_default(monkeypatch):
    monkeypatch.delenv("ES_URL", raising=False)
    assert _es_url() == DEFAULT_ES_URL


def test_es_url_from_env(monkeypatch):
    monkeypatch.setenv("ES_URL", "http://collector:9200/")
    assert _es_url() == "http://collector:9200"  # trailing slash stripped


# --- emit --------------------------------------------------------------------

async def test_emit_single():
    seen: list = []
    async with LogClient(base_url="http://x", transport=_collector(seen)) as c:
        assert await c.emit(_line(1, eventId="evt-A")) == 1
    assert [l["log_id"] for l in seen] == ["log-1"]


async def test_emit_batch():
    seen: list = []
    async with LogClient(base_url="http://x", transport=_collector(seen)) as c:
        assert await c.emit([_line(1), _line(2), _line(3)]) == 3
    assert len(seen) == 3


async def test_emit_raises_on_422():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "bad log"})

    async with LogClient(base_url="http://x", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await c.emit(_line(1))


async def test_one_shot_emit_helper(monkeypatch):
    seen: list = []

    # patch the class so the module-level emit_once() picks up our transport
    real_init = LogClient.__init__

    def patched(self, base_url=None, timeout=5.0, transport=None):
        real_init(self, base_url="http://x", timeout=timeout, transport=_collector(seen))

    monkeypatch.setattr(LogClient, "__init__", patched)
    assert await emit_once(_line(9)) == 1
    assert len(seen) == 1
