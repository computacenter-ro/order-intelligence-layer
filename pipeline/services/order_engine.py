"""cc-order-engine emitter blocks (CLAUDE.md [1]) — the central orchestrator.

This is the busiest emitter. Its blocks:

  * ``create`` — phase 1: consume the creation request, persist the cart header,
    **mint orderId + cartHeaderId into ctx**, publish the creation response.
    This is the ONLY block that fills the order ids. Failure variant
    (``fail_at=create``, scenario 5): BM-DB timeout ×3 → failure response →
    the (inbound) ResponseListener failure line, and the chain stops.

  * ``enrich_<sat>_call`` / ``enrich_<sat>_resp`` — the Feign-style client
    ``--->`` / ``<---`` logs that bracket each satellite call, plus the
    order-engine-side processing filler that the reference dataset shows around
    each satellite (org hierarchy + object attrs before SPT; pricing after SPT;
    PVC/rebate around RSM; internal-contract/fee/cart filler after SETTINGS;
    JWT after JAM). The satellite's own server-side ``serve`` block runs in
    between (its own module).

  * ``dispatch`` — publish the validated order to order.outbound.queue.

Id discipline: ``create`` is phase 1 (eventId only) right up to the moment it
mints the ids; the response-publish line it emits is still eventId-only (the
order ids first *surface in a log* at the bridge). Every enrich/dispatch block
is phase 2 (both order ids, never eventId).
"""
from __future__ import annotations

import random

from pipeline.services.blocklib import emit_line, phase1_ids, phase2_ids
from pipeline.services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton, BatonContext

_PROF = profile("order_engine")

# Loggers (verbatim from the reference dataset).
_LOG_CREATE_LISTENER = "c.c.orderengine.listener.OrderCreateListener"
_LOG_CREATION = "c.c.orderengine.service.OrderCreationService"
_LOG_PUBLISHER = "c.c.orderengine.publisher.RabbitPublisher"
_LOG_ORDER = "c.c.orderengine.service.OrderService"
_LOG_CART_HEADER = "c.c.orderengine.service.CartHeaderService"
_LOG_ORG = "c.c.orderengine.service.OrganizationService"
_LOG_OBJ_ATTR = "c.c.orderengine.service.ObjectAttributeService"
_LOG_PRICING = "c.c.orderengine.service.PricingService"
_LOG_PRODUCT = "c.c.orderengine.service.ProductService"
_LOG_PRODUCT_PVC = "c.c.orderengine.service.ProductPvcService"
_LOG_REBATE_ITEM = "c.c.orderengine.service.RebateItemService"
_LOG_INTERNAL_CONTRACT = "c.c.orderengine.service.InternalContractService"
_LOG_FEE_CONFIG = "c.c.orderengine.service.FeeConfigService"
_LOG_CART_BLOCKING = "c.c.orderengine.service.CartBlockingAndGroupingService"
_LOG_TEXTS_OTHERS = "c.c.orderengine.service.TextsOthersService"
_LOG_FEE_ITEM = "c.c.orderengine.service.FeeItemService"
_LOG_CART_SOURCING = "c.c.orderengine.service.CartSourcingService"
_LOG_JWT = "c.c.orderengine.service.JwtTokenService"
_LOG_PROCESSING = "c.c.orderengine.service.OrderProcessingService"

# Client (Feign) loggers per satellite. Checker is deliberately absent: in the
# reference dataset the margin check is invoked in-process (the checker service
# emits its own lines) with no order-engine client `--->`/`<---` log.
_CLIENT_LOGGER = {
    "spt": "c.c.orderengine.client.SptClient",
    "rsm": "c.c.orderengine.client.RsmClient",
    "settings": "c.c.orderengine.client.SettingsClient",
    "jam": "c.c.orderengine.client.JamClient",
}

_CREATE_THREAD = "order-create-listener-1"

# Sales-org hierarchy per country (reference dataset: 9100 -> <country org> -> GLOBAL).
_COUNTRY_ORG = {"UK": "8100", "DE": "3100", "US": "7100"}
_ORG_SALES = {"UK": "8100", "DE": "3100", "US": "7100"}


def _worker_thread(baton: Baton) -> str:
    """A stable phase-2 worker thread for this flow.

    The reference dataset keeps all of a flow's phase-2 order-engine lines on a
    single pool thread; we pick one deterministically from the flow_id so a
    flow's lines share it (and different flows differ).
    """
    idx = abs(hash(baton.flow_id)) % len(ORDER_ENGINE_WORKER_THREADS)
    return ORDER_ENGINE_WORKER_THREADS[idx]


