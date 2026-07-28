"""Tests for shared/models.py wire contracts."""
from __future__ import annotations

from datetime import datetime, timezone

from shared.models import LogLine, ProcessedAlert


def _log() -> LogLine:
    return LogLine(
        log_id="log-1",
        timestamp=datetime(2026, 7, 26, 8, 0, 0, tzinfo=timezone.utc),
        app_name="cc-order-engine",
        level="ERROR",
        logger="c.c.orderengine.client.SptClient",
        host="CCECMEWEBT001",
        process_id="1234",
        thread="pool-1-thread-1",
        message=(
            "[SptClient#getSptPriceListCode] <--- ERROR "
            "java.net.SocketTimeoutException: connect timed out (10014ms)"
        ),
    )


def test_processed_alert_embedding_defaults_to_none():
    alert = ProcessedAlert(
        alert_id="a1",
        emitted_at=datetime(2026, 7, 26, 8, 0, 1, tzinfo=timezone.utc),
        log=_log(),
        explanation=None,
        department=None,
        source="fallback",
    )
    assert alert.embedding is None


def test_processed_alert_embedding_round_trips_through_json():
    alert = ProcessedAlert(
        alert_id="a1",
        emitted_at=datetime(2026, 7, 26, 8, 0, 1, tzinfo=timezone.utc),
        log=_log(),
        explanation="SPT timed out",
        department="devops",
        source="ai",
        embedding=[0.1, 0.2, 0.3],
    )
    restored = ProcessedAlert.model_validate_json(alert.model_dump_json())
    assert restored.embedding == [0.1, 0.2, 0.3]
