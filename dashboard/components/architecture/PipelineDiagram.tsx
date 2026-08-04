"use client";

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowCounterClockwiseIcon,
  ArrowsOutIcon,
  MagnifyingGlassMinusIcon,
  MagnifyingGlassPlusIcon,
  PauseIcon,
  PlayIcon,
} from "@phosphor-icons/react";
import {
  ARCH_EDGES,
  ARCH_NODES,
  CATEGORY_LEGEND,
  DIAGRAM_HEIGHT,
  DIAGRAM_WIDTH,
  type ArchNode,
} from "@/lib/architecture";
import { CATEGORY_STYLE, edgePath } from "@/lib/architectureStyle";
import { ArchNodeShape } from "@/components/architecture/ArchNodeShape";
import { JOURNEY_STEPS } from "@/lib/architectureJourney";
import {
  MAX_SPEED,
  MIN_SPEED,
  useJourneyPlayback,
} from "@/components/architecture/useJourneyPlayback";
import { JourneyStatusBar } from "@/components/architecture/JourneyStatusBar";

/**
 * The interactive pipeline map: a hand-authored SVG of the simulated order
 * pipeline with zoom, pan, and a cursor-following tooltip.
 *
 * Pan/zoom is a plain `translate(tx,ty) scale(k)` transform on a wrapping `<g>`,
 * driven by pointer events — no pan/zoom library, and none is in package.json.
 * Wheel zooms toward the cursor by solving for the translation that keeps the
 * design-space point under the pointer fixed; drag pans; "Fit" recomputes the
 * scale that fits the whole design box in the container.
 *
 * The diagram is static data (`lib/architecture.ts`) — no backend calls, no
 * persistence.
 */

/**
 * Zoom bounds. The floor is below the nominal 0.4 because "Fit" is clamped by
 * it too: in a short viewport the fit scale for the 1420x860 design box lands
 * under 0.4, and clamping there would make the "fit" view overflow its own
 * container — the one thing fit must never do.
 */
const MIN_SCALE = 0.25;
const MAX_SCALE = 3.0;
/** Padding kept around the diagram when fitting, in container pixels. */
const FIT_PADDING = 24;
/**
 * Extra bottom inset so a fitted diagram clears the legend, which is an overlay
 * pinned inside the same box. Without it the bottom row of nodes sits under it.
 */
const FIT_BOTTOM_INSET = 52;
/** Offset of the tooltip from the pointer, and the gap it keeps from the edges. */
const TOOLTIP_OFFSET = 14;
const TOOLTIP_MARGIN = 12;
const TOOLTIP_WIDTH = 300;

interface Transform {
  k: number;
  tx: number;
  ty: number;
}

interface Size {
  w: number;
  h: number;
}

interface TooltipState {
  node: ArchNode;
  /** Container-relative pointer position; null when shown via keyboard focus. */
  x: number | null;
  y: number | null;
}

const NODE_BY_ID = new Map(ARCH_NODES.map((n) => [n.id, n]));

/**
 * A detached <path>, created once and reused, used purely as a measuring
 * instrument for `pointAlongPath`. Kept at module scope rather than in a ref
 * because it is not React state — nothing renders it and nothing re-creates it.
 */
let measurePath: SVGPathElement | null = null;

/**
 * The point `t` (0..1) of the way along an SVG path's real geometry.
 *
 * The connectors are orthogonal with rounded elbows, so interpolating between
 * the two endpoints would cut every corner and leave the token floating off the
 * drawn line. `getPointAtLength` follows the actual curve. Returns null during
 * SSR, where there is no DOM to measure with.
 */
function pointAlongPath(d: string, t: number): { x: number; y: number } | null {
  if (typeof document === "undefined") return null;
  if (!measurePath) {
    measurePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  }
  measurePath.setAttribute("d", d);
  const pt = measurePath.getPointAtLength(measurePath.getTotalLength() * t);
  return { x: pt.x, y: pt.y };
}

