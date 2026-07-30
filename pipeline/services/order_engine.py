"""cc-order-engine emitter blocks (CLAUDE.md [1]) — the central orchestrator.

This is the busiest emitter. Its blocks:

  * ``create`` — phase 1: consume the creation request, persist the cart header,
    **mint orderId + cartHeaderId into ctx**, publish the creation response.
    This is the ONLY block that fills the order ids. Failure variant
    (``fail_at=create``, scenario 5): BM-DB timeout ×3 → failure response →
    the (inbound) ResponseListener failure line, and the chain stops.

  * ``enrich_<sat>_call`` / ``enrich_<sat>_resp`` — the Feign-style client
    ``--->`` / ``<---`` logs that bracket each satellite call, plus the
    order-engine-side processing filler shown around each satellite (org
    hierarchy + object attrs before the FIRST satellite; internal-contract/
    fee/cart filler after SETTINGS; pricing after SPT; PVC/rebate around RSM;
    resolved-product filler after SOLR; JWT after JAM). The satellite's own
    server-side ``serve`` block runs in between (its own module).

    Satellite ORDER comes from ``ENRICH_SATELLITES`` (Settings first) and is
    never hardcoded here — see ``_FIRST_SATELLITE``. AVALARA's handlers are
    registered too even though it is US-only and therefore not in that list.

  * ``dispatch`` — publish the validated order to order.outbound.queue.

Id discipline: ``create`` is phase 1 (eventId only) right up to the moment it
mints the ids; the response-publish line it emits is still eventId-only (the
order ids first *surface in a log* at the bridge). Every enrich/dispatch block
is phase 2 (both order ids, never eventId).
"""
from __future__ import annotations

import itertools
import random

from pipeline.services.blocklib import emit_line, phase1_ids, phase2_ids
from pipeline.services.profiles import ORDER_ENGINE_WORKER_THREADS, profile
from pipeline.services.registry import EmitFn, register
from shared.models import Baton, BatonContext
from shared.scenarios import AVALARA, ENRICH_SATELLITES

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

# --- id minting (see _mint_ids: uniqueness is load-bearing for correlation) ---
# Counters give uniqueness WITHIN a process run; the random bases only space
# successive runs apart (the counters restart at 0, but Postgres keeps the older
# run's journeys, so identical ids across runs would collide just the same).
# Order numbers keep the familiar ORD-6xxx..ORD-9xxx look of the reference data.
_ORDER_SEQ = itertools.count(random.randint(6000, 9000))
# cartHeaderId must be EXACTLY 19 digits (backend/stitching.py mines \b\d{19}\b).
# 13-digit prefix + 6-digit sequence = 19. The prefix keeps the reference data's
# leading "1840927365018" so the ids still look like the real system's.
_CART_PREFIX = "1840927365018"
assert len(_CART_PREFIX) == 13, "cartHeaderId must stay 19 digits: 13 + 6"
_CART_SEQ = itertools.count(random.randint(0, 999_999))

# Client (Feign) loggers per satellite. Checker is deliberately absent: in the
# reference dataset the margin check is invoked in-process (the checker service
# emits its own lines) with no order-engine client `--->`/`<---` log.
_CLIENT_LOGGER = {
    "spt": "c.c.orderengine.client.SptClient",
    "rsm": "c.c.orderengine.client.RsmClient",
    "solr": "c.c.orderengine.client.SolrClient",
    "settings": "c.c.orderengine.client.SettingsClient",
    "jam": "c.c.orderengine.client.JamClient",
    "avalara": "c.c.orderengine.client.AvalaraClient",
}

