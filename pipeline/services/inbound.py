"""cc-inbound-service emitter blocks (CLAUDE.md [1]).

Inbound is touched THREE times under the five-hop ping-pong: it receives the
order, it runs the pre-creation enrichment leg (Settings → JAM → SOLR), and it
closes the loop when ``order_created`` arrives. Blocks:

  * ``receive`` — phase 1: receive the inbound event, write the audit row,
    transform + SKU-map each line, publish to ``order.init``. Failure variant
    (``fail_at=transform``, scenario 4): an unknown product has no SKU mapping
    → 3 delivery attempts → route to the ``order.init_error`` DLQ, chain stops.
  * ``enrich_settings_call`` / ``enrich_settings_resp``,
    ``enrich_jam_call`` / ``enrich_jam_resp``,
    ``enrich_solr_call`` / ``enrich_solr_resp`` — phase 1: Inbound's own
    enrichment leg, run between the order engine's two turns. The FIRST call
    block (derived from ``INBOUND_SATELLITES[0]``, never hardcoded) also emits
    the ``order_data_ready`` consumption ack. Settings and JAM here are
    documented as Inbound's calls; SOLR's position is inferred — see the
    provenance note in ``shared/scenarios.py``.
  * ``request_create`` — phase 1: publish ``order.approval``, asking the order
    engine's second turn to persist the order.
  * ``close`` — phase 2: consume ``order_created`` and log the completion ack.
    **Its message text is the journey SUCCESS terminal** — the backend's
    journey assembler matches on ``"order_created"`` + ``"processing
    complete"`` (backend/journeys.py). Treat the text as an API.

Id discipline: everything up to ``request_create`` is pure phase 1 (eventId
only) — the order does not exist during Inbound's whole enrichment leg, so
these blocks physically cannot log order-id fields. Only ``close`` is phase 2.
There is no creation-response "bridge" ack anymore: the ``order_data_ready``
ack is the realistic return-leg line (eventId only, links nothing), and the
eventId→order-id join is recovered downstream by mining the order-engine
creation logs' message text (see backend/stitching.py and CLAUDE.md "THE
CORRELATION MODEL"). The ``ctx.bridge_ids`` knob is fully inert.
"""
from __future__ import annotations

import random

from pipeline.services.blocklib import emit_line, phase1_ids, phase2_ids
from pipeline.services.profiles import profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton, BatonContext
from shared.scenarios import INBOUND_SATELLITES

_PROF = profile("inbound")

# Loggers (receive/transform/publisher verbatim from the reference dataset;
# the client/orchestration/created-listener loggers are new with the
# five-hop realignment — Inbound never called satellites before).
_LOG_ORDER_LISTENER = "c.c.inbound.listener.OrderListener"
_LOG_AUDIT = "c.c.inbound.service.OrderAuditService"
_LOG_TRANSFORM = "c.c.inbound.transform.TransformService"
_LOG_PUBLISHER = "c.c.inbound.publisher.RabbitPublisher"
_LOG_DATA_READY_LISTENER = "c.c.inbound.listener.OrderDataReadyListener"
_LOG_ORCHESTRATION = "c.c.inbound.service.OrderOrchestrationService"
_LOG_JWT = "c.c.inbound.service.JwtTokenService"
_LOG_CREATED_LISTENER = "c.c.inbound.listener.OrderCreatedListener"

# Client loggers keep the "<Sat>Client" stems — backend/incidents.py's
# failing-service markers match on those substrings ("JamClient" etc.), and the
# satellites' failure variants emit through these same names.
CLIENT_LOGGER = {
    "settings": "c.c.inbound.client.SettingsClient",
    "jam": "c.c.inbound.client.JamClient",
    "solr": "c.c.inbound.client.SolrClient",
}

_RECEIVE_THREAD = "rabbit-listener-1"
# The order_data_ready and order_created listeners run on a 2nd listener
# thread; Inbound's enrichment leg runs on it too (it is driven by that
# consumption).
ENRICH_THREAD = "rabbit-listener-2"

_ORG_SALES = {"UK": "8100", "DE": "3100", "US": "7100"}

# The first satellite in Inbound's leg owns the one-off order_data_ready ack +
# orchestration line. Derived, never hardcoded, so reordering
# INBOUND_SATELLITES moves the ack with it.
_FIRST_SATELLITE = INBOUND_SATELLITES[0]


