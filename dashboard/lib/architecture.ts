/**
 * The simulated order pipeline, as a hand-authored graph.
 *
 * This is the project's architecture diagram (CLAUDE.md, "The simulated
 * production system") expressed as data so the SVG renderer stays dumb: it
 * draws whatever is listed here and knows nothing about order management.
 *
 * The topology is the FIVE-HOP ping-pong between Inbound and the Order
 * Engine (per the Computacenter service docs the realignment spec cites):
 *
 *   1  Inbound ── order.init ──► Order Engine (first turn: defaults only)
 *   2  Order Engine ── order_data_ready ──► Inbound
 *   3  Inbound ── order.approval ──► Order Engine (second turn: CREATE)
 *   4  Order Engine ── order.create.sap ──► Outbound OSW ─► SAP Fulfilment
 *   5  Order Engine ── order_created ──► Inbound   (closes the loop)
 *
 * Inbound owns the PRE-creation satellites (Settings → JAM → SOLR; SOLR's
 * ownership is inferred, not documented — see shared/scenarios.py). The Order
 * Engine owns the POST-creation ones: Track & Trace right after creation,
 * then SPT → RSM → Validator → Avalara (US) → Checker.
 *
 * The Inbound↔Engine hops all ride the same control queues, so they are drawn
 * as the two BIDIRECTIONAL spine edges through RabbitMQ with the routing keys
 * as labels, rather than as duplicate parallel return lines — the connector
 * geometry anchors both directions to the same two points, so a literal
 * second edge would draw exactly on top of the first. The journey replay
 * still walks the return legs visibly (it traverses these edges in reverse).
 *
 * Deliberately EXCLUDED from the source diagram: "SAP Master Data" and the
 * "ETL Feeds" that populate SPT/RSM/SOLR. They are not simulated by this
 * project, and drawing them would imply the mock services consume them.
 *
 * Coordinates are hand-placed in a fixed design space rather than computed by
 * a layout engine — the topology is static and known, and a hand-tuned layout
 * reads far better than anything auto-routing would produce at this size. The
 * renderer fits this box to the container, so the numbers are a unitless
 * design grid, not pixels on screen. The grid's reading: the pre-creation
 * world (entry + Inbound's satellites) clusters on the LEFT, creation and the
 * post-creation checks sit centre/RIGHT.
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
   * the satellite fan-outs need specific sides to stay orthogonal and
   * un-crossed.
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
 * Layout: one horizontal SPINE at mid-height — Inbound → RabbitMQ → Order
 * Engine → RabbitMQ → Outbound OSW → SAP Fulfilment — with everything else
 * hanging off it:
 *
 *   - INBOUND's satellites (Settings / JAM / SOLR) in a column BELOW-LEFT of
 *     it, top-to-bottom in call order, reached leftward from Inbound's left
 *     edge (the same fan grammar the engine uses for its own satellites).
 *     Salesforce Settings sits above that column, pushing into Settings.
 *   - BM DB directly ABOVE the engine, UI Order Engine and the login actor
 *     directly BELOW it, so the engine sits at a four-way crossing.
 *   - SPT / RSM upper-LEFT of the engine, called back leftward.
 *   - The engine's four remaining satellites in a right-hand COLUMN —
 *     Validator, Avalara, Checker in the documented auto-approval order, then
 *     Track & Trace — each reached from a single vertical bus off the spine.
 *   - SAP BTP bottom-left with Orders B2B/SF beside it; "Create Order" runs
 *     UPWARD into Inbound, and the three dashed reference lookups run right
 *     from SAP BTP and turn up into the spine.
 */
const SPINE_Y = 396;

