"""cc-settings-service emitter block (CLAUDE.md [1]) — margin threshold settings.

The reference dataset shows a single Hibernate SQL log for the settings lookup
(logger ``org.hibernate.SQL``), SQL-Server bracket-quoted style.

Failure variant (``fail_at=settings``, scenario 15 — the NOVEL/embedding-path
clustering test case, see ``shared/scenarios.py``): the Settings web service is
unreachable. As with SPT (see ``spt.py``'s ``_spt_down``), the failure is
expressed entirely from the *order engine* side (a SettingsClient GET, then a
connection-reset ERROR) — the satellite itself emits nothing. This exact
message is deliberately absent from backend/journeys.py's ``_FAILURE_RULES``,
so the journey never resolves to a recognized FAILED subtype and instead
TIMES OUT — the point of scenario 15 is exercising incident clustering's
embedding/novel path on a failure type the backend has never seen before.
"""
from __future__ import annotations

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("settings")
_LOG = "org.hibernate.SQL"

_OE_PROF = profile("order_engine")
_OE_CLIENT = "c.c.orderengine.client.SettingsClient"

_SETTINGS_SQL = (
    "select ssv1_0.[organisation_identifier], ssv1_0.[settings_identifier], "
    "ssv1_0.[settings_value] from [SF_SETTING_VALUE] ssv1_0 where "
    "ssv1_0.[organisation_identifier] in (?, ?, ?, ?) and "
    "ssv1_0.[settings_identifier] in (?, ?)"
)


def _oe_thread(baton: Baton) -> str:
    return ORDER_ENGINE_WORKER_THREADS[abs(hash(baton.flow_id)) % len(ORDER_ENGINE_WORKER_THREADS)]


@register("settings", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx

    if ctx.fail_at == "settings":
        return await _settings_down(baton, emit)

    await emit_line(
        emit, _PROF, logger=_LOG, level="DEBUG",
        message=_SETTINGS_SQL,
        ids=phase2_ids(ctx),
    )
    return True


async def _settings_down(baton: Baton, emit: EmitFn) -> bool:
    """Settings service unreachable.

    The GET request line was already emitted by order_engine's
    ``enrich_settings_call`` block before the baton reached here (same as the
    happy path) — this block only needs to emit the connection-reset ERROR the
    backend has never been taught to recognize, in place of the usual
    ``enrich_settings_resp`` success line (which the truncated chain never
    reaches).
    """
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    thread = _oe_thread(baton)
    await emit_line(
        emit, _OE_PROF, logger=_OE_CLIENT, level="ERROR", thread=thread,
        message=(
            "[SettingsClient#getAccountSettingByOrganizationHierarchy] <--- ERROR: "
            "settings service unavailable — connection reset while fetching "
            "account settings (8000ms)"
        ), ids=ids,
    )
    return False  # fatal — chain stops here; journey never resolves to a known FAILED subtype
