"""[3] AI Service — the poller (async). See CLAUDE.md "[3] AI Service".

Every ``POLL_INTERVAL`` seconds, query the collector for the sliding window
``[now - WINDOW_START_OFFSET, now - WINDOW_END_OFFSET]`` (the 5s tail is the
ingestion-lag guard; consecutive windows overlap on purpose — dedup makes that
safe), then fan each log out:

    for log in window (sorted asc):
        if SETNX dedup says "new":
            publish_raw(log)                         # ALWAYS → raw.events (all levels)
            if level in (WARN, ERROR) and not suppressed(message):
                alert = pipeline.process(log)        # Explainer → Router (slice 2)
                publish_alert(alert)                 # → processed.alerts (slice 3)

The suppression list (benign WARNs) keeps noise out of the alert stream but
those logs STILL reach ``raw.events`` — they are journey material.

Everything external is injected (redis, publisher, pipeline deps, http client)
so the routing logic is unit-tested with fakes — no collector, broker, Redis or
LLM required. ``main.py`` constructs the real dependencies.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from ai_service import settings
from ai_service.graph import PipelineDeps, process
from ai_service.publisher import Publisher
from shared.models import LogLine


# --- pure window helpers (kept module-level; no I/O) --------------------------
def _format_ts(dt: datetime) -> str:
    """Match LogLine's serializer exactly — the collector compares timestamps as
    plain strings, so the format must line up byte-for-byte."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def poll_window(
    now: datetime | None = None,
    *,
    start_offset: int = settings.WINDOW_START_OFFSET,
    end_offset: int = settings.WINDOW_END_OFFSET,
) -> tuple[str, str]:
    """(from_iso, to_iso) for the sliding window ending at ``now`` (default: real now)."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=start_offset)
    end = now - timedelta(seconds=end_offset)
    return _format_ts(start), _format_ts(end)


def needs_alert(log: LogLine) -> bool:
    """A log becomes an alert only if it is WARN/ERROR and not suppressed.

    Suppressed benign WARNs (``settings.is_suppressed``) are filtered here —
    they still reach ``raw.events`` (that happens before this check), they just
    never enter the LangGraph pipeline.
    """
    return log.level in ("WARN", "ERROR") and not settings.is_suppressed(log.message)


class Poller:
    """Async sliding-window poller with dedup + raw/alert fan-out.

    Parameters are injected for testability; ``main.py`` wires the real ones.
    """

    def __init__(
        self,
        *,
        redis,
        publisher: Publisher,
        pipeline_deps: PipelineDeps,
        http: httpx.AsyncClient | None = None,
        es_url: str = settings.ES_URL,
        dedup_ttl: int = settings.DEDUP_TTL_SECONDS,
    ) -> None:
        self._redis = redis
        self._publisher = publisher
        self._deps = pipeline_deps
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._es_url = es_url.rstrip("/")
        self._dedup_ttl = dedup_ttl

    # --- collector fetch ------------------------------------------------------
    async def fetch_logs(self, from_iso: str, to_iso: str) -> list[dict]:
        """GET the collector for logs in [from_iso, to_iso), sorted asc (its contract)."""
        resp = await self._http.get(
            f"{self._es_url}/logs", params={"from": from_iso, "to": to_iso}
        )
        resp.raise_for_status()
        return resp.json()

    async def is_new(self, log_id: str) -> bool:
        """True the first time ``log_id`` is seen (SETNX); False on any repeat."""
        return bool(
            await self._redis.set(f"dedup:{log_id}", 1, nx=True, ex=self._dedup_ttl)
        )

    # --- one poll cycle -------------------------------------------------------
    async def poll_once(self, now: datetime | None = None) -> int:
        """Fetch the current window, route each new log. Returns #logs processed.

        Robust to a malformed line: a log that fails LogLine validation is
        skipped (logged) rather than killing the whole poll.
        """
        from_iso, to_iso = poll_window(now)
        raw_logs = await self.fetch_logs(from_iso, to_iso)
        processed = 0
        for raw in raw_logs:
            log_id = raw.get("log_id")
            if not log_id or not await self.is_new(log_id):
                continue  # missing id or already seen → skip (dedup)
            try:
                log = LogLine.model_validate(raw)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[poller] skipping malformed log {log_id}: {exc}", flush=True)
                continue
            await self._route(log)
            processed += 1
        return processed

    async def _route(self, log: LogLine) -> None:
        """Publish to raw.events (always), then to processed.alerts (if alertable)."""
        await self._publisher.publish_raw(log)
        if needs_alert(log):
            alert = await process(log, self._deps)
            await self._publisher.publish_alert(alert)

    # --- run loop -------------------------------------------------------------
    async def run(self, *, interval: int = settings.POLL_INTERVAL) -> None:
        """Poll forever every ``interval`` seconds."""
        while True:
            try:
                n = await self.poll_once()
                if n:
                    print(f"[poller] processed {n} new log(s)", flush=True)
            except Exception as exc:  # keep the loop alive across transient errors
                print(f"[poller] poll cycle error (continuing): {exc}", flush=True)
            await asyncio.sleep(interval)

    async def aclose(self) -> None:
        await self._http.aclose()