/** Scale + offset that centres the whole design box in a container of this size. */
function fitTransform(size: Size): Transform {
  if (size.w === 0 || size.h === 0) return { k: 1, tx: 0, ty: 0 };
  const usableW = size.w - FIT_PADDING * 2;
  const usableH = size.h - FIT_PADDING * 2 - FIT_BOTTOM_INSET;
  const k = Math.min(usableW / DIAGRAM_WIDTH, usableH / DIAGRAM_HEIGHT);
  // Clamped like every other scale so a very small container can't produce a
  // transform the zoom controls then refuse to move away from.
  const clamped = Math.max(MIN_SCALE, Math.min(MAX_SCALE, k));
  return {
    k: clamped,
    tx: (size.w - DIAGRAM_WIDTH * clamped) / 2,
    // Centred within the usable band, so the legend inset comes off the bottom
    // rather than pushing the diagram off the top.
    ty: FIT_PADDING + (usableH - DIAGRAM_HEIGHT * clamped) / 2,
  };
}

export function PipelineDiagram() {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<TooltipState | null>(null);
  const [dragging, setDragging] = useState(false);

  /**
   * Container and tooltip dimensions are STATE, not measurements read off refs
   * during render. Both feed the transform and the tooltip's edge-flip, i.e.
   * they are render inputs — and a ref read during render doesn't re-render when
   * it changes, so the layout would silently go stale on resize. The
   * ResizeObserver below is the "subscribe to an external system" path that
   * keeps them current.
   */
  const [size, setSize] = useState<Size>({ w: 0, h: 0 });
  const [tooltipSize, setTooltipSize] = useState<Size>({ w: TOOLTIP_WIDTH, h: 96 });

  /**
   * The pan/zoom transform, or null for "fit to the container".
   *
   * Null rather than an eagerly computed transform so the fit is DERIVED from
   * the current size: the diagram is correctly fitted on the very first paint
   * that has a measured size, and stays fitted across resizes, without an effect
   * writing state on mount. Any zoom or pan replaces it with a concrete
   * transform; "Fit" sets it back to null.
   */
  const [manual, setManual] = useState<Transform | null>(null);
  const transform = manual ?? fitTransform(size);

  // Pan bookkeeping. Kept in a ref, not state: it updates on every pointermove
  // and re-rendering the whole SVG for it would drop frames.
  const panStart = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);

  const fit = useCallback(() => setManual(null), []);

  // Track the container's size. useLayoutEffect + an immediate first read so the
  // measured size lands before the browser paints — the diagram never flashes
  // unfitted and then jumps.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () =>
      setSize((prev) => {
        const next = { w: el.clientWidth, h: el.clientHeight };
        return prev.w === next.w && prev.h === next.h ? prev : next;
      });
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // Measure the tooltip so the edge-flip uses its real height (descriptions vary
  // in length, so a fixed guess would flip too early or too late).
  useLayoutEffect(() => {
    const el = tooltipRef.current;
    if (!tooltip || !el) return;
    const observer = new ResizeObserver(() =>
      setTooltipSize((prev) =>
        prev.w === el.offsetWidth && prev.h === el.offsetHeight
          ? prev
          : { w: el.offsetWidth, h: el.offsetHeight }
      )
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [tooltip]);

  /**
   * Zoom about a container-relative point, keeping that point stationary.
   *
   * Takes the pre-zoom transform from the state updater's `prev` (falling back
   * to the derived fit, since `null` means "currently fitted") so a rapid wheel
   * burst composes correctly instead of each event zooming from a stale base.
   */
  const zoomAbout = useCallback(
    (factor: number, px: number, py: number) => {
      setManual((prev) => {
        const base = prev ?? fitTransform(size);
        const k = Math.max(MIN_SCALE, Math.min(MAX_SCALE, base.k * factor));
        if (k === base.k) return base;
        // The design-space point under (px,py) must map to (px,py) again.
        const ratio = k / base.k;
        return { k, tx: px - (px - base.tx) * ratio, ty: py - (py - base.ty) * ratio };
      });
    },
    [size]
  );

  /** Zoom from the buttons: about the container centre, since there's no cursor. */
  const zoomCentre = useCallback(
    (factor: number) => zoomAbout(factor, size.w / 2, size.h / 2),
    [zoomAbout, size]
  );

  // Wheel zoom, registered natively rather than via onWheel: React attaches
  // wheel listeners as passive, so preventDefault() there is ignored and the
  // page would scroll behind the diagram.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      // Trackpad pinch arrives as a wheel event with ctrlKey set; a slightly
      // stronger factor there matches the gesture's expected sensitivity.
      const intensity = e.ctrlKey ? 0.01 : 0.0016;
      const factor = Math.exp(-e.deltaY * intensity);
      zoomAbout(factor, e.clientX - rect.left, e.clientY - rect.top);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomAbout]);

  const onPointerDown = (e: React.PointerEvent) => {
    // Left button only — right/middle drags belong to the browser.
    if (e.button !== 0) return;
    panStart.current = { x: e.clientX, y: e.clientY, tx: transform.tx, ty: transform.ty };
    setDragging(true);
    // Capture so a drag that leaves the container still pans and still ends.
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const start = panStart.current;
    if (!start) return;
    const dx = e.clientX - start.x;
    const dy = e.clientY - start.y;
    setManual((prev) => {
      const base = prev ?? fitTransform(size);
      return { ...base, tx: start.tx + dx, ty: start.ty + dy };
    });
  };

  const endPan = (e: React.PointerEvent) => {
    if (!panStart.current) return;
    panStart.current = null;
    setDragging(false);
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
  };

  /** Container-relative pointer position for the tooltip layer. */
  const localPoint = (e: React.MouseEvent) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };

  const showTooltip = useCallback((node: ArchNode, e: React.MouseEvent | null) => {
    // Suppressed mid-drag: the pointer is panning, not inspecting, and a tooltip
    // chasing the cursor across the canvas is pure noise.
    if (panStart.current) return;
    if (!e) {
      // Keyboard focus — anchor to the node itself, not to a stale cursor.
      setTooltip({ node, x: null, y: null });
      return;
    }
    const { x, y } = localPoint(e);
    setTooltip({ node, x, y });
  }, []);

  const moveTooltip = useCallback((e: React.MouseEvent) => {
    if (panStart.current) return;
    const { x, y } = localPoint(e);
    setTooltip((prev) => (prev ? { ...prev, x, y } : prev));
  }, []);

  const hideTooltip = useCallback(() => setTooltip(null), []);

  // ── Journey replay ────────────────────────────────────────────────────────
  // The motion preference is read at Play time, not held in state: it is an
  // input to starting a journey, never something the idle map renders
  // differently, so mirroring it into React would be a copy with no reader.
  const {
    state: playback,
    speed,
    play,
    pause,
    reset,
    setSpeed,
  } = useJourneyPlayback();
  const currentStep = playback.stepIndex >= 0 ? JOURNEY_STEPS[playback.stepIndex] : null;
  /**
   * Playing, paused mid-way, OR holding the finished state — i.e. the map is in
   * replay mode and should dim untouched nodes / hide the legend. A paused
   * journey still counts: freezing the token does not put the map back to its
   * idle reading.
   */
  const journeyRunning = playback.playing || playback.paused || playback.finished;
  /** Anything to reset — a journey is under way, paused, or finished. */
  const journeyStarted = journeyRunning || playback.stepIndex >= 0;

  /**
   * Every journey hop's geometry, precomputed.
   *
   * Each hop names an edge by its endpoints; this resolves that to the same
   * `edgePath` the diagram already draws, so the token travels the visible
   * line rather than a straight line of its own. Memoised because the paths
   * are static — recomputing 25 of them per animation frame would be waste.
   */
  const hopPaths = useMemo(
    () =>
      JOURNEY_STEPS.map((step) => {
        const edge = ARCH_EDGES.find(
          (e) => e.from === step.from && e.to === step.to && !e.dashed
        );
        const from = NODE_BY_ID.get(step.from);
        const to = NODE_BY_ID.get(step.to);
        if (!edge || !from || !to) return null;
        return edgePath(edge, from, to).d;
      }),
    []
  );

  /**
   * Where the token is right now — DERIVED during render, not stored in state.
   *
   * It is a pure function of (step, progress), both of which already live in
   * `playback`, so mirroring it into its own state would just be a second copy
   * updated one render late.
   */
  const activeHopPath = playback.stepIndex >= 0 ? hopPaths[playback.stepIndex] : null;
  const tokenPos =
    // `paused` as well as `playing`: pausing must FREEZE the token in place, and
    // gating on `playing` alone would make it disappear instead.
    activeHopPath && (playback.playing || playback.paused)
      ? // A reversed hop is the same drawn line walked from the far end — that
        // is how the bridge ack goes visibly BACKWARDS to Inbound without the
        // diagram needing a duplicate return edge.
        pointAlongPath(
          activeHopPath,
          currentStep?.reverse ? 1 - playback.progress : playback.progress
        )
      : null;

  // Tooltip placement: cursor + offset, flipped to the other side when it would
  // overflow the container, then clamped so it can never leave it entirely.
  let tipLeft = 0;
  let tipTop = 0;
  if (tooltip) {
    const cw = size.w;
    const ch = size.h;

    if (tooltip.x === null || tooltip.y === null) {
      // Focus mode: place it under the node, in container coordinates.
      const nx = tooltip.node.x * transform.k + transform.tx;
      const ny = (tooltip.node.y + tooltip.node.h / 2) * transform.k + transform.ty;
      tipLeft = nx - tooltipSize.w / 2;
      tipTop = ny + TOOLTIP_OFFSET;
    } else {
      tipLeft = tooltip.x + TOOLTIP_OFFSET;
      tipTop = tooltip.y + TOOLTIP_OFFSET;
      if (tipLeft + tooltipSize.w > cw - TOOLTIP_MARGIN) {
        tipLeft = tooltip.x - TOOLTIP_OFFSET - tooltipSize.w;
      }
      if (tipTop + tooltipSize.h > ch - TOOLTIP_MARGIN) {
        tipTop = tooltip.y - TOOLTIP_OFFSET - tooltipSize.h;
      }
    }
    tipLeft = Math.max(TOOLTIP_MARGIN, Math.min(tipLeft, cw - tooltipSize.w - TOOLTIP_MARGIN));
    tipTop = Math.max(TOOLTIP_MARGIN, Math.min(tipTop, ch - tooltipSize.h - TOOLTIP_MARGIN));
  }

  return (
    <div
      ref={containerRef}
      style={{
        position: "relative",
        width: "100%",
        // Tall enough that the wide design box fits at a legible scale, capped
        // so it never outgrows the viewport and hides its own zoom controls.
        height: "clamp(520px, calc(100vh - 232px), 1000px)",
        background: "var(--cc-cloud-white)",
        border: "1px solid var(--cc-grey-six)",
        borderRadius: "8px",
        boxShadow: "var(--cc-shadow-sm)",
        overflow: "hidden",
        // Own the gesture so a two-finger pan doesn't scroll the page instead.
        touchAction: "none",
        cursor: dragging ? "grabbing" : "grab",
      }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endPan}
      onPointerCancel={endPan}
    >
      <svg
        width="100%"
        height="100%"
        role="img"
        aria-label="Architecture diagram of the simulated order pipeline. Orders arrive from B2B and Salesforce through SAP BTP into the Inbound service, which calls Settings, JAM and SOLR before the order exists, then ping-pongs with the Order Engine over RabbitMQ: order init, order data ready, order approval. The Order Engine persists to the BM database, registers Track and Trace, calls SPT, RSM, Validator, Avalara and Checker, publishes order create sap to RabbitMQ for Outbound OSW to submit to SAP Fulfilment, and answers order created back to Inbound, where the flow ends."
      >
        <defs>
          {/* One marker per stroke colour: SVG markers don't inherit the path's
              stroke, so a single shared arrowhead would be the wrong colour on
              the dashed reference lines. */}
          <marker
            id="arch-arrow"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--cc-grey-two)" />
          </marker>
          <marker
            id="arch-arrow-muted"
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--cc-grey-four)" />
          </marker>
        </defs>

        <g transform={`translate(${transform.tx} ${transform.ty}) scale(${transform.k})`}>
          {/* Edges first so nodes always paint over them. */}
          {ARCH_EDGES.map((edge, i) => {
            const from = NODE_BY_ID.get(edge.from);
            const to = NODE_BY_ID.get(edge.to);
            if (!from || !to) return null;
            const { d, labelX, labelY } = edgePath(edge, from, to);
            const stroke = edge.dashed ? "var(--cc-grey-four)" : "var(--cc-grey-two)";
            const marker = edge.dashed ? "url(#arch-arrow-muted)" : "url(#arch-arrow)";
            return (
              <g key={`${edge.from}-${edge.to}-${i}`}>
                <path
                  d={d}
                  fill="none"
                  stroke={stroke}
                  strokeWidth={edge.dashed ? 1.2 : 1.6}
                  strokeDasharray={edge.dashed ? "6 5" : undefined}
                  markerEnd={marker}
                  markerStart={edge.bidirectional ? marker : undefined}
                />
                {edge.label && (
                  <text
                    x={labelX}
                    y={labelY - 6}
                    textAnchor="middle"
                    fontSize={11}
                    fontWeight={500}
                    fill={edge.dashed ? "var(--cc-grey-three)" : "var(--cc-grey-two)"}
                    style={{ pointerEvents: "none", userSelect: "none" }}
                  >
                    {/* Painted twice: a Cloud White halo underneath knocks the
                        connector out from behind the text so the label stays
                        legible where it crosses a line. */}
                    <tspan
                      stroke="var(--cc-cloud-white)"
                      strokeWidth={4}
                      strokeLinejoin="round"
                      paintOrder="stroke"
                    >
                      {edge.label}
                    </tspan>
                  </text>
                )}
              </g>
            );
          })}

          {/* The hop currently being travelled, traced over the base connector
              so the active leg reads as lit rather than merely having a dot on
              it. Drawn between edges and nodes: above the lines, under the
              boxes. */}
          {(playback.playing || playback.paused) &&
            playback.stepIndex >= 0 &&
            hopPaths[playback.stepIndex] && (
              <path
                d={hopPaths[playback.stepIndex] as string}
                fill="none"
                stroke="var(--cc-heritage-blue)"
                strokeWidth={2.6}
                strokeLinecap="round"
                opacity={0.5}
                style={{ pointerEvents: "none" }}
              />
            )}

          {ARCH_NODES.map((node) => (
            <ArchNodeShape
              key={node.id}
              node={node}
              hovered={tooltip?.node.id === node.id}
              visited={playback.visited.has(node.id)}
              active={playback.activeNode === node.id}
              dimmed={journeyRunning && !playback.visited.has(node.id)}
              onShow={showTooltip}
              onMove={moveTooltip}
              onHide={hideTooltip}
            />
          ))}

          {/* The travelling order. A halo plus a solid core so it stays visible
              over both the pale node fills and the Cloud White background. */}
          {tokenPos && (
            <g style={{ pointerEvents: "none" }}>
              <circle
                cx={tokenPos.x}
                cy={tokenPos.y}
                r={11}
                fill="var(--cc-heritage-blue)"
                opacity={0.22}
              />
              <circle
                cx={tokenPos.x}
                cy={tokenPos.y}
                r={5.5}
                fill="var(--cc-heritage-blue)"
                stroke="var(--cc-cloud-white)"
                strokeWidth={1.5}
              />
            </g>
          )}
        </g>
      </svg>

      {/* Zoom controls — real buttons, top-right, above the canvas. */}
      <div
        style={{
          position: "absolute",
          top: "12px",
          right: "12px",
          display: "flex",
          flexDirection: "column",
          gap: "4px",
        }}
        // The controls sit inside the pan surface, so their own pointerdown must
        // not also start a drag.
        onPointerDown={(e) => e.stopPropagation()}
      >
        <ZoomButton label="Zoom in" onClick={() => zoomCentre(1.25)}>
          <MagnifyingGlassPlusIcon size={20} />
        </ZoomButton>
        <ZoomButton label="Zoom out" onClick={() => zoomCentre(1 / 1.25)}>
          <MagnifyingGlassMinusIcon size={20} />
        </ZoomButton>
        <ZoomButton label="Reset zoom and fit the diagram to view" onClick={fit}>
          <ArrowsOutIcon size={20} />
        </ZoomButton>
      </div>

      {/* Transport controls — top-left, opposite the zoom stack. Play/Pause and
          Reset on one row, the speed slider beneath them. */}
      <div
        style={{
          position: "absolute",
          top: "12px",
          left: "12px",
          display: "flex",
          flexDirection: "column",
          gap: "8px",
          alignItems: "flex-start",
        }}
        // The controls sit inside the pan surface, so their own pointerdown must
        // not also start a drag — this is what lets the slider thumb be dragged
        // without panning the diagram underneath it.
        onPointerDown={(e) => e.stopPropagation()}
      >
        <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
          <button
            type="button"
            // Playing -> pause. Paused -> resume. Idle or finished -> start over.
            onClick={playback.playing ? pause : play}
            className="oil-journey-button"
            aria-label={
              playback.playing
                ? "Pause the order journey replay"
                : playback.paused
                  ? "Resume the order journey replay"
                  : "Play an order journey through the pipeline"
            }
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              height: "32px",
              padding: "0 16px",
              // Hollow while playing so "pause" never looks like the page's
              // primary call to action; filled whenever pressing it starts
              // motion (idle, paused, or replaying a finished journey).
              background: playback.playing
                ? "var(--cc-cloud-white)"
                : "var(--cc-heritage-blue)",
              border: playback.playing ? "1px solid var(--cc-grey-five)" : "none",
              borderRadius: "8px",
              color: playback.playing
                ? "var(--cc-heritage-blue)"
                : "var(--cc-cloud-white)",
              fontSize: "14px",
              fontWeight: 600,
              lineHeight: "20px",
              cursor: "pointer",
            }}
          >
            {playback.playing ? <PauseIcon size={20} /> : <PlayIcon size={20} />}
            {playback.playing
              ? "Pause Journey"
              : playback.paused
                ? "Resume Journey"
                : "Play Order Journey"}
          </button>

          {/* Hollow, never primary: Reset is a way back, not the main action.
              Disabled while idle — there is nothing to reset, and a live-looking
              button that does nothing is worse than a visibly inert one. */}
          <button
            type="button"
            onClick={reset}
            disabled={!journeyStarted}
            className="oil-journey-button"
            aria-label="Reset the order journey replay to the start"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              height: "32px",
              padding: "0 16px",
              background: "var(--cc-cloud-white)",
              border: `1px solid ${
                journeyStarted ? "var(--cc-grey-five)" : "var(--cc-grey-four)"
              }`,
              borderRadius: "8px",
              color: journeyStarted
                ? "var(--cc-heritage-blue)"
                : "var(--cc-grey-three)",
              fontSize: "14px",
              fontWeight: 600,
              lineHeight: "20px",
              cursor: journeyStarted ? "pointer" : "not-allowed",
            }}
          >
            <ArrowCounterClockwiseIcon size={20} />
            Reset
          </button>
        </div>

        <JourneySpeedControl speed={speed} onChange={setSpeed} />
      </div>

      {/* Legend — bottom-left, inside the canvas so it travels with the diagram
          card rather than competing with the page header. Hidden during a
          replay: the status bar takes the same corner, and the categories are
          not what the reader is following at that point. */}
      <div
        hidden={journeyRunning}
        style={{
          position: "absolute",
          left: "12px",
          bottom: "12px",
          display: journeyRunning ? "none" : "flex",
          flexWrap: "wrap",
          gap: "4px 16px",
          maxWidth: "calc(100% - 24px)",
          padding: "8px 12px",
          background: "var(--cc-cloud-white)",
          border: "1px solid var(--cc-grey-six)",
          borderRadius: "8px",
          boxShadow: "var(--cc-shadow-sm)",
          pointerEvents: "none",
        }}
      >
        {CATEGORY_LEGEND.map(({ category, label }) => (
          <span
            key={category}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "8px",
              fontSize: "12px",
              fontWeight: 600,
              lineHeight: "16px",
              color: "var(--cc-grey-two)",
            }}
          >
            <span
              aria-hidden="true"
              style={{
                width: "12px",
                height: "12px",
                flexShrink: 0,
                borderRadius: category === "queue" || category === "datastore" ? "2px" : "3px",
                background: CATEGORY_STYLE[category].fill,
                border: `1.5px solid ${CATEGORY_STYLE[category].stroke}`,
              }}
            />
            {label}
          </span>
        ))}
      </div>

      {journeyRunning && currentStep && (
        <JourneyStatusBar
          phase={currentStep.phase}
          caption={currentStep.caption}
          step={playback.stepIndex + 1}
          total={JOURNEY_STEPS.length}
          finished={playback.finished}
        />
      )}

      {tooltip && (
        <div
          ref={tooltipRef}
          role="tooltip"
          style={{
            position: "absolute",
            left: `${tipLeft}px`,
            top: `${tipTop}px`,
            width: `${TOOLTIP_WIDTH}px`,
            maxWidth: "calc(100% - 24px)",
            padding: "12px",
            background: "var(--cc-cloud-white)",
            border: "1px solid var(--cc-grey-five)",
            borderRadius: "8px",
            boxShadow: "var(--cc-shadow-lg)",
            // Never intercept the pointer: the tooltip sits under the cursor, so
            // a hit-testable layer there would immediately fire the node's
            // mouseleave and flicker it out of existence.
            pointerEvents: "none",
            zIndex: 5,
          }}
        >
          <div
            style={{
              fontSize: "14px",
              fontWeight: 700,
              lineHeight: "18px",
              color: "var(--cc-foundation-blue)",
              marginBottom: "4px",
            }}
          >
            {tooltip.node.label}
          </div>
          <div style={{ fontSize: "13px", lineHeight: "18px", color: "var(--cc-grey-two)" }}>
            {tooltip.node.description}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Speed slider for the journey replay.
 *
 * The slider is LOGARITHMIC, not linear: the useful range is 0.25x..4x, and on a
 * linear track 1x — by far the most-wanted value — would sit at 20%, with three
 * quarters of the travel spent on speeds faster than normal. Mapping through
 * log2 puts 1x exactly at the midpoint and gives each halving/doubling the same
 * width, so "one notch slower" feels the same at either end.
 *
 * `step` is fine-grained rather than snapped to presets: the request was a
 * continuous speed control, and the label reports the exact multiplier.
 */
const SPEED_EXP_MIN = Math.log2(MIN_SPEED); // -2
const SPEED_EXP_MAX = Math.log2(MAX_SPEED); // +2

function JourneySpeedControl({
  speed,
  onChange,
}: {
  speed: number;
  onChange: (next: number) => void;
}) {
  const labelId = "oil-journey-speed-label";
  return (
    <div
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "8px",
        height: "32px",
        padding: "0 12px",
        background: "var(--cc-cloud-white)",
        border: "1px solid var(--cc-grey-five)",
        borderRadius: "8px",
      }}
    >
      <span
        id={labelId}
        style={{
          fontSize: "14px",
          fontWeight: 500,
          lineHeight: "18px",
          color: "var(--cc-grey-two)",
        }}
      >
        Speed
      </span>
      <input
        type="range"
        min={SPEED_EXP_MIN}
        max={SPEED_EXP_MAX}
        step={0.05}
        value={Math.log2(speed)}
        onChange={(e) => onChange(2 ** Number(e.target.value))}
        aria-labelledby={labelId}
        // The visible label reads "Speed" and the value shows as a multiplier;
        // without this a screen reader would announce the raw log2 exponent.
        aria-valuetext={`${formatSpeed(speed)} times normal speed`}
        className="oil-speed-slider"
        style={{ width: "104px" }}
      />
      {/* Fixed width so the row does not reflow as the number changes width. */}
      <span
        style={{
          minWidth: "38px",
          fontSize: "14px",
          fontWeight: 600,
          lineHeight: "18px",
          color: "var(--cc-heritage-blue)",
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {formatSpeed(speed)}x
      </span>
    </div>
  );
}

/** 0.5 -> "0.5", 2 -> "2" — no trailing ".0" on whole multipliers. */
function formatSpeed(speed: number): string {
  return Number.isInteger(speed) ? String(speed) : speed.toFixed(2).replace(/0$/, "");
}

function ZoomButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="oil-zoom-button"
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: "32px",
        height: "32px",
        padding: 0,
        background: "var(--cc-cloud-white)",
        border: "1px solid var(--cc-grey-five)",
        borderRadius: "8px",
        color: "var(--cc-heritage-blue)",
        cursor: "pointer",
        lineHeight: 0,
      }}
    >
      {children}
    </button>
  );
}
