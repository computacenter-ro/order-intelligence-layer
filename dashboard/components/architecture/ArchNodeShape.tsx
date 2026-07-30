"use client";

import { CATEGORY_STYLE } from "@/lib/architectureStyle";
import type { ArchNode } from "@/lib/architecture";

/**
 * One node of the architecture diagram.
 *
 * Shape carries category meaning alongside colour (WCAG 1.4.1 — colour is never
 * the only cue): datastores are cylinders, queues hexagons, external systems
 * soft cloud-ish lozenges, the UI a browser-chrome rectangle, a person a glyph,
 * and the pipeline's own services plain rounded rectangles. This echoes the
 * source diagram's shape language in the CC palette.
 *
 * The whole node is a focusable `<g role="button">` so tab reaches it and the
 * tooltip can be driven from focus as well as hover.
 */

interface ArchNodeShapeProps {
  node: ArchNode;
  hovered: boolean;
  /** The journey replay has already passed through this node. */
  visited?: boolean;
  /** The journey token is sitting on this node right now. */
  active?: boolean;
  /**
   * A replay is running and the order has not reached this node yet. Recedes it
   * so the travelled path stands out; never set while idle, or the static map
   * would render permanently faded.
   */
  dimmed?: boolean;
  /** Fires on both mouseenter and focus; `event` is null for focus. */
  onShow: (node: ArchNode, event: React.MouseEvent | null) => void;
  onMove: (event: React.MouseEvent) => void;
  onHide: () => void;
}

/** Split a label onto at most two lines so long names stay inside the box. */
function labelLines(label: string, maxChars: number): string[] {
  if (label.length <= maxChars) return [label];
  const words = label.split(" ");
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (current && (current + " " + word).length > maxChars) {
      lines.push(current);
      current = word;
    } else {
      current = current ? `${current} ${word}` : word;
    }
  }
  if (current) lines.push(current);
  // Two lines maximum: a third would overflow the fixed node height.
  if (lines.length > 2) return [lines[0], lines.slice(1).join(" ")];
  return lines;
}