const COL = {
  ordersSrc: 84,
  btp: 288,
  inbound: 288,
  /** Inbound's satellite column (pre-creation leg), below-left of Inbound. */
  inboundSat: 84,
  queueIn: 452,
  engine: 660,
  /** The upper-left satellites (SPT / RSM). */
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
      "Where orders come from — B2B customer channels and Salesforce. Every order in the pipeline starts here.",
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
      "SAP's integration layer — the bridge between the external order sources and the internal order pipeline.",
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
      "The pipeline's front door — receives incoming orders, prepares them, and hands them to the Order Engine.",
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
      "Message broker carrying the back-and-forth between Inbound and the Order Engine — orders travel through the pipeline as queue messages, not direct calls.",
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
      "The heart of the pipeline — creates the order, runs the checks, and decides whether it can go to SAP.",
    x: COL.engine,
    y: SPINE_Y,
    w: 156,
    h: 56,
  },
  {
    id: "outbound",
    label: "Outbound OSW",
    category: "service",
    description: "Sends completed orders on to SAP for fulfilment.",
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
    description: "Where the order goes once the pipeline is done with it — SAP takes over delivery and invoicing.",
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
      "The order database — the Order Engine writes each order here at creation, and the order's identifiers exist from this point on.",
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
      "The screen agents use to view, edit, approve or reject orders in the Order Engine. Not part of this simulation.",
    x: COL.engine,
    y: 566,
    w: 140,
    h: 76,
  },
  {
    id: "oe_user",
    label: "OE Login User",
    category: "actor",
    description: "The person behind the screen — a Computacenter agent working orders through the UI.",
    x: COL.engine,
    y: 714,
    w: 128,
    h: 64,
  },

  // ── Inbound's pre-creation satellites, below-left in call order ───────────
  {
    id: "sf_settings",
    label: "Salesforce Settings",
    category: "external",
    description: "Where customer configuration is maintained — pushed from Salesforce into the Settings service the pipeline reads.",
    x: COL.inboundSat,
    y: 356,
    w: 132,
    h: 58,
  },
  {
    id: "settings",
    label: "Settings",
    category: "service",
    description:
      "Serves per-customer configuration — account settings and business thresholds the pipeline consults before an order is created.",
    x: COL.inboundSat,
    y: 470,
    w: NODE_W,
    h: 46,
  },
  {
    id: "jam",
    label: "JAM",
    category: "service",
    description:
      "The authentication and privileges service — confirms the ordering user's identity and rights; a blocked account stops the order before it's created.",
    x: COL.inboundSat,
    y: 550,
    w: NODE_W,
    h: 46,
  },
  {
    id: "solr",
    label: "SOLR",
    category: "service",
    description:
      "The product-search service — finds the catalogue item behind each order line so the order references real products.",
    x: COL.inboundSat,
    y: 630,
    w: NODE_W,
    h: 46,
  },

  // ── The engine's upper-left satellites ────────────────────────────────────
  {
    id: "spt",
    label: "SPT",
    category: "service",
    description:
      "The pricing service — supplies the account's price lists so the order's lines get their costs.",
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
      "Provides rebate and PVC information.",
    x: COL.upperSat,
    y: 176,
    w: NODE_W,
    h: 46,
  },

  // ── Right-hand satellite column (documented auto-approval order) ──────────
  {
    id: "validator",
    label: "Validator",
    category: "service",
    description:
      "Runs the business validation rules — required fields, addresses, quantities, dates — and reports anything wrong with the order.",
    x: COL.rightSat,
    y: 104,
    w: NODE_W,
    h: 46,
  },
  {
    id: "avalara",
    label: "Avalara",
    category: "service",
    description:
      "Verifies US shipping addresses (US orders only).",
    x: COL.rightSat,
    y: 200,
    w: NODE_W,
    h: 46,
  },
  {
    id: "checker",
    label: "Checker",
    category: "service",
    description:
      "The margin check — verifies the order meets its margin rules; an order below threshold is held for a person to review.",
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
      "Registers the newly created order for tracking so its progress is visible to the customer from this point on.",
    x: COL.rightSat,
    y: 490,
    w: NODE_W,
    h: 50,
  },

  // ON the spine between the engine and Outbound OSW — the same grammar as
  // rmq_in: the order's path runs THROUGH the queue, never past it. (The
  // source sketch parks this queue above Outbound OSW, but an off-path queue
  // reads as optional, and routing the publish leg up to it would either
  // cross the right-hand satellite bus or overlap the consume leg.)
  {
    id: "rmq_out",
    label: "RabbitMQ",
    category: "queue",
    description:
      "Message queue on the way out — approved orders wait here until Outbound OSW picks them up for SAP.",
    // Midpoint of the gap between the engine's right edge and Outbound's left.
    x: 983,
    y: SPINE_Y,
    w: NODE_W,
    h: 50,
  },
];

