"""cc-settings-service emitter block (CLAUDE.md [1]) — margin threshold settings.

The reference dataset shows a single Hibernate SQL log for the settings lookup
(logger ``org.hibernate.SQL``), SQL-Server bracket-quoted style.
"""
from __future__ import annotations

from services.blocklib import emit_line, phase2_ids
from services.profiles import profile
from services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("settings")
_LOG = "org.hibernate.SQL"

_SETTINGS_SQL = (
    "select ssv1_0.[organisation_identifier], ssv1_0.[settings_identifier], "
    "ssv1_0.[settings_value] from [SF_SETTING_VALUE] ssv1_0 where "
    "ssv1_0.[organisation_identifier] in (?, ?, ?, ?) and "
    "ssv1_0.[settings_identifier] in (?, ?)"
)


@register("settings", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    await emit_line(
        emit, _PROF, logger=_LOG, level="DEBUG",
        message=_SETTINGS_SQL,
        ids=phase2_ids(baton.ctx),
    )
    return True
