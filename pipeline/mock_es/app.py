"""[2] Mock Elasticsearch — Log Collector (FastAPI, :9200).

Intentionally dumb: in-memory storage, no journey/correlation logic ever.
See CLAUDE.md section "[2] Mock Elasticsearch".
"""
import os
from collections import deque
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query

app = FastAPI(title="Mock Elasticsearch — Log Collector")

# In-memory store: log dicts kept exactly as ingested, capped at MAX_LOGS.
#
# The cap exists because this store is unbounded by nature and the deployed
# simulation runs CONTINUOUSLY: at ~2 flows/30s the injector produces on the
# order of 170k log lines/day, so a week-long demo run would hold ~1.2M dicts
# resident and eventually OOM the container. A ring buffer bounds it instead.
#
# Dropping the oldest logs is safe because nothing at RUNTIME reads old ones:
# the AI-service poller only ever asks for a ~20s window (watermark → now-5s),
# and ``GET /logs?id=`` is a debug/ops tool (CLAUDE.md [2]). The default holds
# many hours of logs at demo rates — far more than the poller's window needs,
# even after a long stall (MAX_WINDOW_SPAN caps catch-up at 120s).
#
# Eviction is by INSERTION order, not by timestamp, and that distinction is
# deliberate: concurrent flows interleave, so the newest-arriving log is not
# necessarily the one with the latest timestamp. Evicting by timestamp could
# discard a log that just arrived and still sits inside the poller's window.
MAX_LOGS = int(os.getenv("MOCK_ES_MAX_LOGS", "200000"))

# deque(maxlen=...) discards from the opposite end on append — O(1), no manual
# trimming, and it can never exceed the cap even mid-request.
_STORE: deque[dict[str, Any]] = deque(maxlen=MAX_LOGS)


@app.post("/logs")
async def ingest(payload: Any = Body(...)) -> dict[str, int]:
    """Accept a single log object OR an array of them.

    Every log must carry ``log_id`` and ``timestamp`` (else 422).
    Returns ``{"ingested": N}``.
    """
    logs = payload if isinstance(payload, list) else [payload]
    for log in logs:
        if not isinstance(log, dict) or not log.get("log_id") or not log.get("timestamp"):
            raise HTTPException(
                status_code=422,
                detail="each log must include non-empty 'log_id' and 'timestamp'",
            )
    # Validate the WHOLE batch before storing any of it, so a 422 stores nothing.
    _STORE.extend(logs)  # oldest logs fall off the left once MAX_LOGS is reached
    return {"ingested": len(logs)}


# Cap on an unfiltered ``GET /logs``. Without one, a no-params call serializes the
# WHOLE store to JSON — ~90 MB at the 200k default — spiking memory on an endpoint
# that no runtime component uses (the poller always passes from/to). A filtered
# query is NOT capped: the poller's correctness depends on receiving its entire
# window, and truncating that would silently drop logs.
DEFAULT_QUERY_LIMIT = int(os.getenv("MOCK_ES_QUERY_LIMIT", "5000"))


@app.get("/logs")
async def query(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
) -> list[dict[str, Any]]:
    """Query stored logs, always sorted ascending by ``timestamp``.

    * ``?id=<X>``          → logs where eventId==X OR orderId==X OR cartHeaderId==X.
    * ``?from=&to=``       → half-open time range ``from <= timestamp < to``
      (lexicographic string comparison — ISO-8601 UTC sorts correctly as text,
      so we never parse dates).
    * no params           → the most recent ``DEFAULT_QUERY_LIMIT`` logs.

    ``limit`` keeps the NEWEST matches (the tail after sorting), because the only
    reason to read without a window is to look at recent activity.
    """
    if id is not None:
        result = [
            log
            for log in _STORE
            if id in (log.get("eventId"), log.get("orderId"), log.get("cartHeaderId"))
        ]
    elif from_ is not None or to is not None:
        result = [
            log
            for log in _STORE
            if (from_ is None or log.get("timestamp", "") >= from_)
            and (to is None or log.get("timestamp", "") < to)
        ]
    else:
        result = list(_STORE)
        # Only the unfiltered read is capped by default; see DEFAULT_QUERY_LIMIT.
        if limit is None:
            limit = DEFAULT_QUERY_LIMIT

    result = sorted(result, key=lambda log: log.get("timestamp", ""))
    return result[-limit:] if limit is not None else result


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + store occupancy + retention floor.

    ``capacity`` is reported alongside ``stored`` so a long demo run can be
    checked for whether the ring buffer has started evicting (stored == capacity)
    without reading the process's memory.

    ``oldest_timestamp`` is the retention floor: the earliest timestamp still
    held (``None`` when empty). Consumers use it to tell "this window is genuinely
    empty" from "this window is BEFORE anything I still have" — the second case
    means data was lost to eviction or to a restart (this store is in-memory, so a
    restart empties it while consumers' own cursors survive elsewhere). Without
    this, a consumer silently reads nothing and moves on.

    It is the MINIMUM timestamp, not ``_STORE[0]``: eviction is by insertion order
    while this is a statement about time, and interleaved flows mean the
    left-most entry is not necessarily the earliest.
    """
    oldest = min((log.get("timestamp", "") for log in _STORE), default=None)
    return {
        "status": "ok",
        "stored": len(_STORE),
        "capacity": MAX_LOGS,
        "oldest_timestamp": oldest or None,
    }
