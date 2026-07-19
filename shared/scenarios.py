"""The 10 canonical scenarios + step-chain compiler — single source of truth.

This module is *declarative*: it defines the ten canonical order flows
(CLAUDE.md "[1] ... The 10 canonical scenarios") and compiles each into the
ordered ``(service, block)`` step chain a Baton carries. It writes **no log
text** and mints **no ids** — the service blocks emit logs from the Baton
``ctx`` at run time, and ``injector/inject.py`` mints the concrete
``eventId`` / ``orderId`` / ``cartHeaderId`` when it turns a scenario into a
live Baton.

Consumers:
* ``injector/inject.py`` — compiles ``SCENARIOS[n]`` + fresh ids into a Baton.
* ``services/`` + ``services/runner.py`` — the block names emitted here are the
  vocabulary each service must register (see ``BLOCKS`` below).
* tests — ``Scenario`` metadata (``outcome``, ``reaches_creation``,
  ``terminal``) is the ground truth for outcome / correlation-invariant tests.

Correlation model (why the chain order matters — CLAUDE.md "THE CORRELATION
MODEL"): every step before ``order_engine/create`` is phase 1 and can only
carry ``eventId``; ``inbound/bridge`` exposes the new order id(s) per
``bridge_ids``; every step after it is phase 2 and carries both order ids. The
id lifecycle is therefore *emergent from the compiled step order* — this module
just guarantees that order (and, via ``fail_at``, whether creation is reached
at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.models import BridgeIds, OrderLine

# --- Services (app_name stems) -----------------------------------------------
# The first element of every step tuple. Matches CLAUDE.md's service table.
INBOUND = "inbound"
ORDER_ENGINE = "order_engine"
SPT = "spt"
RSM = "rsm"
SOLR = "solr"
SETTINGS = "settings"
JAM = "jam"
CHECKER = "checker"
AVALARA = "avalara"
VALIDATOR = "validator"
OUTBOUND = "outbound_osw"
TRACK_TRACE = "track_trace"


# --- Block names (the second element of every step tuple) ---------------------
# This is the contract with services/ and runner.py: each service must register
# a handler for every block name that can appear against it in a step chain.
#
# Enrichment uses the *fine-grained* encoding: for each satellite the order
# engine emits a client "--->" call block, the satellite emits its "serve"
# block, then the order engine emits the "<---" response block. That trio maps
# 1:1 onto the real Feign-style log sequence, so the runner stays dumb.
class BLOCKS:
    # phase 1 — pre-creation (eventId only)
    RECEIVE = "receive"          # inbound: receive + transform + SKU map + publish
    CREATE = "create"            # order_engine: create order, fill ids, publish response
    # bridge — the one log where eventId coexists with the new order id(s)
    BRIDGE = "bridge"            # inbound: logs the creation response
    # phase 2 — post-creation (orderId + cartHeaderId)
    #   enrichment satellite trio (per satellite): call -> serve -> resp
    ENRICH_CALL = "enrich_{sat}_call"    # order_engine client ---> log
    SERVE = "serve"                      # satellite server-side log
    ENRICH_RESP = "enrich_{sat}_resp"    # order_engine client <--- log
    VALIDATE = "validate"        # validator: strategy logs (incl. benign WARNs)
    DISPATCH = "dispatch"        # order_engine: publish to order.outbound.queue
    SUBMIT = "submit"            # outbound_osw: SAP submission
    REGISTER = "register"        # track_trace: success terminal


def _enrich_call(sat: str) -> str:
    return BLOCKS.ENRICH_CALL.format(sat=sat)


def _enrich_resp(sat: str) -> str:
    return BLOCKS.ENRICH_RESP.format(sat=sat)


# --- Enrichment satellite order ----------------------------------------------
# The order the order engine calls each satellite during enrich, taken from the
# reference dataset (data/mock-order-flows-v2.json): SPT -> RSM -> SETTINGS ->
# JAM -> CHECKER.
#
# Two services from the CLAUDE.md table are deliberately NOT standalone enrich
# satellites, because the reference dataset does not emit them as separate
# server-side steps during enrichment:
#   * SOLR  — product-id resolution happens inside the order engine; there is no
#             cc-solr-service `serve` block in any reference flow.
#   * AVALARA — US ship-to verification is emitted by cc-validator-service (its
#             AvalaraClient + ValidateShipToWithAvalara strategy), NOT by a
#             standalone service. So Avalara is handled inside the validator's
#             `validate` block for US flows, not as an enrich satellite.
ENRICH_SATELLITES: list[str] = [SPT, RSM, SETTINGS, JAM, CHECKER]


# --- Scenario definition ------------------------------------------------------
@dataclass(frozen=True)
class Scenario:
    """One canonical flow: its ctx seed, failure knob, and test ground-truth."""

    id: int
    name: str
    outcome: str

    # ctx seed (injector fills eventId + concrete ids at run time)
    country: str
    user: str
    accountNumber: str
    lines: list[OrderLine]
    bridge_ids: BridgeIds = "both"
    fail_at: str | None = None          # block name that fails, or None

    # test ground-truth (derived-but-explicit, so tests key off one place)
    reaches_creation: bool = True       # False for pre-creation failures (4, 5)
    terminal: tuple[str, str] | None = None  # last (service, block) of the chain

    def context_seed(self) -> dict:
        """The static ctx fields for this scenario (no ids — injector mints those).

        Returned as a plain dict so ``injector`` can merge in the freshly-minted
        ``eventId`` (and leave ``orderId``/``cartHeaderId`` as ``None``) before
        constructing a ``BatonContext``.
        """
        return {
            "accountNumber": self.accountNumber,
            "country": self.country,
            "user": self.user,
            "lines": list(self.lines),
            "bridge_ids": self.bridge_ids,
            "fail_at": self.fail_at,
        }


# --- Step-chain compiler ------------------------------------------------------
def _full_chain(scenario: Scenario) -> list[tuple[str, str]]:
    """The complete success chain for a scenario, ignoring ``fail_at``.

    ``compile_steps`` then truncates this at the failing block.
    """
    steps: list[tuple[str, str]] = []

    # phase 1 — pre-creation
    steps.append((INBOUND, BLOCKS.RECEIVE))
    steps.append((ORDER_ENGINE, BLOCKS.CREATE))
    # bridge
    steps.append((INBOUND, BLOCKS.BRIDGE))

    # phase 2 — enrichment (fine-grained satellite trio each). US Avalara
    # verification is NOT a satellite here — the validator emits it (see the
    # ENRICH_SATELLITES note and services/validator.py).
    for sat in ENRICH_SATELLITES:
        steps.append((ORDER_ENGINE, _enrich_call(sat)))
        steps.append((sat, BLOCKS.SERVE))
        steps.append((ORDER_ENGINE, _enrich_resp(sat)))

    # validation → dispatch → SAP submit → tracking (success terminal)
    steps.append((VALIDATOR, BLOCKS.VALIDATE))
    steps.append((ORDER_ENGINE, BLOCKS.DISPATCH))
    steps.append((OUTBOUND, BLOCKS.SUBMIT))
    steps.append((TRACK_TRACE, BLOCKS.REGISTER))
    return steps


# Which block a given ``fail_at`` name aborts on. ``fail_at`` uses the CLAUDE.md
# scenario vocabulary (e.g. "transform", "margin", "udf", "sap", "spt"); this
# maps each to the step tuple whose emission fails and stops the chain.
def _failing_step(fail_at: str, chain: list[tuple[str, str]]) -> int:
    """Return the index in ``chain`` of the step that fails for ``fail_at``.

    The chain is truncated *inclusive* of this step (the block emits its failure
    variant, then the baton is not forwarded).
    """
    match fail_at:
        case "transform":
            target = (INBOUND, BLOCKS.RECEIVE)
        case "create":
            target = (ORDER_ENGINE, BLOCKS.CREATE)
        case "spt":
            target = (SPT, BLOCKS.SERVE)
        case "jam":
            target = (JAM, BLOCKS.SERVE)
        case "margin":
            target = (CHECKER, BLOCKS.SERVE)
        case "udf":
            target = (VALIDATOR, BLOCKS.VALIDATE)
        case "sap":
            target = (OUTBOUND, BLOCKS.SUBMIT)
        case _:
            raise ValueError(f"unknown fail_at block: {fail_at!r}")
    try:
        return chain.index(target)
    except ValueError as exc:  # pragma: no cover - guards scenario/chain drift
        raise ValueError(
            f"fail_at {fail_at!r} maps to {target} which is not in this chain"
        ) from exc


def compile_steps(scenario: Scenario) -> list[tuple[str, str]]:
    """Compile a scenario into its ``Baton.steps`` chain.

    Success scenarios return the full chain; failure scenarios truncate the
    chain *inclusive* of the failing block, so nothing runs past a fatal
    failure (CLAUDE.md: "the baton is not forwarded past a fatal failure").
    """
    chain = _full_chain(scenario)
    if scenario.fail_at is None:
        return chain
    end = _failing_step(scenario.fail_at, chain)
    return chain[: end + 1]


# --- Products (per scenario ctx.lines) ---------------------------------------
# productId -> internal SKU. The SKU may be left None for a scenario that fails
# in transform (unknown product), but here we give real mappings; the failing
# transform is driven by ``fail_at``, not by a missing SKU in the seed.
def _line(product_id: str, sku: str | None = None) -> OrderLine:
    return OrderLine(productId=product_id, sku=sku)


# --- The 10 canonical scenarios ----------------------------------------------
# Ground truth for tests. Mirrors CLAUDE.md's table exactly.
SCENARIOS: dict[int, Scenario] = {
    1: Scenario(
        id=1,
        name="Happy path — 3-line UK order (GPUs + workstation)",
        outcome="SUCCESS",
        country="UK",
        user="RFLORIA",
        accountNumber="81036533",
        lines=[
            _line("4788230", "SKU-APC-UPS-3000VA"),
            _line("4249751", "SKU-DELL-P7680-I9"),
            _line("5001914", "SKU-GPU-H100-80GB"),
        ],
        bridge_ids="both",
        terminal=(TRACK_TRACE, BLOCKS.REGISTER),
    ),
    2: Scenario(
        id=2,
        name="Happy path — 1-line DE order via Salesforce",
        outcome="SUCCESS",
        country="DE",
        user="MWEBER",
        accountNumber="62011948",
        lines=[_line("3652269", "SKU-GPU-A100-80GB")],
        bridge_ids="order",
        terminal=(TRACK_TRACE, BLOCKS.REGISTER),
    ),
    3: Scenario(
        id=3,
        name="Happy path — US order (Avalara runs)",
        outcome="SUCCESS",
        country="US",
        user="JSMITH",
        accountNumber="55829104",
        lines=[
            _line("3652269", "SKU-GPU-A100-80GB"),
            _line("4788230", "SKU-APC-UPS-3000VA"),
        ],
        bridge_ids="cart",
        terminal=(TRACK_TRACE, BLOCKS.REGISTER),
    ),
    4: Scenario(
        id=4,
        name="Inbound transform failed (unknown product)",
        outcome="INBOUND_TRANSFORM_FAILED",
        country="UK",
        user="RFLORIA",
        accountNumber="81036533",
        lines=[_line("9999999")],  # unknown product → transform fails
        fail_at="transform",
        reaches_creation=False,     # never created — eventId-only journey
        terminal=(INBOUND, BLOCKS.RECEIVE),
    ),
    5: Scenario(
        id=5,
        name="Order creation failed (BM DB timeout)",
        outcome="ORDER_CREATION_FAILED",
        country="UK",
        user="RFLORIA",
        accountNumber="81036533",
        lines=[_line("4249751", "SKU-DELL-P7680-I9")],
        fail_at="create",
        reaches_creation=False,     # creation itself fails — still eventId-only
        terminal=(ORDER_ENGINE, BLOCKS.CREATE),
    ),
    6: Scenario(
        id=6,
        name="Margin check failed (below threshold)",
        outcome="MARGIN_CHECK_FAILED",
        country="UK",
        user="RFLORIA",
        accountNumber="70443218",
        lines=[_line("4249751", "SKU-DELL-P7680-I9")],
        bridge_ids="order",
        fail_at="margin",
        terminal=(CHECKER, BLOCKS.SERVE),
    ),
    7: Scenario(
        id=7,
        name="Validation failed (missing costCenter UDF)",
        outcome="VALIDATION_FAILED",
        country="UK",
        user="RFLORIA",
        accountNumber="81036533",
        lines=[_line("5001914", "SKU-GPU-H100-80GB")],
        bridge_ids="both",
        fail_at="udf",
        terminal=(VALIDATOR, BLOCKS.VALIDATE),
    ),
    8: Scenario(
        id=8,
        name="Enrichment failed (SPT down)",
        outcome="ENRICHMENT_FAILED",
        country="UK",
        user="RFLORIA",
        accountNumber="81036533",
        lines=[_line("3652269", "SKU-GPU-A100-80GB")],
        bridge_ids="cart",
        fail_at="spt",
        terminal=(SPT, BLOCKS.SERVE),
    ),
    9: Scenario(
        id=9,
        name="Auth failed (JAM 403 account disabled)",
        outcome="AUTH_FAILED",
        country="UK",
        user="XDISABLED",
        accountNumber="81036533",
        lines=[_line("4788230", "SKU-APC-UPS-3000VA")],
        bridge_ids="order",
        fail_at="jam",
        terminal=(JAM, BLOCKS.SERVE),
    ),
    10: Scenario(
        id=10,
        name="SAP submission failed (RFC failure)",
        outcome="SAP_SUBMISSION_FAILED",
        country="UK",
        user="RFLORIA",
        accountNumber="81036533",
        lines=[
            _line("4249751", "SKU-DELL-P7680-I9"),
            _line("5001914", "SKU-GPU-H100-80GB"),
        ],
        bridge_ids="both",
        fail_at="sap",
        terminal=(OUTBOUND, BLOCKS.SUBMIT),
    ),
}


def all_scenarios() -> list[Scenario]:
    """The 10 scenarios in id order (1..10)."""
    return [SCENARIOS[i] for i in sorted(SCENARIOS)]
