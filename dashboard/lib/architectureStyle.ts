/**
 * Category styling and connector geometry for the architecture diagram.
 *
 * Split from `architecture.ts` (the topology) so the "what" and the "how it
 * looks" stay separable, and so the elbow maths is unit-testable without a DOM.
 */

import type { ArchEdge, ArchNode, NodeCategory, Side } from "@/lib/architecture";

/**
 * One subtle accent per category, all from the design-system palette.
 *
 * The source sketch used saturated pink/green fills; those are not in the CC
 * palette and a large accent-coloured fill would break the "accents only on
 * small elements" rule. So category identity is carried by a tinted-but-pale
 * fill plus a coloured 1.5px stroke and the shape itself — the diagram reads as
 * blue-dominant, and colour is never the only cue (shape and the legend repeat
 * it, per WCAG 1.4.1).
 */
export interface CategoryStyle {
  /** Node fill — pale enough that the label keeps AA contrast on it. */
  fill: string;
  /** Border + legend swatch colour. */
  stroke: string;
  /** Label colour. */
  text: string;
}

export const CATEGORY_STYLE: Record<NodeCategory, CategoryStyle> = {
  // The pipeline's own services: the dominant class, so they take the primary blue.
  service: {
    fill: "var(--cc-grey-six)",
    stroke: "var(--cc-heritage-blue)",
    text: "var(--cc-foundation-blue)",
  },
  // Queues are infrastructure in motion — Horizon Blue, the accent-on-blue tone.
  queue: {
    fill: "#EAF7FF",
    stroke: "var(--cc-horizon-blue)",
    text: "#005C99",
  },
  // Persistence.
  datastore: {
    fill: "#F3EAFF",
    stroke: "var(--cc-neural-purple)",
    text: "#552496",
  },
  // Systems outside the simulation.
  external: {
    fill: "#FFF4E4",
    stroke: "var(--cc-fibre-orange)",
    text: "#B45500",
  },
  // The Angular front end.
  ui: {
    fill: "#E7F4E9",
    stroke: "var(--cc-circuit-green)",
    text: "#13681F",
  },
  // A human, not a system.
  actor: {
    fill: "var(--cc-grey-six)",
    stroke: "var(--cc-grey-two)",
    text: "var(--cc-grey-one)",
  },
};

/** The point on a node's outline where an edge attaches. */
export function anchor(node: ArchNode, side: Side): { x: number; y: number } {
  const halfW = node.w / 2;
  const halfH = node.h / 2;
  switch (side) {
    case "left":
      return { x: node.x - halfW, y: node.y };
    case "right":
      return { x: node.x + halfW, y: node.y };
    case "top":
      return { x: node.x, y: node.y - halfH };
    case "bottom":
      return { x: node.x, y: node.y + halfH };
  }
}

/** How far an arrowhead stops short of the node outline, so it doesn't touch it. */
const ARROW_GAP = 5;
/** Radius of the rounded elbow corners. */
const ELBOW_R = 10;
/** Shortest horizontal run considered wide enough to carry a label. */
const MIN_LABEL_RUN = 48;

function pull(
  point: { x: number; y: number },
  side: Side,
  distance: number
): { x: number; y: number } {
  switch (side) {
    case "left":
      return { x: point.x - distance, y: point.y };
    case "right":
      return { x: point.x + distance, y: point.y };
    case "top":
      return { x: point.x, y: point.y - distance };
    case "bottom":
      return { x: point.x, y: point.y + distance };
  }
}

/**
 * An orthogonal connector between two nodes, with rounded corners.
 *
 * The waypoint list is built from the two attachment sides (a horizontal exit
 * goes H-V-H, a vertical exit goes V-H-V, and a mixed pair takes a single
 * corner), then rendered as a path where every interior corner is an arc. Doing
 * it this way rather than emitting a bespoke path per edge is what keeps every
 * connector in the diagram consistent.
 */