export const ARCH_EDGES: ArchEdge[] = [
  // ── Entry: bottom-left, rising into the spine ─────────────────────────────
  { from: "orders_src", to: "sap_btp", fromSide: "right", toSide: "left" },
  { from: "sap_btp", to: "inbound", label: "Create Order", fromSide: "top", toSide: "bottom" },

  // ── The spine: the five hops' request/response pairs ──────────────────────
  // Both spine edges are bidirectional: order.init and order.approval travel
  // rightward, order_data_ready and order_created travel leftward — the same
  // queues carry all four, so one line per segment with heads at both ends is
  // the honest drawing (see the header comment).
  {
    from: "inbound",
    to: "rmq_in",
    label: "order.init · order.approval",
    bidirectional: true,
    fromSide: "right",
    toSide: "left",
    // ABOVE the spine. The run here is only ~36px (Inbound's right edge to
    // RabbitMQ's left edge) but the label is ~150px, so centred on the line it
    // spills into both boxes — and edges paint UNDER nodes, so it vanished
    // entirely. Its sibling edge drops below the spine (labelDy: 44); this one
    // goes up so the two adjacent labels don't land on top of each other.
    labelDy: -30,
  },
  {
    from: "rmq_in",
    to: "order_engine",
    label: "order_data_ready · order_created",
    bidirectional: true,
    fromSide: "right",
    toSide: "left",
    // Below the spine, as in the source — the run between these two adjacent
    // boxes is far too short to hold the text on the line itself.
    labelDx: -4,
    labelDy: 44,
  },
  // Hop 4 rides a queue like every other hop: the engine PUBLISHES
  // order.create.sap — its responsibility ends there (order-engine.md §9.3,
  // §9 row 10) — and Outbound OSW consumes it. A direct engine → outbound
  // edge would assert a synchronous handoff that does not exist.
  {
    from: "order_engine",
    to: "rmq_out",
    label: "order.create.sap",
    fromSide: "right",
    toSide: "left",
    // Above the line: the run's midpoint sits on the satellite bus's elbows
    // at x≈814, and the label halo would knock them out of the drawing.
    labelDy: -26,
  },
  { from: "rmq_out", to: "outbound", fromSide: "right", toSide: "left" },
  { from: "outbound", to: "sap_ful", fromSide: "right", toSide: "left" },

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

  // ── Inbound's satellites: called back leftward, downward fan ──────────────
  // Same fan grammar as the engine's: leave the caller's LEFT edge, enter the
  // satellite's right edge. Lane assignment avoids crossings: the DEEPEST
  // target takes the lane closest to Inbound (largest x), so no approach run
  // ever crosses a longer lane.
  {
    from: "inbound",
    to: "settings",
    label: "Get Settings",
    fromSide: "left",
    toSide: "right",
    bend: 0.5,
  },
  {
    from: "inbound",
    to: "jam",
    label: "Authenticate",
    fromSide: "left",
    toSide: "right",
    bend: 0.34,
  },
  {
    from: "inbound",
    to: "solr",
    label: "Products Ids",
    fromSide: "left",
    toSide: "right",
    bend: 0.18,
  },
  { from: "sf_settings", to: "settings", label: "push settings", fromSide: "bottom", toSide: "top" },

  // ── The engine's upper-left satellites, called back leftward ──────────────
  // Each leaves the engine's LEFT edge and enters the satellite's right edge,
  // so the arrowheads point left exactly as in the source. Leaving from the top
  // would drive them straight through BM DB, which sits directly above.
  // Distinct lanes keep the vertical legs apart.
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

  // ── Right-hand satellite column, from a shared vertical bus ───────────────
  // All four leave the engine's right edge and enter their satellite's left
  // edge on a common lane, reproducing the source's single vertical bus with
  // horizontal spurs. Top-to-bottom: the documented auto-approval order
  // (Validator → Avalara → Checker), then Track & Trace.
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
    to: "avalara",
    label: "US Address Verify",
    fromSide: "right",
    toSide: "left",
    bend: 0.22,
  },
  {
    from: "order_engine",
    to: "checker",
    label: "Margin Check",
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
