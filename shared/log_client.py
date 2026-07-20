"""HTTP client for POSTing log lines to the Mock Elasticsearch collector.

Every mock service emits its logs through this module (CLAUDE.md [1]:
"POSTs them to the Log Collector ... all services use this"). It is the thin
seam between the services and the collector's ``POST /logs`` endpoint
(``pipeline/mock_es/app.py``, CLAUDE.md [2]).

Design notes
------------
* Async (``httpx.AsyncClient``) — the services are async baton consumers and
  the collector/poller are async elsewhere, so the whole services→collector
  path stays on one event loop.
* Log lines are serialized **through the ``LogLine`` model**, never hand-built
  dicts. That is what guarantees the canonical ``YYYY-MM-DDTHH:MM:SS.mmmZ``
  timestamp and the exact field set the collector (and later the poller) expect.
* Both single-line and batch emission are supported; ``POST /logs`` accepts a
  single object or an array.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from shared.models import LogLine

DEFAULT_ES_URL = "http://localhost:9200"


def _es_url() -> str:
    """Collector base URL from the ``ES_URL`` env var (CLAUDE.md default)."""
    return os.environ.get("ES_URL", DEFAULT_ES_URL).rstrip("/")


def _serialize(logs: LogLine | list[LogLine]) -> Any:
    """Render one or many ``LogLine``s to JSON-ready payload for ``POST /logs``.

    A single line is sent as a lone object; multiple as an array — mirroring
    what the collector accepts.
    """
    if isinstance(logs, LogLine):
        return logs.model_dump(mode="json")
    return [line.model_dump(mode="json") for line in logs]


class LogClient:
    """Reusable async client for emitting logs to the collector.

    Holds one ``httpx.AsyncClient`` for connection reuse across a service's
    many log lines. Use as an async context manager, or call :meth:`aclose`
    when done.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or _es_url()).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, transport=transport
        )

    async def __aenter__(self) -> "LogClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def emit(self, logs: LogLine | list[LogLine]) -> int:
        """POST one log line or a batch. Returns the collector's ``ingested`` count.

        Raises ``httpx.HTTPStatusError`` on a non-2xx response (e.g. the 422 the
        collector returns for a log missing ``log_id``/``timestamp`` — which
        should never happen for a real ``LogLine``, but surfaces bugs loudly
        rather than silently dropping logs).
        """
        response = await self._client.post("/logs", json=_serialize(logs))
        response.raise_for_status()
        return int(response.json()["ingested"])


async def emit_once(logs: LogLine | list[LogLine], *, base_url: str | None = None) -> int:
    """One-shot emit for callers that don't hold a :class:`LogClient`.

    Opens and closes a client per call — convenient for scripts/tests, but
    services running many lines should instantiate a :class:`LogClient` once
    and reuse it.
    """
    async with LogClient(base_url=base_url) as client:
        return await client.emit(logs)
