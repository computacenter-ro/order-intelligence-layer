"""cc-avalara-service emitter block (CLAUDE.md [1]) — US ship-to verification.

Avalara is a first-class enrichment satellite, and the LAST enrichment step
before dispatch. It runs for **US orders only** (``ctx.country == "US"``), which
is why ``shared/scenarios.py`` appends its call/serve/resp trio conditionally
instead of listing it in ``ENRICH_SATELLITES``.

These lines used to be emitted by ``cc-validator-service`` (its ``AvalaraClient``
+ ``ValidateShipToWithAvalara`` strategy). They were removed from
``services/validator.py`` when Avalara became a standalone service, so the
verification is emitted exactly once — by this module.

**Success path only, by design.** No scenario fails at Avalara; there is no
``fail_at="avalara"`` in ``shared/scenarios.py`` and none should be invented
here. A non-US flow simply never routes a baton to this service.
"""
from __future__ import annotations

from pipeline.services.blocklib import emit_line, phase2_ids
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton

_PROF = profile("avalara")
_LOG_RESOLVE = "c.c.avalara.service.AddressResolutionService"
_LOG_CLIENT = "c.c.avalara.client.AvaTaxClient"

# The US ship-to address used for the verification line. Kept identical to the
# address the validator used to report, so the emitted text is unchanged now
# that ownership moved here.
_US_SHIPTO = "1401 Elm St, Dallas, TX 75202"


@register("avalara", "serve")
async def serve(baton: Baton, emit: EmitFn) -> bool:
    ctx = baton.ctx
    ids = phase2_ids(ctx)

    await emit_line(
        emit, _PROF, logger=_LOG_CLIENT, level="DEBUG",
        message=(
            "[AvaTaxClient#resolveAddress] ---> POST "
            "https://rest.avatax.com/api/v2/addresses/resolve HTTP/1.1"
        ),
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_RESOLVE, level="INFO",
        message=f"Ship-to address verified: {_US_SHIPTO}, resolution quality: Premises",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_RESOLVE, level="DEBUG",
        message=(
            f"Resolved jurisdiction for account {ctx.accountNumber}: "
            f"TX / Dallas County, taxability confirmed"
        ),
        ids=ids,
    )
    return True