# The FIRST satellite in the enrichment order owns the one-off orchestration
# preamble (see _orchestration_preamble). Derived from ENRICH_SATELLITES rather
# than hardcoded, so reordering the satellites moves the preamble with it — it
# used to be pinned to "spt", which silently emitted it mid-enrichment once
# Settings became first.
_FIRST_SATELLITE = ENRICH_SATELLITES[0]

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
    """Assign a unique orderId + 19-digit cartHeaderId into ctx if absent.

    The injector leaves these None (they are born here). If they were pre-seeded
    (e.g. a deterministic test), keep them.

    **Uniqueness is load-bearing, not cosmetic.** The backend correlates journeys
    by an alias set of ids, so two flows sharing an orderId are merged into ONE
    journey: the loser stops receiving logs, never reaches a terminal marker, and
    is swept as ``TIMED_OUT`` after ``STALLED_TIMEOUT``. The previous
    ``random.randint(1, 999)`` had only 999 possible order numbers, so by the
    birthday bound a few dozen flows collided in practice.

    So the counter — not randomness — provides uniqueness: every call takes the
    next value, and all services run as asyncio tasks in ONE process
    (``run_all.py``), so ``next()`` needs no lock. The random *base* only spaces
    successive process runs apart, since the counter restarts at zero while
    Postgres keeps the previous run's journeys.

    ``cartHeaderId`` must stay EXACTLY 19 digits: ``backend/stitching.py`` mines
    it with ``\\b\\d{19}\\b`` (anchored both ends), so a 20-digit value would
    silently stop correlating. Hence the fixed 19-digit width below — the prefix
    shrinks to make room for the 6-digit sequence.
    """
    if ctx.orderId is None:
        ctx.orderId = f"ORD-{next(_ORDER_SEQ)}"
    if ctx.cartHeaderId is None:
        # 13-digit prefix + 6-digit sequence = 19 digits exactly.
        ctx.cartHeaderId = f"{_CART_PREFIX}{next(_CART_SEQ) % 1_000_000:06d}"


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
    ObjectAttributeService — emitted once, right before the FIRST satellite's
    call line (Settings, per ENRICH_SATELLITES).
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
    if sat == "solr":
        product_ids = ",".join(line.productId for line in ctx.lines)
        return (
            f"[SolrClient#searchProductsByIds] ---> GET "
            f"http://solrws-uat.computacenter.com/solr/products_{country.lower()}/select"
            f"?q=productId:({product_ids})&rows={len(ctx.lines)} HTTP/1.1"
        )
    if sat == "avalara":
        return (
            f"[AvalaraClient#resolveShipToAddress] ---> POST "
            f"http://avalarews-uat.computacenter.com/api/v1/addresses/resolve/"
            f"{acct}?countryIdentifier={country} HTTP/1.1"
        )
    return f"[{sat}] ---> call"


def _make_enrich_call(sat: str):
    """Build the enrich_<sat>_call handler for one satellite."""

    async def _call(baton: Baton, emit: EmitFn) -> bool:
        ctx = baton.ctx
        thread = _worker_thread(baton)
        ids = phase2_ids(ctx)

        # The first satellite in the enrichment order → emit the one-off
        # orchestration preamble before its call line.
        if sat == _FIRST_SATELLITE:
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

        elif sat == "solr":
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=(
                                f"[SolrClient#searchProductsByIds] "
                                f"<--- HTTP/1.1 200 ({random.randint(20, 70)}ms)"
                            ), ids=ids)
            await emit_line(emit, _PROF, logger=_LOG_PRODUCT, level="DEBUG", thread=thread,
                            message=(
                                f"Resolved {len(ctx.lines)} product entities from search "
                                f"for cart header {ctx.cartHeaderId}"
                            ), ids=ids)

        elif sat == "avalara":
            await emit_line(emit, _PROF, logger=logger, level="DEBUG", thread=thread,
                            message=(
                                f"[AvalaraClient#resolveShipToAddress] "
                                f"<--- HTTP/1.1 200 ({random.randint(120, 260)}ms)"
                            ), ids=ids)

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
#
# AVALARA is included explicitly: it is US-only, so ``shared/scenarios.py``
# appends its trio conditionally rather than listing it in ENRICH_SATELLITES —
# but the handlers must still be registered, or a US chain would dispatch to a
# missing block.
for _sat in [*ENRICH_SATELLITES, AVALARA]:
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