# --- create (phase 1) --------------------------------------------------------
@register("order_engine", "create")
async def create(baton: Baton, emit: EmitFn) -> bool:
    """Phase 1: create the order, mint ids into ctx, publish response (or fail)."""
    ctx = baton.ctx
    ids = phase1_ids(ctx)

    await emit_line(
        emit, _PROF, logger=_LOG_CREATE_LISTENER, level="INFO", thread=_CREATE_THREAD,
        message=f"Received order creation request for event {ctx.eventId}",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_CREATION, level="INFO", thread=_CREATE_THREAD,
        message=f"Creating order from event {ctx.eventId} for account {ctx.accountNumber}",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_CREATION, level="DEBUG", thread=_CREATE_THREAD,
        message="Persisting cart header to BM DB",
        ids=ids,
    )

    if ctx.fail_at == "create":
        return await _create_failure(emit, ctx)

    # Success: mint the ids INTO ctx — this is the only place they are born.
    # The injector already staged them as None; fill them now so phase-2 blocks
    # (and the bridge) can read them.
    _mint_ids(ctx)

    await emit_line(
        emit, _PROF, logger=_LOG_CREATION, level="INFO", thread=_CREATE_THREAD,
        message=f"Created cart header {ctx.cartHeaderId}",
        ids=ids,  # still phase-1: eventId only, even though ctx now has the ids
    )
    await emit_line(
        emit, _PROF, logger=_LOG_CREATION, level="INFO", thread=_CREATE_THREAD,
        message=f"Generated order number {ctx.orderId} for cart header {ctx.cartHeaderId}",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_PUBLISHER, level="INFO", thread=_CREATE_THREAD,
        message=(
            f"Published order creation response for event {ctx.eventId} "
            f"to queue order.response.queue"
        ),
        ids=ids,
    )
    return True  # forward to inbound/bridge


def _mint_ids(ctx: BatonContext) -> None:
    """Assign a realistic orderId + 19-digit cartHeaderId into ctx if absent.

    The injector leaves these None (they are born here). If they were pre-seeded
    (e.g. a deterministic test), keep them.
    """
    if ctx.orderId is None:
        ctx.orderId = f"ORD-{6000 + random.randint(1, 999)}"
    if ctx.cartHeaderId is None:
        ctx.cartHeaderId = f"18409273650182{random.randint(0, 99999):05d}"


async def _create_failure(emit: EmitFn, ctx: BatonContext) -> bool:
    """BM-DB timeout ×3 → failure response → inbound failure line. No ids ever.

    Scenario 5: creation itself fails, so the order ids are never minted — the
    journey lives and dies eventId-only. Because the chain truncates at this
    block, we also emit the (inbound) ResponseListener failure line here, since
    inbound will not run again.
    """
    ids = phase1_ids(ctx)
    for attempt in (1, 2, 3):
        await emit_line(
            emit, _PROF, logger=_LOG_CREATION, level="ERROR", thread=_CREATE_THREAD,
            message=(
                "Failed to persist cart header: java.sql.SQLTimeoutException: "
                "timeout after 30000ms acquiring connection to BM DB"
            ),
            ids=ids,
        )
        if attempt < 3:
            await emit_line(
                emit, _PROF, logger=_LOG_CREATION, level="WARN", thread=_CREATE_THREAD,
                message=(
                    f"Retrying order creation for event {ctx.eventId} "
                    f"(attempt {attempt + 1}/3)"
                ),
                ids=ids,
            )
    await emit_line(
        emit, _PROF, logger=_LOG_CREATION, level="ERROR", thread=_CREATE_THREAD,
        message=f"Order creation failed for event {ctx.eventId} after 3 attempt(s)",
        ids=ids,
    )
    await emit_line(
        emit, _PROF, logger=_LOG_PUBLISHER, level="INFO", thread=_CREATE_THREAD,
        message=(
            f"Published order creation failure for event {ctx.eventId} "
            f"to queue order.response.queue"
        ),
        ids=ids,
    )
    # The inbound response listener records the failure (emitted here because
    # the chain stops — inbound does not run again). Still eventId-only.
    inbound_prof = profile("inbound")
    await emit_line(
        emit, inbound_prof, logger="c.c.inbound.listener.ResponseListener",
        level="ERROR", thread="rabbit-listener-2",
        message=(
            f"Order creation failed for event {ctx.eventId}: "
            f"DB_TIMEOUT — no order was created"
        ),
        ids=ids,
    )
    return False  # fatal, pre-creation → eventId-only journey


