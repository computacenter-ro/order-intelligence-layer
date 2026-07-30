/**
 * The simulated order pipeline, as a hand-authored graph.
 *
 * This is the project's architecture diagram (CLAUDE.md, "The simulated
 * production system") expressed as data so the SVG renderer stays dumb: it
 * draws whatever is listed here and knows nothing about order management.
 *
 * Deliberately EXCLUDED from the source diagram: "SAP Master Data" and the
 * "ETL Feeds" that populate SPT/RSM/SOLR. They are not simulated by this
 * project, and drawing them would imply the mock services consume them.
 *
 * Coordinates are hand-placed in a fixed 1240x760 design space rather than
 * computed by a layout engine — the topology is static and known, and a
 * hand-tuned layout reads far better than anything auto-routing would produce
 * at this size. The renderer fits this box to the container, so the numbers
 * are a unitless design grid, not pixels on screen.
 */

/** Drives shape and colour. One accent per category, all design-system tokens. */
export type NodeCategory = "service" | "queue" | "datastore" | "external" | "ui" | "actor";

export interface ArchNode {
  id: string;
  label: string;
  category: NodeCategory;
  /** Tooltip body — what this box is and what it does in the pipeline. */
  description: string;
  /** Centre of the node in design-space coordinates. */
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ArchEdge {
  from: string;
  to: string;
  label?: string;
  /** Dashed edges are reference lookups, not the order's path through the pipeline. */
  dashed?: boolean;
  /** Arrowheads at both ends (a request/response pair drawn as one line). */
  bidirectional?: boolean;
  /**
   * Where the line leaves `from` and enters `to`. Explicit rather than derived:
   * the satellite fan-out and the Track & Trace return leg need specific sides
   * to stay orthogonal and un-crossed.
   */
  fromSide?: Side;
  toSide?: Side;
  /**
   * Elbow position along the run, 0..1 (default 0.5). Shifts the vertical leg of
   * an H-V-H connector so parallel edges don't overlap each other's labels.
   */
  bend?: number;
  /** Nudges the label off the line's midpoint when two labels would collide. */
  labelDx?: number;
  labelDy?: number;
}

export type Side = "left" | "right" | "top" | "bottom";

/** The design space the coordinates below live in; the view fits this box. */
export const DIAGRAM_WIDTH = 1620;
export const DIAGRAM_HEIGHT = 790;

const NODE_W = 128;
const NODE_H = 50;

/**
 * Layout: a faithful transcription of the project's source architecture
 * diagram (`ways-of-working/razvan/order_engine_architecture.png`), minus the
 * deliberately excluded "SAP Master Data" and "ETL Feeds" boxes.
 *
 * The source is organised around one horizontal SPINE at mid-height —
 * Inbound → RabbitMq → Order Engine → Outbound OSW → SAP Fulfilment — with
 * everything else hanging off it:
 *
 *   - BM DB directly ABOVE the engine, UI Order Engine and the login actor
 *     directly BELOW it, so the engine sits at a four-way crossing.
 *   - SPT / RSM / SOLR upper-LEFT, called back leftward from a vertical bus
 *     that rises out of the spine just right of the engine.
 *   - The six remaining satellites in a right-hand COLUMN, each reached from a
 *     single vertical bus that drops (and rises) from the spine.
 *   - SAP BTP bottom-left with Orders B2B/SF beside it; "Create Order" runs
 *     UPWARD into Inbound, and the three dashed reference lookups run right
 *     from SAP BTP and turn up into the spine.
 */
const SPINE_Y = 396;

const COL = {
  ordersSrc: 84,
  btp: 288,
  inbound: 288,
  queueIn: 452,
  engine: 660,
  /** The upper-left satellites (SPT / RSM / SOLR). */
  upperSat: 358,
  /** Label channel + vertical bus feeding the right-hand satellite column. */
  rightBus: 940,
  rightSat: 1152,
  farRight: 1360,
} as const;

export const ARCH_NODES: ArchNode[] = [
  // ── Entry, bottom-left: orders arrive and rise into Inbound ───────────────
  {
    id: "orders_src",
    label: "Orders B2B / SF",
    category: "external",
    description:
      "Incoming orders from B2B customers and Salesforce — the source of every order event.",
    // Sits below-left of SAP BTP in the source, angling up into it.
    x: COL.ordersSrc,
    y: 736,
    w: 136,
    h: 58,
  },
  {
    id: "sap_btp",
    label: "SAP BTP",
    category: "external",
    description:
      "SAP Business Technology Platform — the integration entry point where orders arrive; simulated by the injector.",
    x: COL.btp,
    y: 706,
    w: 124,
    h: 66,
  },

  // ── The spine ─────────────────────────────────────────────────────────────
  {
    id: "inbound",
    label: "Inbound",
    category: "service",
    description:
      "cc-inbound-service: receives the raw order event, transforms it and maps vendor product IDs to internal SKUs, then publishes to order.inbound.queue.",
    x: COL.inbound,
    y: SPINE_Y,
    w: NODE_W,
    h: NODE_H,
  },
  {
    id: "rmq_in",
    label: "RabbitMQ",
    category: "queue",
    description:
      "order.inbound.queue — carries the transformed order from Inbound to the Order Engine; failed deliveries dead-letter to _error.",
    x: COL.queueIn,
    y: SPINE_Y,
    w: NODE_W,
    h: NODE_H,
  },
  {
    id: "order_engine",
    label: "Order Engine",
    category: "service",
    description:
      "cc-order-engine: the central orchestrator. Creates the order (persists the cart header to BM DB, generates the order number) and enriches it by calling the satellite services.",
    x: COL.engine,
    y: SPINE_Y,
    w: 156,
    h: 56,
  },
  {
    id: "outbound",
    label: "Outbound OSW",
    category: "service",
    description: "cc-outbound-osw: submits the order to SAP fulfilment via RFC (Submit).",
    x: COL.farRight - 66,
    y: SPINE_Y,
    w: 132,
    h: 54,
  },
  // A tall bar on the far right in the source, spanning the spine.
  {
    id: "sap_ful",
    label: "SAP Fulfilment",
    category: "external",
    description: "SAP ECC fulfilment system that receives the submitted order.",
    x: COL.farRight + 76,
    y: SPINE_Y,
    w: 74,
    h: 320,
  },

  // ── Above and below the engine ────────────────────────────────────────────
  {
    id: "bm_db",
    label: "BM DB",
    category: "datastore",
    description:
      "Business-master database where the Order Engine persists the cart header at order creation.",
    x: COL.engine,
    y: 92,
    w: 104,
    h: 72,
  },
  {
    id: "ui_oe",
    label: "UI Order Engine (Angular)",
    category: "ui",
    description:
      "The Angular UI an agent uses to interact with the Order Engine (not part of the simulation).",
    x: COL.engine,
    y: 566,
    w: 140,
    h: 76,
  },
  {
    id: "oe_user",
    label: "OE Login User",
    category: "actor",
    description: "The support agent who signs into the Order Engine UI.",
    x: COL.engine,
    y: 714,
    w: 128,
    h: 64,
  },

  // ── Upper-left satellites, called back leftward ───────────────────────────
  {
    id: "spt",
    label: "SPT",
    category: "service",
    description: "cc-spt-service: pricing — returns the account's price lists (Get Prices).",
    x: COL.upperSat,
    y: 84,
    w: NODE_W,
    h: 46,
  },
  {
    id: "rsm",
    label: "RSM",
    category: "service",
    description:
      "cc-rsm-service: rebate scheme manager — computes rebates / PVC rates (Get Rebates).",
    x: COL.upperSat,
    y: 176,
    w: NODE_W,
    h: 46,
  },
  {
    id: "solr",
    label: "SOLR",
    category: "service",
    description: "cc-solr-service — product search / ID resolution during enrichment.",
    x: COL.upperSat,
    y: 286,
    w: NODE_W,
    h: 46,
  },

  // ── Right-hand satellite column ───────────────────────────────────────────
  {
    id: "settings",
    label: "Settings",
    category: "service",
    description:
      "cc-settings-service: margin thresholds and account settings, SQL-backed and pushed from Salesforce (Get Settings).",
    x: COL.rightSat,
    y: 104,
    w: NODE_W,
    h: 46,
  },
  {
    id: "checker",
    label: "Checker",
    category: "service",
    description:
      "cc-checker-service: margin check — can block the order when the line margin is below the configured threshold.",
    x: COL.rightSat,
    y: 200,
    w: NODE_W,
    h: 46,
  },
  {
    id: "validator",
    label: "Validator",
    category: "service",
    description:
      "cc-validator-service: runs validation strategies before dispatch; rejects on missing UDFs (e.g. costCenter).",
    x: COL.rightSat,
    y: 288,
    w: NODE_W,
    h: 46,
  },
  {
    id: "track_trace",
    label: "Track & Trace",
    category: "service",
    description:
      "cc-track-trace: registers the order for tracking — the success terminal event of a journey.",
    x: COL.rightSat,
    y: 490,
    w: NODE_W,
    h: 50,
  },
  {
    id: "jam",
    label: "JAM",
    category: "service",
    description:
      "cc-jam-service: user authentication and privileges; issues a JWT. A disabled account blocks the order (403).",
    x: COL.rightSat,
    y: 578,
    w: NODE_W,
    h: 46,
  },
  {
    id: "avalara",
    label: "Avalara",
    category: "service",
    description:
      "US ship-to address verification, US orders only, before dispatch.",
    x: COL.rightSat,
    y: 668,
    w: NODE_W,
    h: 46,
  },
  // Feeds Settings from the far right, top corner — as in the source.
  {
    id: "sf_settings",
    label: "Salesforce Settings",
    category: "external",
    description: "Salesforce source that pushes settings into the Settings service.",
    x: COL.farRight + 90,
    y: 100,
    w: 132,
    h: 58,
  },

  // The outbound queue sits ABOVE Outbound OSW in the source.
  {
    id: "rmq_out",
    label: "RabbitMQ",
    category: "queue",
    description:
      "order.outbound.queue — carries the validated order to Outbound OSW; failed deliveries dead-letter to _error.",
    x: COL.farRight - 66,
    y: 254,
    w: NODE_W,
    h: 50,
  },
];

export const ARCH_EDGES: ArchEdge[] = [
  // ── Entry: bottom-left, rising into the spine ─────────────────────────────
  { from: "orders_src", to: "sap_btp", fromSide: "right", toSide: "left" },
  { from: "sap_btp", to: "inbound", label: "Create Order", fromSide: "top", toSide: "bottom" },

  // ── The spine itself ──────────────────────────────────────────────────────
  { from: "inbound", to: "rmq_in", fromSide: "right", toSide: "left" },
  {
    from: "rmq_in",
    to: "order_engine",
    label: "Inbounded Order Data",
    fromSide: "right",
    toSide: "left",
    // Below the spine, as in the source — the run between these two adjacent
    // boxes is far too short to hold the text on the line itself.
    labelDx: -4,
    labelDy: 44,
  },
  { from: "order_engine", to: "outbound", label: "Submit", fromSide: "right", toSide: "left" },
  { from: "outbound", to: "sap_ful", fromSide: "right", toSide: "left" },
  { from: "rmq_out", to: "outbound", fromSide: "bottom", toSide: "top" },

  // ── Above / below the engine ──────────────────────────────────────────────
  {
    from: "order_engine",
    to: "bm_db",
    label: "Save & Receive",
    bidirectional: true,
    fromSide: "top",
    toSide: "bottom",
  },
  { from: "ui_oe", to: "order_engine", fromSide: "top", toSide: "bottom" },
  { from: "oe_user", to: "ui_oe", fromSide: "top", toSide: "bottom" },

  // ── Upper-left satellites: the engine calls back leftward into them ───────
  // Each leaves the engine's LEFT edge and enters the satellite's right edge,
  // so the arrowheads point left exactly as in the source. Leaving from the top
  // would drive them straight through BM DB, which sits directly above.
  // Distinct lanes keep the three vertical legs apart.
  {
    from: "order_engine",
    to: "spt",
    label: "Get Prices",
    fromSide: "left",
    toSide: "right",
    bend: 0.18,
  },
  {
    from: "order_engine",
    to: "rsm",
    label: "Get Rebates",
    fromSide: "left",
    toSide: "right",
    bend: 0.34,
  },
  {
    from: "order_engine",
    to: "solr",
    label: "Products Ids",
    fromSide: "left",
    toSide: "right",
    bend: 0.5,
  },

  // ── Right-hand satellite column, from a shared vertical bus ───────────────
  // All six leave the engine's right edge and enter their satellite's left
  // edge on a common lane, reproducing the source's single vertical bus with
  // horizontal spurs.
  {
    from: "order_engine",
    to: "settings",
    label: "Get Settings",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  {
    from: "order_engine",
    to: "checker",
    label: "Marging Check",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  {
    from: "order_engine",
    to: "validator",
    label: "Validate",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  {
    from: "order_engine",
    to: "track_trace",
    label: "Track Order",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  {
    from: "order_engine",
    to: "jam",
    label: "Authenticate",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  {
    from: "order_engine",
    to: "avalara",
    label: "US Address Verify",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  { from: "sf_settings", to: "settings", label: "push settings", fromSide: "left", toSide: "right" },

  // ── Dashed reference lookups: right out of SAP BTP, up into the spine ─────
  // All three share the same endpoints, so they draw as one line with the
  // three operations named against it (three near-identical curves would be
  // noise, not information).
  // They enter the engine's LEFT edge (the source turns them up into the spine
  // just short of it); a bottom entry would collide with the UI/actor stack
  // that hangs directly below the engine.
  {
    from: "sap_btp",
    to: "order_engine",
    label: "SF Case",
    dashed: true,
    fromSide: "right",
    toSide: "left",
    bend: 0.82,
    labelDy: -34,
  },
  {
    from: "sap_btp",
    to: "order_engine",
    label: "Get Opportunities",
    dashed: true,
    fromSide: "right",
    toSide: "left",
    bend: 0.82,
    labelDy: -18,
  },
  {
    from: "sap_btp",
    to: "order_engine",
    label: "Get Contracts & Internal Contracts",
    dashed: true,
    fromSide: "right",
    toSide: "left",
    bend: 0.82,
    labelDy: -2,
  },
];


/** Legend copy — the same six categories the nodes above are tagged with. */
export const CATEGORY_LEGEND: { category: NodeCategory; label: string }[] = [
  { category: "service", label: "Service" },
  { category: "queue", label: "Message queue" },
  { category: "datastore", label: "Datastore" },
  { category: "external", label: "External system" },
  { category: "ui", label: "User interface" },
  { category: "actor", label: "Person" },
];
