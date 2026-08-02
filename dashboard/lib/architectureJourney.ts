/**
 * The animated order journey — one order's path across the architecture map.
 *
 * This replays the auto-approval HAPPY PATH as a sequence of hops along the
 * diagram's own edges: a US order (so Avalara fires), the order.approval
 * route, all rules passing, ending with the closing `order_created` hop back
 * to Inbound — the hop that makes the five-hop ping-pong legible. It is a
 * TEACHING animation over static data: nothing here reads the backend, and
 * the step list is hand-written from the compiled step chain rather than
 * fetched. There is deliberately no route picker.
 *
 * ## Why the phases matter (CLAUDE.md, "THE CORRELATION MODEL")
 *
 * The point of the animation is that **the identifier changes over the
 * journey's lifetime** — there is no single id present on every log:
 *
 *  - **Phase 1 (pre-creation)** — Orders → SAP BTP → Inbound → the Order
 *    Engine's defaults-only first turn → back to Inbound → Inbound's WHOLE
 *    enrichment leg (Settings → JAM → SOLR) → order.approval. Logs carry
 *    **`eventId` ONLY**. No `orderId`, no `cartHeaderId`: they do not exist
 *    yet — the satellites are consulted about an order that has never been
 *    persisted.
 *  - **The `order_data_ready` return leg** — the engine answers its first
 *    turn and the flow goes BACKWARDS to Inbound. Still `eventId` only; this
 *    ack links nothing (there are no order ids to link). Shown as its own
 *    phase because it is the first of the two return legs.
 *  - **Creation** — on `order.approval` the engine persists the cart header
 *    to BM DB (order INACTIVE) and mints `orderId` + `cartHeaderId`. Its own
 *    creation logs carry `eventId` as a field *and print the new order ids in
 *    their message text* — that text is the join the stitcher mines.
 *  - **Phase 2 (post-creation)** — Track & Trace registration (mid-flow,
 *    right after creation), then SPT → RSM → Validator → Avalara → Checker,
 *    dispatch on order.create.sap, SAP submission, and the closing
 *    `order_created` back at Inbound — the journey's SUCCESS terminal.
 *    `eventId` is GONE; every log carries **`orderId` + `cartHeaderId`**.
 *
 * ## Where the satellite order comes from
 *
 * Both legs come from `shared/scenarios.py` — `INBOUND_SATELLITES` (Settings,
 * JAM, SOLR; SOLR's placement inferred, not documented) and
 * `ENRICH_SATELLITES` (SPT, RSM, Validator, Checker, with Avalara inserted
 * before Checker for US orders — the documented auto-approval rule order).
 * Every satellite is a real baton step now, and this replay walks them in
 * exactly the compiled order — the old note that SOLR and Avalara were walked
 * at "conceptual" positions is obsolete: there is nothing conceptual left,
 * the map and the step chain agree.
 */

/** Which ids a log emitted at this point in the journey would carry. */
export type JourneyPhase = "phase1" | "creation" | "bridge" | "phase2";

export interface JourneyStep {
  /** Edge to travel, by node ids. Must match an edge in ARCH_EDGES. */
  from: string;
  to: string;
  /**
   * Traverse the edge's path backwards. The two return legs
   * (`order_data_ready`, `order_created`) reuse the spine geometry in reverse
   * rather than adding duplicate return edges — the connector anchors both
   * directions to the same points, so a second edge would draw exactly on top
   * of the first (the spine edges are bidirectional for the same reason).
   */
  reverse?: boolean;
  phase: JourneyPhase;
  /** Caption shown in the status strip while this hop runs. */
  caption: string;
  /**
   * Node lit up when the hop ARRIVES. Usually `to` (or `from` when reversed);
   * set explicitly when the interesting node differs.
   */
  highlight?: string;
  /** Extra dwell after arriving (ms), for hops that deserve a beat. */
  dwell?: number;
}

/** What the id panel shows during each phase. */
export interface JourneyIds {
  eventId: string | null;
  orderId: string | null;
  cartHeaderId: string | null;
}

