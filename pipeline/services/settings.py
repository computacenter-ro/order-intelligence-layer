"""cc-settings-service emitter block (CLAUDE.md [1]) — margin threshold settings.

Settings is the FIRST stop on **Inbound's pre-creation enrichment leg**
(documented: Inbound calls Settings between the order engine's two turns).
Its logs are therefore phase 1 — eventId only, no order ids, because the order
does not exist yet.

The reference dataset shows a single Hibernate SQL log for the settings lookup
(logger ``org.hibernate.SQL``), SQL-Server bracket-quoted style.

Two failure variants, both novel/embedding-path clustering test cases (see
``shared/scenarios.py``), both expressed entirely from the *caller's* side —
an Inbound-identity SettingsClient ERROR (the satellite itself emits nothing,
same pattern as SPT's ``_spt_down`` in ``spt.py``). Both are PRE-CREATION
failures now: the journey dies eventId-only, which is CLAUDE.md invariant #3
at work, not a data gap. The ``SettingsClient`` logger stem is kept so
backend/incidents.py's failing-service derivation is unchanged in shape; the
app_name behind it is ``cc-inbound-service`` for all three scenarios
(15/16/17), preserving their identical-failing-service clustering semantics.

* ``fail_at=settings`` (scenarios 15/16): the Settings web service is
  unreachable (connection-reset). Deliberately absent from
  backend/journeys.py's ``_FAILURE_RULES``, so the journey resolves to
  UNRECOGNIZED_FAILURE, not a recognized subtype — the point is exercising
  the novel/embedding clustering path on a failure type the backend has
  never named.
* ``fail_at=settings_rejected`` (scenario 17): a DIFFERENT cause on the SAME
  satellite — a 503 from the settings gateway rejecting the request, not a
  connectivity problem. Also unrecognized, and still INFRA-shaped (the ``5xx``
  code), so it still takes the cosine-search path — but its salient wording
  ("rejected"/"failed"/"503") shares no salient tokens with settings_down's
  ("unavailable"/"8000ms"), so backend/incidents.py's divergence guard must
  veto a false merge even though both alerts describe the same satellite and
  score a high cosine similarity. This is what proves the guard, not just the
  failing_service pre-filter, is doing real work.
"""
from __future__ import annotations

from pipeline.services.blocklib import emit_line, phase1_ids
from pipeline.services.inbound import CLIENT_LOGGER, ENRICH_THREAD
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("settings")
_LOG = "org.hibernate.SQL"

_CALLER_PROF = profile("inbound")
_CALLER_CLIENT = CLIENT_LOGGER["settings"]

_SETTINGS_SQL = (
    "select ssv1_0.[organisation_identifier], ssv1_0.[settings_identifier], "
    "ssv1_0.[settings_value] from [SF_SETTING_VALUE] ssv1_0 where "
    "ssv1_0.[organisation_identifier] in (?, ?, ?, ?) and "
    "ssv1_0.[settings_identifier] in (?, ?)"
)


@register("settings", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx

    if ctx.fail_at == "settings":
        return await _settings_down(baton, emit)
    if ctx.fail_at == "settings_rejected":
        return await _settings_rejected(baton, emit)

    await emit_line(
        emit, _PROF, logger=_LOG, level="DEBUG",
        message=_SETTINGS_SQL,
        ids=phase1_ids(ctx),
    )
    return True


async def _settings_down(baton: Baton, emit: EmitFn) -> bool:
    """Settings service unreachable.

    The GET request line was already emitted by inbound's
    ``enrich_settings_call`` block before the baton reached here (same as the
    happy path) — this block only needs to emit the connection-reset ERROR the
    backend has never been taught to recognize, in place of the usual
    ``enrich_settings_resp`` success line (which the truncated chain never
    reaches).
    """
    ctx = baton.ctx
    await emit_line(
        emit, _CALLER_PROF, logger=_CALLER_CLIENT, level="ERROR", thread=ENRICH_THREAD,
        message=(
            "[SettingsClient#getAccountSettingByOrganizationHierarchy] <--- ERROR: "
            "settings service unavailable — connection reset while fetching "
            "account settings (8000ms)"
        ), ids=phase1_ids(ctx),
    )
    return False  # fatal — chain stops here; journey never resolves to a known FAILED subtype


async def _settings_rejected(baton: Baton, emit: EmitFn) -> bool:
    """Settings gateway rejected the request (HTTP 503) — a different cause
    than ``_settings_down``, not a connectivity problem. See this module's
    docstring for why the wording is deliberately chosen to share no salient
    tokens with ``_settings_down``'s message.
    """
    ctx = baton.ctx
    await emit_line(
        emit, _CALLER_PROF, logger=_CALLER_CLIENT, level="ERROR", thread=ENRICH_THREAD,
        message=(
            "[SettingsClient#getAccountSettingByOrganizationHierarchy] <--- ERROR: "
            "settings request rejected — organisation hierarchy lookup failed "
            "with HTTP 503 from settings gateway"
        ), ids=phase1_ids(ctx),
    )
    return False  # fatal — chain stops here; journey never resolves to a known FAILED subtype
