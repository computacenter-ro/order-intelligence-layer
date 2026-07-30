/**
 * The animated order journey — one order's path across the architecture map.
 *
 * This replays the canonical SUCCESS flow (scenario 1 in `shared/scenarios.py`)
 * as a sequence of hops along the diagram's own edges. It is a TEACHING
 * animation over static data: nothing here reads the backend, and the step list
 * is hand-written from the compiled step chain rather than fetched.
 *
 * ## Why the phases matter (CLAUDE.md, "THE CORRELATION MODEL")
 *
 * The point of the animation is that **the identifier changes over the
 * journey's lifetime** — there is no single id present on every log:
 *
 *  - **Phase 1 (pre-creation)** — Orders → SAP BTP → Inbound → RabbitMQ →
 *    Order Engine. Logs carry **`eventId` ONLY**. No `orderId`, no
 *    `cartHeaderId`: they do not exist yet.
 *  - **Creation** — the Order Engine persists the cart header to BM DB and
 *    mints `orderId` + `cartHeaderId`. Its own creation logs carry `eventId`
 *    as a field *and print the new order ids in their message text* — that
 *    text is the join the stitcher mines.
 *  - **The return leg (the bridge)** — the engine publishes a creation
 *    response and **Inbound logs it**, so the flow goes BACKWARDS to Inbound
 *    for one hop. That ack carries **`eventId` only** and deliberately links
 *    nothing; it is kept because it is what the real service does. This is the
 *    hinge where phase 1 ends.
 *  - **Phase 2 (post-creation)** — enrichment (SPT → RSM → SOLR → Settings →
 *    JAM → Checker → Avalara), validation, dispatch, SAP submission, tracking.
 *    `eventId` is GONE; every log carries **`orderId` + `cartHeaderId`**.
 *
 * ## Where the satellite order comes from, and where it deliberately differs
 *
 * The five satellites that are their own baton steps come from
 * `shared/scenarios.py` (`ENRICH_SATELLITES` = SPT, RSM, Settings, JAM,
 * Checker), and this replay keeps them in exactly that order.
 *
 * SOLR and Avalara are drawn on the map as engine-called satellites with their
 * own connectors, but they are NOT standalone steps in the emitter chain: SOLR
 * resolution happens inside the engine, and Avalara is emitted by the
 * validator. This replay still walks the token through both, at the position
 * the enrichment conceptually reaches them — SOLR after RSM, Avalara after
 * Checker as the last enrichment call before dispatch — because the animation
 * explains the ARCHITECTURE the map draws, not the baton step list. It is an
 * illustration over static data: it reads no backend, and nothing downstream
 * derives a step chain from it. The emitters, scenarios, and the Journeys
 * pipeline trail are untouched by this and remain the authority on what a real
 * run emits.
 */

/** Which ids a log emitted at this point in the journey would carry. */
export type JourneyPhase = "phase1" | "creation" | "bridge" | "phase2";