/**
 * Concrete ids for the replay. Fixed, not random: the animation is an
 * explanation, and a stable order number is easier to follow across the
 * `eventId` → `orderId` handover than a value that changes every run. Shapes
 * match the real minting rules (`evt-<uuid>`, `ORD-<seq>`, 19-digit cart id).
 */
export const DEMO_IDS = {
  eventId: "evt-372656a7-9f14-4c2b-8d51-6ac1b0e37742",
  orderId: "ORD-6001",
  cartHeaderId: "1840927365018240001",
} as const;

/** The ids visible to a log emitted during each phase. */
export function idsForPhase(phase: JourneyPhase): JourneyIds {
  switch (phase) {
    case "phase1":
      // Pre-creation: the order ids genuinely do not exist yet — including
      // through Inbound's whole Settings/JAM/SOLR leg.
      return { eventId: DEMO_IDS.eventId, orderId: null, cartHeaderId: null };
    case "creation":
      // The join: eventId as a FIELD, the fresh order ids in the message TEXT.
      return {
        eventId: DEMO_IDS.eventId,
        orderId: DEMO_IDS.orderId,
        cartHeaderId: DEMO_IDS.cartHeaderId,
      };
    case "bridge":
      // The order_data_ready return leg — eventId only, links nothing (there
      // is nothing to link yet).
      return { eventId: DEMO_IDS.eventId, orderId: null, cartHeaderId: null };
    case "phase2":
      // eventId is gone for good.
      return { eventId: null, orderId: DEMO_IDS.orderId, cartHeaderId: DEMO_IDS.cartHeaderId };
  }
}

/** Human-readable phase name for the status strip. */
export const PHASE_LABEL: Record<JourneyPhase, string> = {
  phase1: "Phase 1 · pre-creation",
  creation: "Order created",
  bridge: "order_data_ready · return leg",
  phase2: "Phase 2 · post-creation",
};

/**
 * The journey, hop by hop — the five-hop ping-pong with both enrichment legs.
 *
 * Enrichment is drawn as call-and-return per satellite (the caller's Feign
 * `--->` / `<---` pair), which is why each satellite appears twice.
 */