export function ArchNodeShape({
  node,
  hovered,
  visited = false,
  active = false,
  dimmed = false,
  onShow,
  onMove,
  onHide,
}: ArchNodeShapeProps) {
  const style = CATEGORY_STYLE[node.category];
  const left = node.x - node.w / 2;
  const top = node.y - node.h / 2;
  // A node the order is sitting on outranks hover: during a replay the reader
  // is following the token, not the cursor.
  const strokeWidth = active ? 3 : hovered ? 2.5 : visited ? 2 : 1.5;

  // The tooltip is a DOM layer, so the accessible text lives here: `<title>` is
  // what a screen reader announces, and it doubles as the native title tooltip.
  const accessibleText = `${node.label}. ${node.description}`;

  let shape: React.ReactNode;

  switch (node.category) {
    case "datastore": {
      // Cylinder: a body rect plus an elliptical cap, and a faint arc for the
      // far rim so it reads as 3D rather than as a pill.
      const ry = 9;
      shape = (
        <>
          <path
            d={`M ${left} ${top + ry}
                A ${node.w / 2} ${ry} 0 0 1 ${left + node.w} ${top + ry}
                L ${left + node.w} ${top + node.h - ry}
                A ${node.w / 2} ${ry} 0 0 1 ${left} ${top + node.h - ry}
                Z`}
            fill={style.fill}
            stroke={style.stroke}
            strokeWidth={strokeWidth}
          />
          <path
            d={`M ${left} ${top + ry} A ${node.w / 2} ${ry} 0 0 0 ${left + node.w} ${top + ry}`}
            fill="none"
            stroke={style.stroke}
            strokeWidth={strokeWidth}
            opacity={0.55}
          />
        </>
      );
      break;
    }
    case "queue": {
      // Hexagon (flat top, pointed left/right).
      const notch = 16;
      shape = (
        <polygon
          points={[
            `${left + notch},${top}`,
            `${left + node.w - notch},${top}`,
            `${left + node.w},${node.y}`,
            `${left + node.w - notch},${top + node.h}`,
            `${left + notch},${top + node.h}`,
            `${left},${node.y}`,
          ].join(" ")}
          fill={style.fill}
          stroke={style.stroke}
          strokeWidth={strokeWidth}
        />
      );
      break;
    }
    case "external": {
      // A tall external is a BAR, not a cloud: the source diagram draws the
      // fulfilment system as a full-height column, and the cloud treatment
      // (rounded to half the height, with bumps) degenerates into an ellipse
      // once the box is taller than it is wide.
      if (node.h > node.w) {
        shape = (
          <rect
            x={left}
            y={top}
            width={node.w}
            height={node.h}
            rx={8}
            ry={8}
            fill={style.fill}
            stroke={style.stroke}
            strokeWidth={strokeWidth}
          />
        );
        break;
      }
      // Soft cloud-ish lozenge: fully rounded ends, plus two bumps on the top
      // edge so it silhouettes as "a system out there" rather than a pill.
      const r = node.h / 2;
      shape = (
        <>
          <rect
            x={left}
            y={top}
            width={node.w}
            height={node.h}
            rx={r}
            ry={r}
            fill={style.fill}
            stroke={style.stroke}
            strokeWidth={strokeWidth}
          />
          <path
            d={`M ${left + node.w * 0.26} ${top + 3}
                a 13 13 0 0 1 24 -2
                a 15 15 0 0 1 26 2`}
            fill={style.fill}
            stroke={style.stroke}
            strokeWidth={strokeWidth}
            strokeLinecap="round"
          />
          {/* Masks the lozenge's own top edge under the bumps so the cloud reads
              as one outline instead of two overlapping shapes. */}
          <path
            d={`M ${left + node.w * 0.26} ${top + 2} L ${left + node.w * 0.26 + 50} ${top + 2}`}
            stroke={style.fill}
            strokeWidth={strokeWidth + 1.5}
            fill="none"
          />
        </>
      );
      break;
    }
    case "ui": {
      // Browser chrome: a rect with a title bar and three dots.
      shape = (
        <>
          <rect
            x={left}
            y={top}
            width={node.w}
            height={node.h}
            rx={8}
            ry={8}
            fill={style.fill}
            stroke={style.stroke}
            strokeWidth={strokeWidth}
          />
          <path
            d={`M ${left} ${top + 14} L ${left + node.w} ${top + 14}`}
            stroke={style.stroke}
            strokeWidth={1}
            opacity={0.6}
          />
          {[0, 1, 2].map((i) => (
            <circle
              key={i}
              cx={left + 12 + i * 9}
              cy={top + 7}
              r={2.5}
              fill={style.stroke}
              opacity={0.7}
            />
          ))}
        </>
      );
      break;
    }
    case "actor": {
      // Person glyph (head + shoulders) above the label, on a plain card.
      const headY = top + 15;
      shape = (
        <>
          <rect
            x={left}
            y={top}
            width={node.w}
            height={node.h}
            rx={8}
            ry={8}
            fill={style.fill}
            stroke={style.stroke}
            strokeWidth={strokeWidth}
            strokeDasharray="5 3"
          />
          <circle cx={node.x} cy={headY} r={6} fill="none" stroke={style.stroke} strokeWidth={1.6} />
          <path
            d={`M ${node.x - 11} ${headY + 15} a 11 11 0 0 1 22 0`}
            fill="none"
            stroke={style.stroke}
            strokeWidth={1.6}
          />
        </>
      );
      break;
    }
    default:
      shape = (
        <rect
          x={left}
          y={top}
          width={node.w}
          height={node.h}
          rx={8}
          ry={8}
          fill={style.fill}
          stroke={style.stroke}
          strokeWidth={strokeWidth}
        />
      );
  }

  // The person glyph occupies the top of its card, so its label drops below it.
  const labelBaseY = node.category === "actor" ? node.y + 18 : node.y;
  // Budget characters by the box's actual width (~6.6px per char at 13px
  // semibold) rather than a fixed pair of buckets, so a narrow tall bar wraps
  // instead of spilling its label out both sides.
  const lines = labelLines(node.label, Math.max(6, Math.floor((node.w - 12) / 6.6)));

  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={accessibleText}
      className="oil-arch-node"
      onMouseEnter={(e) => onShow(node, e)}
      onMouseMove={onMove}
      onMouseLeave={onHide}
      onFocus={() => onShow(node, null)}
      onBlur={onHide}
      style={{ cursor: "pointer", opacity: dimmed ? 0.42 : 1 }}
    >
      <title>{accessibleText}</title>

      {/* Hover/focus lift: a soft halo behind the shape rather than a filter
          (an SVG drop-shadow filter on every node is expensive to re-rasterize
          while panning). */}
      {hovered && !active && (
        <rect
          x={left - 6}
          y={top - 6}
          width={node.w + 12}
          height={node.h + 12}
          rx={12}
          ry={12}
          fill="none"
          stroke={style.stroke}
          strokeWidth={1.5}
          opacity={0.35}
        />
      )}

      {/* The order is here right now: a Heritage Blue ring, the same colour as
          the token, so the eye connects the two. */}
      {active && (
        <rect
          x={left - 7}
          y={top - 7}
          width={node.w + 14}
          height={node.h + 14}
          rx={13}
          ry={13}
          fill="none"
          stroke="var(--cc-heritage-blue)"
          strokeWidth={2.5}
          opacity={0.75}
        />
      )}

      {shape}

      <text
        x={node.x}
        y={labelBaseY}
        textAnchor="middle"
        dominantBaseline="middle"
        fontSize={13}
        fontWeight={600}
        fill={style.text}
        // Labels must never eat their own pointer events, or moving across a
        // label inside a node would fire mouseleave and flicker the tooltip.
        style={{ pointerEvents: "none", userSelect: "none" }}
      >
        {lines.map((line, i) => (
          <tspan key={i} x={node.x} dy={i === 0 ? (lines.length > 1 ? -8 : 0) : 16}>
            {line}
          </tspan>
        ))}
      </text>
    </g>
  );
}