# --- phase-2 orchestration preamble ------------------------------------------
async def _orchestration_preamble(baton: Baton, emit: EmitFn) -> None:
    """The order-engine lines that precede the first satellite call (phase 2).

    OrderService / CartHeaderService / OrganizationService (hierarchy) /
    ObjectAttributeService — emitted once, right before the SPT call.
    """
    ctx = baton.ctx
    ids = phase2_ids(ctx)
    thread = _worker_thread(baton)
    country_org = _COUNTRY_ORG.get(ctx.country, "8100")

    lines = [
        (_LOG_ORDER, "INFO", f"Get order by Order Number:{ctx.orderId}"),
        (_LOG_CART_HEADER, "INFO", f"Get Cart Header for id:{ctx.cartHeaderId}"),
        (_LOG_ORG, "INFO", f"Extracting organization for cart header: {ctx.cartHeaderId}"),
        (_LOG_ORG, "DEBUG", "Getting parent organization with id: 9100"),
        (_LOG_ORG, "DEBUG", f"Getting parent organization with id: {country_org}"),
        (_LOG_ORG, "DEBUG", "Getting parent organization with id: GLOBAL"),
        (_LOG_OBJ_ATTR, "INFO", f"Get Object attributes for id: {ctx.cartHeaderId}"),
    ]
    for logger, level, message in lines:
        await emit_line(emit, _PROF, logger=logger, level=level, message=message,
                        thread=thread, ids=ids)


# --- enrichment call / resp blocks -------------------------------------------
def _endpoint(sat: str, ctx: BatonContext) -> str:
    """The Feign call line's HTTP request text for a satellite."""
    acct = ctx.accountNumber
    country = ctx.country
    if sat == "spt":
        return (
            f"[SptClient#getSptPriceListCode] ---> GET "
            f"http://sptws-test.computacenter.com/api/v1/pricelist/{acct} HTTP/1.1"
        )
    if sat == "rsm":  # the pvc call — the rebate call is emitted in the resp filler
        return (
            f"[RsmClient#getPvcRates] ---> POST "
            f"http://rsmws-uat.computacenter.com/api/v1/rebate-schemes/customers/"
            f"{acct}/products/pvc?countryIdentifier={country} HTTP/1.1"
        )
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
    return f"[{sat}] ---> call"


def _make_enrich_call(sat: str):
    """Build the enrich_<sat>_call handler for one satellite."""

    async def _call(baton: Baton, emit: EmitFn) -> bool:
        ctx = baton.ctx
        thread = _worker_thread(baton)
        ids = phase2_ids(ctx)

        # SPT is the first satellite → emit the orchestration preamble first.
        if sat == "spt":
            await _orchestration_preamble(baton, emit)

        # Checker has no order-engine client call line — the checker service
        # emits everything in its `serve` block. Nothing to emit here.
        if sat == "checker":
            return True

        # RSM's call is preceded by ProductService/ProductPvcService filler.
        if sat == "rsm":
            await emit_line(emit, _PROF, logger=_LOG_PRODUCT, level="DEBUG", thread=thread,
                            message=f"Extracting product entities for card header {ctx.cartHeaderId}",
                            ids=ids)
            product_ids = ", ".join(line.productId for line in ctx.lines)
            await emit_line(emit, _PROF, logger=_LOG_PRODUCT_PVC, level="DEBUG", thread=thread,
                            message=f"Extracting PVC rates for products: [{product_ids}]",
                            ids=ids)

        logger = _CLIENT_LOGGER[sat]
        await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                        message=_endpoint(sat, ctx), ids=ids)
        return True

    _call.__name__ = f"enrich_{sat}_call"
    return _call