export interface JourneyStep {
  /** Edge to travel, by node ids. Must match an edge in ARCH_EDGES. */
  from: string;
  to: string;
  /**
   * Traverse the edge's path backwards. The bridge hop reuses the
   * Inbound→RabbitMQ→Engine geometry in reverse rather than adding a return
   * edge to the diagram, which would clutter a map that is also read statically.
   */
  reverse?: boolean;
  phase: JourneyPhase;
  /** Caption shown in the status strip while this hop runs. */
  caption: string;
  /**
   * Node lit up when the hop ARRIVES. Usually `to`, but the bridge lights
   * Inbound while the ids on the wire are still the phase-1 ones.
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
 * Concrete ids for the replay. Fixed, not random: the animation is a
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
      // Pre-creation: the order ids genuinely do not exist yet.
      return { eventId: DEMO_IDS.eventId, orderId: null, cartHeaderId: null };
    case "creation":
      // The join: eventId as a FIELD, the fresh order ids in the message TEXT.
      return {
        eventId: DEMO_IDS.eventId,
        orderId: DEMO_IDS.orderId,
        cartHeaderId: DEMO_IDS.cartHeaderId,
      };
    case "bridge":
      // The ack links nothing — eventId only, by design.
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
  bridge: "Bridge ack · return leg",
  phase2: "Phase 2 · post-creation",
};

/**
 * The journey, hop by hop.
 *
 * Enrichment is drawn as call-and-return per satellite (the engine's Feign
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
    caption: "SAP BTP hands the order event to Inbound",
  },
  {
    from: "inbound",
    to: "rmq_in",
    phase: "phase1",
    caption: "Inbound transforms the order and maps vendor IDs to internal SKUs",
  },
  {
    from: "rmq_in",
    to: "order_engine",
    phase: "phase1",
    caption: "order.inbound.queue delivers it to the Order Engine",
  },

  // ── Creation — the ids are born here ────────────────────────────────────
  {
    from: "order_engine",
    to: "bm_db",
    phase: "creation",
    caption: "Order Engine persists the cart header and generates the order number",
    dwell: 900,
  },

  // ── The return leg — this is the hinge ──────────────────────────────────
  // The engine publishes its creation response to order.response.queue and
  // INBOUND logs it: the flow goes backwards one hop. Travelling the two spine
  // edges in reverse is what makes the "we go back" visible.
  {
    from: "rmq_in",
    to: "order_engine",
    reverse: true,
    phase: "bridge",
    caption: "Creation response published back to order.response.queue",
  },
  {
    from: "inbound",
    to: "rmq_in",
    reverse: true,
    phase: "bridge",
    highlight: "inbound",
    caption: "Inbound logs the ack — eventId ONLY, so this line links nothing",
    dwell: 1100,
  },

  // ── Phase 2 — orderId + cartHeaderId ────────────────────────────────────
  {
    from: "inbound",
    to: "rmq_in",
    phase: "phase2",
    caption: "Enrichment begins — every log now carries orderId + cartHeaderId",
  },
  {
    from: "rmq_in",
    to: "order_engine",
    phase: "phase2",
    caption: "Back at the Order Engine, now orchestrating enrichment",
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
    to: "solr",
    phase: "phase2",
    caption: "SOLR — resolve the product IDs for the order's lines",
  },
  {
    from: "order_engine",
    to: "solr",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "SOLR returns the resolved product IDs",
  },
  {
    from: "order_engine",
    to: "settings",
    phase: "phase2",
    caption: "Settings — read the margin thresholds",
  },
  {
    from: "order_engine",
    to: "settings",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Settings returns the thresholds",
  },
  {
    from: "order_engine",
    to: "jam",
    phase: "phase2",
    caption: "JAM — authenticate the user and issue a JWT",
  },
  {
    from: "order_engine",
    to: "jam",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "JAM returns privileges",
  },
  {
    from: "order_engine",
    to: "checker",
    phase: "phase2",
    caption: "Checker — margin check against the threshold",
  },
  {
    from: "order_engine",
    to: "checker",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Margin check passed",
  },
  {
    from: "order_engine",
    to: "avalara",
    phase: "phase2",
    caption: "Avalara — verify the US ship-to address (US orders only)",
  },
  {
    from: "order_engine",
    to: "avalara",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Address verified — the last enrichment call before dispatch",
  },
  {
    from: "order_engine",
    to: "validator",
    phase: "phase2",
    caption: "Validator — run the validation strategies",
  },
  {
    from: "order_engine",
    to: "validator",
    reverse: true,
    phase: "phase2",
    highlight: "order_engine",
    caption: "Validation passed",
  },

  // ── Dispatch and submission ─────────────────────────────────────────────
  {
    from: "order_engine",
    to: "outbound",
    phase: "phase2",
    caption: "Dispatched to order.outbound.queue → Outbound OSW",
  },
  {
    from: "outbound",
    to: "sap_ful",
    phase: "phase2",
    caption: "Submitted to SAP fulfilment via RFC",
    dwell: 500,
  },

  // ── Success terminal ────────────────────────────────────────────────────
  {
    from: "order_engine",
    to: "track_trace",
    phase: "phase2",
    caption: "Registered for tracking — the journey's SUCCESS terminal event",
    dwell: 1400,
  },
];

/** Milliseconds a single hop takes to travel, before any per-step dwell. */
export const HOP_DURATION = 620;