@register("inbound", "receive")
async def receive(baton: Baton, emit: EmitFn) -> bool:
    """Phase 1: receive → audit → transform + SKU-map → publish (or fail)."""
    ctx = baton.ctx
    ids = phase1_ids(ctx)
    n = len(ctx.lines)

    await emit_line(
        emit, _PROF, logger=_LOG_ORDER_LISTENER, level="INFO", thread=_RECEIVE_THREAD,
        message=f"Received inbound order event {ctx.eventId} for account {ctx.accountNumber}",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_AUDIT, level="DEBUG", thread=_RECEIVE_THREAD,
        message=f"Wrote order audit row for event {ctx.eventId}",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_TRANSFORM, level="INFO", thread=_RECEIVE_THREAD,
        message=f"Transforming inbound payload for event {ctx.eventId}, {n} line(s)",
        ids=ids,
    )

    if ctx.fail_at == "transform":
        return await _transform_failure(emit, ctx)

    # Happy transform: one DEBUG "Mapped product X to internal SKU Y" per line.
    for line in ctx.lines:
        await emit_line(
            emit, _PROF, logger=_LOG_TRANSFORM, level="DEBUG", thread=_RECEIVE_THREAD,
            message=f"Mapped product {line.productId} to internal SKU {line.sku}",
            ids=ids,
        )
    await emit_line(
        emit, _PROF, logger=_LOG_PUBLISHER, level="INFO", thread=_RECEIVE_THREAD,
        message=f"Published event {ctx.eventId} to queue order.init",
        ids=ids,
    )
    return True  # forward the baton to order_engine/init


async def _transform_failure(emit: EmitFn, ctx: BatonContext) -> bool:
    """Unknown-product transform failure: 3 attempts, then route to the DLQ.

    The first line with no SKU triggers the error; RabbitMQ redelivers up to
    3 times (attempts 2/3 and 3/3 are logged), then the message is dead-
    lettered to ``order.init_error``. The baton is NOT forwarded (return
    False) — a fatal failure.
    """
    ids = phase1_ids(ctx)
    # The offending product is the first line without a resolvable SKU.
    bad = next((line.productId for line in ctx.lines if line.sku is None), ctx.lines[0].productId)

    for attempt in (1, 2, 3):
        await emit_line(
            emit, _PROF, logger=_LOG_TRANSFORM, level="ERROR", thread=_RECEIVE_THREAD,
            message=f"No internal SKU mapping found for product {bad}",
            ids=ids,
        )
        if attempt < 3:
            await emit_line(
                emit, _PROF, logger=_LOG_ORDER_LISTENER, level="WARN", thread=_RECEIVE_THREAD,
                message=(
                    f"Requeueing event {ctx.eventId} for redelivery "
                    f"(attempt {attempt + 1}/3)"
                ),
                ids=ids,
            )
    await emit_line(
        emit, _PROF, logger=_LOG_ORDER_LISTENER, level="ERROR", thread=_RECEIVE_THREAD,
        message=(
            f"Max redelivery attempts reached for event {ctx.eventId}; "
            f"routing message to order.init_error"
        ),
        ids=ids,
    )
    return False  # fatal — never created; eventId-only journey


# --- Inbound's enrichment leg (phase 1) ---------------------------------------
def _endpoint(sat: str, ctx: BatonContext) -> str:
    """The Feign-style call line's HTTP request text for a satellite."""
    acct = ctx.accountNumber
    country = ctx.country
    if sat == "settings":
        org = _ORG_SALES.get(country, "8100")
        cc = f"CC_{country}"
        return (
            f"[SettingsClient#getAccountSettingByOrganizationHierarchy] ---> GET "
            f"http://settingsws-uat.computacenter.com/api/v1/settings/{country}/{cc}/"
            f"{org}/{acct}?settingsIdentifier=marginThresholdPercentage"
            f"&settingsIdentifier=marginThresholdValue HTTP/1.1"
        )
    if sat == "jam":
        return (
            f"[JamClient#getUserProfileWithPrivilegesBySamAccountName] ---> GET "
            f"http://jamws-uat.computacenter.com/api/user/oe/{ctx.user} HTTP/1.1"
        )
    if sat == "solr":
        product_ids = ",".join(line.productId for line in ctx.lines)
        return (
            f"[SolrClient#searchProductsByIds] ---> GET "
            f"http://solrws-uat.computacenter.com/solr/products_{country.lower()}/select"
            f"?q=productId:({product_ids})&rows={len(ctx.lines)} HTTP/1.1"
        )
    return f"[{sat}] ---> call"