def _make_enrich_resp(sat: str):
    """Build the enrich_<sat>_resp handler for one satellite."""

    async def _resp(baton: Baton, emit: EmitFn) -> bool:
        ctx = baton.ctx
        thread = _worker_thread(baton)
        ids = phase2_ids(ctx)
        logger = _CLIENT_LOGGER.get(sat, f"c.c.orderengine.client.{sat.title()}Client")

        if sat == "spt":
            latency = random.randint(5, 20)
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=f"[SptClient#getSptPriceListCode] <--- HTTP/1.1 200 ({latency}ms)",
                            ids=ids)
            # Pricing filler: one retained-margin line per order line.
            for _line in ctx.lines:
                margin = round(random.uniform(15.0, 55.0), 2)
                await emit_line(emit, _PROF, logger=_LOG_PRICING, level="DEBUG", thread=thread,
                                message=f"Returning retained margin of: {margin}", ids=ids)

        elif sat == "rsm":
            latency = random.randint(60, 130)
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=f"[RsmClient#getPvcRates] <--- HTTP/1.1 200 ({latency}ms)",
                            ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_REBATE_ITEM, level="INFO", thread=thread,
                            message=f"Extracting cart items rebates for header id {ctx.cartHeaderId}",
                            ids=ids)
            # The second RSM call (getRebates) call+resp pair.
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=(
                                f"[RsmClient#getRebates] ---> POST "
                                f"http://rsmws-uat.computacenter.com/api/v1/rebate-schemes/customers/"
                                f"{ctx.accountNumber}/products/oe?countryIdentifier={ctx.country} HTTP/1.1"
                            ), ids=ids)
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=f"[RsmClient#getRebates] <--- HTTP/1.1 200 ({random.randint(90, 130)}ms)",
                            ids=ids)

        elif sat == "settings":
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=(
                                f"[SettingsClient#getAccountSettingByOrganizationHierarchy] "
                                f"<--- HTTP/1.1 200 ({random.randint(90, 130)}ms)"
                            ), ids=ids)
            # Post-settings filler: internal contracts (benign WARN), fees, cart.
            org = _ORG_SALES.get(ctx.country, "8100")
            await emit_line(emit, _PROF, logger=_LOG_INTERNAL_CONTRACT, level="DEBUG", thread=thread,
                            message=f"Attempting to retrieve internal contracts for sales org: {org}", ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_INTERNAL_CONTRACT, level="WARN", thread=thread,
                            message=f"No internal contracts found for sales org: {org}", ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_FEE_CONFIG, level="DEBUG", thread=thread,
                            message=f"Extracting FeeConfigs by countryCode {ctx.country}", ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_CART_BLOCKING, level="DEBUG", thread=thread,
                            message=f"Get cart blocking and grouping items for header id {ctx.cartHeaderId}", ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_TEXTS_OTHERS, level="INFO", thread=thread,
                            message=f"Extracting cart items universal attributes for line udfs by {ctx.cartHeaderId}", ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_FEE_ITEM, level="DEBUG", thread=thread,
                            message=f"Get cart fees for header id {ctx.cartHeaderId}", ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_CART_SOURCING, level="DEBUG", thread=thread,
                            message=f"Get cart sourcing items for cart header id {ctx.cartHeaderId}", ids=ids)

        elif sat == "jam":
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=(
                                f"[JamClient#getUserProfileWithPrivilegesBySamAccountName] "
                                f"<--- HTTP/1.1 200 ({random.randint(300, 450)}ms)"
                            ), ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_JWT, level="DEBUG", thread=thread,
                            message=(
                                "Generated jwt: eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
                                f"eyJzdWIiOiJ{ctx.user}IiwiZXhwIjoxNzUyNDg4ODAwfQ.mock-signature"
                            ), ids=ids)

        elif sat == "checker":
            # Checker's serve block emits the margin lines; nothing extra here.
            pass

        return True

    _resp.__name__ = f"enrich_{sat}_resp"
    return _resp


# Register the call/resp handlers for every enrich satellite.
from shared.scenarios import ENRICH_SATELLITES  # noqa: E402

for _sat in ENRICH_SATELLITES:
    register("order_engine", f"enrich_{_sat}_call")(_make_enrich_call(_sat))
    register("order_engine", f"enrich_{_sat}_resp")(_make_enrich_resp(_sat))


# --- dispatch ----------------------------------------------------------------
@register("order_engine", "dispatch")
async def dispatch(baton: Baton, emit: EmitFn) -> bool:
    """Publish the validated order to order.outbound.queue (phase 2)."""
    ctx = baton.ctx
    await emit_line(
        emit, _PROF, logger=_LOG_PUBLISHER, level="INFO", thread=_worker_thread(baton),
        message=(
            f"Published order {ctx.orderId} to queue order.outbound.queue "
            f"for SAP submission"
        ),
        ids=phase2_ids(ctx),
    )
    return True