export function edgePath(
  edge: ArchEdge,
  from: ArchNode,
  to: ArchNode
): { d: string; labelX: number; labelY: number } {
  const fromSide: Side = edge.fromSide ?? "right";
  const toSide: Side = edge.toSide ?? "left";
  const bend = edge.bend ?? 0.5;

  const start = anchor(from, fromSide);
  // Stop short at the destination so the arrowhead has clearance, and pull the
  // start out too when the line is bidirectional (it gets a head at both ends).
  const rawEnd = anchor(to, toSide);
  const end = pull(rawEnd, toSide, ARROW_GAP);
  const begin = edge.bidirectional ? pull(start, fromSide, ARROW_GAP) : start;

  const horizontalExit = fromSide === "left" || fromSide === "right";
  const horizontalEntry = toSide === "left" || toSide === "right";

  let points: { x: number; y: number }[];

  if (horizontalExit && horizontalEntry) {
    // H-V-H: one vertical leg, positioned by `bend` along the horizontal run.
    const midX = begin.x + (end.x - begin.x) * bend;
    points =
      begin.y === end.y
        ? [begin, end]
        : [begin, { x: midX, y: begin.y }, { x: midX, y: end.y }, end];
  } else if (!horizontalExit && !horizontalEntry) {
    // V-H-V: one horizontal leg.
    const midY = begin.y + (end.y - begin.y) * bend;
    points =
      begin.x === end.x
        ? [begin, end]
        : [begin, { x: begin.x, y: midY }, { x: end.x, y: midY }, end];
  } else if (horizontalExit) {
    // Leaves sideways, arrives vertically: a single corner under/over the target.
    points = [begin, { x: end.x, y: begin.y }, end];
  } else {
    // Leaves vertically, arrives sideways.
    points = [begin, { x: begin.x, y: end.y }, end];
  }

  // Collapse zero-length segments — they'd emit degenerate arcs.
  points = points.filter(
    (p, i) => i === 0 || Math.abs(p.x - points[i - 1].x) > 0.5 || Math.abs(p.y - points[i - 1].y) > 0.5
  );

  return { d: roundedPath(points), ...midpoint(points, edge) };
}

/** `M`/`L` through the waypoints, with each interior corner replaced by an arc. */
function roundedPath(points: { x: number; y: number }[]): string {
  if (points.length < 2) return "";
  let d = `M ${points[0].x} ${points[0].y}`;

  for (let i = 1; i < points.length - 1; i++) {
    const prev = points[i - 1];
    const corner = points[i];
    const next = points[i + 1];

    // Never round past the midpoint of either adjoining segment, or short legs
    // would produce arcs that overshoot the corner and self-intersect.
    const inLen = Math.hypot(corner.x - prev.x, corner.y - prev.y);
    const outLen = Math.hypot(next.x - corner.x, next.y - corner.y);
    const r = Math.min(ELBOW_R, inLen / 2, outLen / 2);

    if (r < 1) {
      d += ` L ${corner.x} ${corner.y}`;
      continue;
    }

    const inUnit = { x: (corner.x - prev.x) / inLen, y: (corner.y - prev.y) / inLen };
    const outUnit = { x: (next.x - corner.x) / outLen, y: (next.y - corner.y) / outLen };
    const arcStart = { x: corner.x - inUnit.x * r, y: corner.y - inUnit.y * r };
    const arcEnd = { x: corner.x + outUnit.x * r, y: corner.y + outUnit.y * r };

    // Cross product sign picks the sweep direction, so both left and right turns
    // round the correct way.
    const sweep = inUnit.x * outUnit.y - inUnit.y * outUnit.x > 0 ? 1 : 0;
    d += ` L ${arcStart.x} ${arcStart.y} A ${r} ${r} 0 0 ${sweep} ${arcEnd.x} ${arcEnd.y}`;
  }

  const last = points[points.length - 1];
  d += ` L ${last.x} ${last.y}`;
  return d;
}

/**
 * Where the label sits.
 *
 * Labels are horizontal text, so they go on the longest HORIZONTAL segment —
 * even when a vertical leg is longer. Putting them on a vertical leg is what
 * makes a fan of parallel connectors unreadable: the legs are lanes apart
 * (tens of px) but the labels are hundreds of px wide, so they overlap into
 * mush. Falling back to the longest segment of any orientation covers the
 * purely vertical connectors, which have no horizontal run to sit on.
 */
function midpoint(
  points: { x: number; y: number }[],
  edge: ArchEdge
): { labelX: number; labelY: number } {
  let best = { x: points[0].x, y: points[0].y };
  let bestLen = -1;
  let bestHorizontal: { x: number; y: number } | null = null;

  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1];
    const b = points[i];
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const len = Math.hypot(b.x - a.x, b.y - a.y);

    if (len > bestLen) {
      bestLen = len;
      best = mid;
    }
    // The LAST horizontal run long enough to hold text, not the longest one.
    // In a fan of connectors the first horizontal segment is shared ground —
    // every edge leaves its source at the same y — while the final approach to
    // the destination is unique per edge. Labelling the approach is what keeps
    // nine parallel connectors legible; labelling the longest run stacks all
    // nine labels on top of each other at the source.
    if (Math.abs(b.y - a.y) < 0.5 && len >= MIN_LABEL_RUN) {
      bestHorizontal = mid;
    }
  }

  const chosen = bestHorizontal ?? best;
  return {
    labelX: chosen.x + (edge.labelDx ?? 0),
    labelY: chosen.y + (edge.labelDy ?? 0),
  };
}
