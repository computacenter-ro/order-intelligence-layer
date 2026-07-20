"""[2] Mock Elasticsearch — Log Collector (FastAPI, :9200).

Intentionally dumb: in-memory storage, no journey/correlation logic ever.
See CLAUDE.md section "[2] Mock Elasticsearch".
"""
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query

app = FastAPI(title="Mock Elasticsearch — Log Collector")

# In-memory store: a plain list of log dicts, kept exactly as ingested.
_STORE: list[dict[str, Any]] = []


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
    _STORE.extend(logs)
    return {"ingested": len(logs)}


@app.get("/logs")
async def query(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Query stored logs, always sorted ascending by ``timestamp``.

    * ``?id=<X>``          → logs where eventId==X OR orderId==X OR cartHeaderId==X.
    * ``?from=&to=``       → half-open time range ``from <= timestamp < to``
      (lexicographic string comparison — ISO-8601 UTC sorts correctly as text,
      so we never parse dates).
    * no params           → all logs.
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

    return sorted(result, key=lambda log: log.get("timestamp", ""))


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "stored": len(_STORE)}
