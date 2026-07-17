"""[3] AI Service — poller. See CLAUDE.md section "[3] AI Service".

Polls the collector on a sliding window [now - WINDOW_START_OFFSET, now - WINDOW_END_OFFSET]
every POLL_INTERVAL seconds.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import redis

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
WINDOW_START_OFFSET = int(os.getenv("WINDOW_START_OFFSET", "25"))
WINDOW_END_OFFSET = int(os.getenv("WINDOW_END_OFFSET", "5"))
ES_URL = os.getenv("ES_URL", "http://localhost:9200")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis = redis.Redis.from_url(REDIS_URL)


def _format_ts(dt: datetime) -> str:
    """Match LogLine's own serializer exactly — the collector compares timestamps
    as plain strings, so the format must line up byte-for-byte."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def poll_window(now: datetime | None = None) -> tuple[str, str]:
    """Return (from_iso, to_iso) for the sliding window ending at `now` (defaults to real now)."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=WINDOW_START_OFFSET)
    end = now - timedelta(seconds=WINDOW_END_OFFSET)
    return _format_ts(start), _format_ts(end)


def fetch_logs(from_iso: str, to_iso: str) -> list[dict]:
    """GET the collector for logs in [from_iso, to_iso), sorted ascending (its own contract)."""
    response = httpx.get(f"{ES_URL}/logs", params={"from": from_iso, "to": to_iso})
    response.raise_for_status()
    return response.json()


def is_new(log_id: str) -> bool:
    """True the first time log_id is seen; False on any repeat within the next hour."""
    return bool(_redis.set(f"dedup:{log_id}", 1, nx=True, ex=3600))


def needs_alert(log: dict) -> bool:
    """WARN/ERROR only for now — suppression criteria pending team discussion."""
    return log["level"] in ("WARN", "ERROR")


def poll_once(now: datetime | None = None) -> list[dict]:
    """Fetch the current window, keep only logs not seen before (dedup), return them."""
    from_iso, to_iso = poll_window(now)
    logs = fetch_logs(from_iso, to_iso)
    return [log for log in logs if is_new(log["log_id"])]


def run() -> None:
    """Continuously poll every POLL_INTERVAL seconds.

    Every deduped log is raw.events material; WARN/ERROR ones also need an alert.
    """
    while True:
        for log in poll_once():
            print(f"[raw] {log}")
            if needs_alert(log):
                print(f"[alert] {log}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