export const JOURNEY_STEPS: JourneyStep[] = [
  // ── Phase 1 — eventId only ──────────────────────────────────────────────
  {
    from: "orders_src",
    to: "sap_btp",
    phase: "phase1",
    caption: "Order arrives from B2B / Salesforce",
  },
  {
    from: "sap_btp",
    to: "inbound",
    phase: "phase1",
    caption: "SAP BTP posts the order to Inbound — the eventId is born; audit row written",
  },
  {
    from: "inbound",
    to: "rmq_in",
    phase: "phase1",
    caption: "Inbound transforms the order, maps SKUs, and publishes order.init  (hop 1 of 5)",
  },
  {
    from: "rmq_in",
    to: "order_engine",
    phase: "phase1",
    caption: "order.init reaches the Order Engine — its FIRST turn",
  },

  // ── Return leg 1: order_data_ready ──────────────────────────────────────
  {
    from: "rmq_in",
    to: "order_engine",
    reverse: true,
    phase: "bridge",
    caption:
      "The engine assembles DEFAULT order data — nothing persisted — and answers order_data_ready  (hop 2 of 5)",
  },
  {
    from: "inbound",
    to: "rmq_in",
    reverse: true,
    phase: "bridge",
    highlight: "inbound",
    caption: "order_data_ready returns to Inbound — still eventId only; there are no order ids to link",
    dwell: 1100,
  },

  // ── Inbound's pre-creation enrichment leg ───────────────────────────────
  {
    from: "inbound",
    to: "settings",
    phase: "phase1",
    caption: "Settings — Inbound reads the account's margin thresholds",
  },
  {
    from: "inbound",
    to: "settings",
    reverse: true,
    phase: "phase1",
    highlight: "inbound",
    caption: "Settings returns the thresholds",
  },
  {
    from: "inbound",
    to: "jam",
    phase: "phase1",
    caption: "JAM — Inbound authenticates the user and fetches privileges",
  },
  {
    from: "inbound",
    to: "jam",
    reverse: true,
    phase: "phase1",
    highlight: "inbound",
    caption: "JAM returns privileges; Inbound mints the JWT",
  },
  {
    from: "inbound",
    to: "solr",
    phase: "phase1",
    caption: "SOLR — Inbound matches the order's catalogue lines",
  },
  {
    from: "inbound",
    to: "solr",
    reverse: true,
    phase: "phase1",
    highlight: "inbound",
    caption: "Catalogue lines matched — the pre-creation checks all passed",
  },

  // ── Hop 3: order.approval → creation ────────────────────────────────────
  {
    from: "inbound",
    to: "rmq_in",
    phase: "phase1",
    caption: "Inbound publishes order.approval — requesting creation  (hop 3 of 5)",
  },
  {
    from: "rmq_in",
    to: "order_engine",
    phase: "phase1",
    caption: "order.approval reaches the Order Engine — its SECOND turn",
  },
  {
    from: "order_engine",
    to: "bm_db",
    phase: "creation",
    caption:
      "The engine persists the cart header (order INACTIVE) and mints the ids — the creation logs' text is the correlation join",
    dwell: 900,
  },

  // ── Phase 2 — orderId + cartHeaderId ────────────────────────────────────
  {
    from: "order_engine",
    to: "track_trace",
    phase: "phase2",
    caption: "Track & Trace registers the order — MID-flow now, before any check has run",
  },
  {
    from: "order_engine",
    to: "track_trace",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Back at the engine for enrichment — every log now carries orderId + cartHeaderId",
  },
  { from: "order_engine", to: "spt", phase: "phase2", caption: "SPT — get the account's price lists" },
  {
    from: "order_engine",
    to: "spt",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "SPT returns pricing",
  },
  { from: "order_engine", to: "rsm", phase: "phase2", caption: "RSM — compute rebates / PVC rates" },
  {
    from: "order_engine",
    to: "rsm",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "RSM returns rebates",
  },
  {
    from: "order_engine",
    to: "validator",
    phase: "phase2",
    caption: "Validator — auto-approval rule 1: run the validation strategies",
  },
  {
    from: "order_engine",
    to: "validator",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Validation passed",
  },
  {
    from: "order_engine",
    to: "avalara",
    phase: "phase2",
    caption: "Avalara — still rule 1: verify the US ship-to address (US orders only)",
  },
  {
    from: "order_engine",
    to: "avalara",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Address verified",
  },
  {
    from: "order_engine",
    to: "checker",
    phase: "phase2",
    caption: "Checker — rule 3: the margin check, last of the rules",
  },
  {
    from: "order_engine",
    to: "checker",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Margin check passed — every auto-approval rule is green",
  },

  // ── Hop 4: dispatch and submission ──────────────────────────────────────
  {
    from: "order_engine",
    to: "outbound",
    phase: "phase2",
    caption: "Dispatched on order.create.sap → Outbound OSW  (hop 4 of 5)",
  },
  {
    from: "outbound",
    to: "sap_ful",
    phase: "phase2",
    caption: "Submitted to SAP fulfilment via RFC",
    dwell: 500,
  },

  // ── Hop 5: order_created — the loop closes at Inbound ───────────────────
  {
    from: "rmq_in",
    to: "order_engine",
    reverse: true,
    phase: "phase2",
    caption: "The engine publishes order_created — closing the loop  (hop 5 of 5)",
  },
  {
    from: "inbound",
    to: "rmq_in",
    reverse: true,
    phase: "phase2",
    highlight: "inbound",
    caption:
      "order_created lands at Inbound: order processing complete — the journey's SUCCESS terminal",
    dwell: 1400,
  },
];

/** Milliseconds a single hop takes to travel, before any per-step dwell. */
export const HOP_DURATION = 620;