def _make_enrich_call(sat: str):
    """Build the enrich_<sat>_call handler for one Inbound-leg satellite."""

    async def _call(baton: Baton, emit: EmitFn) -> bool:
        ctx = baton.ctx
        ids = phase1_ids(ctx)

        # The first satellite in the leg → emit the one-off order_data_ready
        # consumption ack + orchestration line before its call line. The ack is
        # the realistic return-leg line (eventId ONLY — it links nothing; the
        # id join lives in the order-engine creation logs' text).
        if sat == _FIRST_SATELLITE:
            await emit_line(
                emit, _PROF, logger=_LOG_DATA_READY_LISTENER, level="INFO",
                thread=ENRICH_THREAD,
                message=(
                    f"Received order_data_ready for event {ctx.eventId}: "
                    f"default order data assembled"
                ),
                ids=ids,
            )
            await emit_line(
                emit, _PROF, logger=_LOG_ORCHESTRATION, level="INFO",
                thread=ENRICH_THREAD,
                message=f"Starting pre-creation checks for event {ctx.eventId}",
                ids=ids,
            )

        await emit_line(
            emit, _PROF, logger=CLIENT_LOGGER[sat], level="DEBUG", thread=ENRICH_THREAD,
            message=_endpoint(sat, ctx), ids=ids,
        )
        return True

    _call.__name__ = f"enrich_{sat}_call"
    return _call


def _make_enrich_resp(sat: str):
    """Build the enrich_<sat>_resp handler for one Inbound-leg satellite."""

    async def _resp(baton: Baton, emit: EmitFn) -> bool:
        ctx = baton.ctx
        ids = phase1_ids(ctx)
        logger = CLIENT_LOGGER[sat]

        if sat == "settings":
            await emit_line(
                emit, _PROF, logger=logger, level="DEBUG", thread=ENRICH_THREAD,
                message=(
                    f"[SettingsClient#getAccountSettingByOrganizationHierarchy] "
                    f"<--- HTTP/1.1 200 ({random.randint(90, 130)}ms)"
                ), ids=ids,
            )

        elif sat == "jam":
            await emit_line(
                emit, _PROF, logger=logger, level="DEBUG", thread=ENRICH_THREAD,
                message=(
                    f"[JamClient#getUserProfileWithPrivilegesBySamAccountName] "
                    f"<--- HTTP/1.1 200 ({random.randint(300, 450)}ms)"
                ), ids=ids,
            )
            await emit_line(
                emit, _PROF, logger=_LOG_JWT, level="DEBUG", thread=ENRICH_THREAD,
                message=(
                    "Generated jwt: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
                    f"eyJzdWIiOiJ{ctx.user}IiwiZXhwIjoxNzUyNDg4ODAwfQ.mock-signature"
                ), ids=ids,
            )

        elif sat == "solr":
            await emit_line(
                emit, _PROF, logger=logger, level="DEBUG", thread=ENRICH_THREAD,
                message=(
                    f"[SolrClient#searchProductsByIds] "
                    f"<--- HTTP/1.1 200 ({random.randint(20, 70)}ms)"
                ), ids=ids,
            )
            await emit_line(
                emit, _PROF, logger=_LOG_ORCHESTRATION, level="DEBUG", thread=ENRICH_THREAD,
                message=(
                    f"Matched {len(ctx.lines)} catalogue line(s) for event {ctx.eventId}"
                ), ids=ids,
            )

        return True

    _resp.__name__ = f"enrich_{sat}_resp"
    return _resp


for _sat in INBOUND_SATELLITES:
    register("inbound", f"enrich_{_sat}_call")(_make_enrich_call(_sat))
    register("inbound", f"enrich_{_sat}_resp")(_make_enrich_resp(_sat))


# --- request_create (phase 1) -------------------------------------------------
@register("inbound", "request_create")
async def request_create(baton: Baton, emit: EmitFn) -> bool:
    """Publish order.approval — the pre-creation checks passed; ask the order
    engine's second turn to persist the order. Still phase 1: the order ids do
    not exist until the engine's create block mints them."""
    ctx = baton.ctx
    await emit_line(
        emit, _PROF, logger=_LOG_PUBLISHER, level="INFO", thread=ENRICH_THREAD,
        message=f"Published event {ctx.eventId} to queue order.approval",
        ids=phase1_ids(ctx),
    )
    return True


# --- close (phase 2) — THE SUCCESS TERMINAL -----------------------------------
@register("inbound", "close")
async def close(baton: Baton, emit: EmitFn) -> bool:
    """Consume order_created — the hop that closes the five-hop loop.

    **LOAD-BEARING TEXT**: backend/journeys.py detects journey SUCCESS by
    matching ``"order_created"`` AND ``"processing complete"`` on a journey's
    last line. Changing this message means changing the terminal-detection
    rules and their tests in the same commit (CLAUDE.md "Gotchas").
    Phase 2: carries both order ids, never eventId.
    """
    ctx = baton.ctx
    await emit_line(
        emit, _PROF, logger=_LOG_CREATED_LISTENER, level="INFO", thread=ENRICH_THREAD,
        message=f"Received order_created for order {ctx.orderId}: order processing complete",
        ids=phase2_ids(ctx),
    )
    return True
