"""[3] AI Service — the poller (async). See CLAUDE.md "[3] AI Service".

Every ``POLL_INTERVAL`` seconds, query the collector for a **watermark-anchored**
window ``[last_to, now - WINDOW_END_OFFSET]`` and fan each new log out:

    for log in window (sorted asc):
        if SETNX dedup says "new":
            publish_raw(log)                         # ALWAYS → raw.events (all levels)
    # then, OFF the fetch path, for the WARN/ERROR (non-suppressed) subset:
            alert = pipeline.process(log)            # Explainer → Router (two LLM calls)
            publish_alert(alert)                     # → processed.alerts

Two properties this module guarantees, both learned the hard way:

* **Contiguous windows (watermark).** The window's ``from`` is the previous
  window's ``to`` (persisted in Redis as ``ai:last_to``), NOT ``now - start``.
  A slow cycle therefore never skips wall-clock time. On a cold start (no
  watermark) it falls back to ``now - WINDOW_START_OFFSET`` so history is not
  replayed; after a long stall the look-back is capped at ``MAX_WINDOW_SPAN``.

* **LLM off the critical path.** ``raw.events`` is published for every deduped
  log *before* any LLM call runs — journey assembly must never wait on the
  explainer/router. Alert processing then runs concurrently (bounded by
  ``ALERT_CONCURRENCY``) so a batch of alerts can't serialize the poll loop and
  let the next window's logs age out of the collector.

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
    """(from_iso, to_iso) for the cold-start window ending at ``now``.

    This is the *no-watermark* window — ``[now - start_offset, now - end_offset]``.
    The steady-state window is watermark-anchored; see :func:`window_from_watermark`.
    """
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(seconds=start_offset)
    end = now - timedelta(seconds=end_offset)
    return _format_ts(start), _format_ts(end)


def _parse_ts(iso: str) -> datetime:
    """Parse a watermark timestamp (``...Z``) back to a tz-aware UTC datetime."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def window_from_watermark(
    last_to: str | None,
    now: datetime | None = None,
    *,
    start_offset: int = settings.WINDOW_START_OFFSET,
    end_offset: int = settings.WINDOW_END_OFFSET,
    max_span: int = settings.MAX_WINDOW_SPAN,
) -> tuple[str, str]:
    """(from_iso, to_iso) for the next window.

    ``to`` is always ``now - end_offset`` (the ingestion-lag guard). ``from`` is
    the previous window's ``to`` (the watermark) so windows are contiguous — no
    wall-clock time is ever skipped. With no watermark (cold start) ``from``
    falls back to ``now - start_offset``. A ``from`` older than ``max_span``
    before ``to`` (a long stall/outage) is clamped forward to ``to - max_span``.
    """
    now = now or datetime.now(timezone.utc)
    end = now - timedelta(seconds=end_offset)
    if last_to:
        start = _parse_ts(last_to)
    else:
        start = now - timedelta(seconds=start_offset)
    # Clamp an over-long look-back (long stall) so one catch-up read stays bounded.
    floor = end - timedelta(seconds=max_span)
    if start < floor:
        start = floor
    # Never invert (e.g. watermark somehow ahead of end): fall back to a normal
    # start-offset window rather than an empty/negative one.
    if start > end:
        start = now - timedelta(seconds=start_offset)
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
        watermark_key: str = settings.WATERMARK_KEY,
        alert_concurrency: int = settings.ALERT_CONCURRENCY,
    ) -> None:
        self._redis = redis
        self._publisher = publisher
        self._deps = pipeline_deps
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._es_url = es_url.rstrip("/")
        self._dedup_ttl = dedup_ttl
        self._watermark_key = watermark_key
        self._alert_sem = asyncio.Semaphore(max(1, alert_concurrency))

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

    # --- watermark ------------------------------------------------------------
    async def _read_watermark(self) -> str | None:
        """The ``to`` of the last fetched window, or None (cold start)."""
        value = await self._redis.get(self._watermark_key)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else str(value)

    async def _write_watermark(self, to_iso: str) -> None:
        await self._redis.set(self._watermark_key, to_iso)

    # --- one poll cycle -------------------------------------------------------
    async def poll_once(self, now: datetime | None = None) -> int:
        """Fetch the next (watermark-anchored) window, route each new log.

        Returns #logs processed. raw.events is published for every new log
        BEFORE any LLM call; alertable logs are then processed concurrently
        (bounded) so the fetch path is never serialized behind the LLM.
        Robust to a malformed line: it is skipped (logged), not fatal.
        """
        last_to = await self._read_watermark()
        from_iso, to_iso = window_from_watermark(last_to, now)
        raw_logs = await self.fetch_logs(from_iso, to_iso)

        alertable: list[LogLine] = []
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
            # raw.events FIRST — journey material never waits on the LLM.
            await self._publisher.publish_raw(log)
            processed += 1
            if needs_alert(log):
                alertable.append(log)

        # Advance the watermark now that this window is fetched + raw-published,
        # so the next cycle picks up exactly where this one ended (contiguous).
        await self._write_watermark(to_iso)

        # Alert LLM processing runs OFF the fetch/raw path, concurrently.
        if alertable:
            await asyncio.gather(*(self._process_alert(log) for log in alertable))
        return processed

    async def _process_alert(self, log: LogLine) -> None:
        """Explain+route one alertable log and publish it (bounded concurrency)."""
        async with self._alert_sem:
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
