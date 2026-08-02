"use client";

import { PHASE_LABEL, idsForPhase, type JourneyPhase } from "@/lib/architectureJourney";

/**
 * The strip under the diagram during a journey replay: which phase the order is
 * in, what is happening right now, and — the point of the whole animation —
 * which ids a log emitted at this instant would actually carry.
 *
 * The id panel is the teaching device. Watching `eventId` go from present to
 * struck-through while `orderId` / `cartHeaderId` appear is the correlation
 * model (CLAUDE.md) made visible: there is no single id on every log, so
 * correlating by one field is impossible.
 */

interface JourneyStatusBarProps {
  phase: JourneyPhase;
  caption: string;
  /** 1-based position in the journey, for the progress readout. */
  step: number;
  total: number;
  finished: boolean;
}

/** Per-phase accent. Signal colours, used only on this small strip. */
const PHASE_ACCENT: Record<JourneyPhase, string> = {
  // Pre-creation: in flight, nothing decided yet.
  phase1: "var(--cc-horizon-blue)",
  // The ids are minted — the one genuinely creative moment.
  creation: "var(--cc-circuit-green)",
  // The order_data_ready return leg. Amber because it is the trap: a return
  // hop LOOKS like it should carry new ids, and it carries eventId only — the
  // order (and both its ids) still does not exist at this point.
  bridge: "var(--cc-fibre-orange)",
  phase2: "var(--cc-heritage-blue)",
};

function IdChip({
  label,
  value,
  state,
}: {
  label: string;
  value: string | null;
  /** `gone` renders the id as struck-through — it existed, and no longer does. */
  state: "present" | "absent" | "gone" | "text-only";
}) {
  const active = state === "present" || state === "text-only";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "2px", minWidth: 0 }}>
      <span
        style={{
          fontSize: "12px",
          fontWeight: 600,
          lineHeight: "16px",
          color: active ? "var(--cc-grey-two)" : "var(--cc-grey-three)",
        }}
      >
        {label}
        {/* The creation logs carry the order ids in their message TEXT, not as
            fields — that distinction is the whole basis of id mining, so it is
            labelled rather than glossed over. */}
        {state === "text-only" && (
          <span style={{ fontWeight: 500, color: "var(--cc-grey-three)" }}> · in message text</span>
        )}
      </span>
      <span
        className="oil-mono"
        style={{
          fontSize: "13px",
          lineHeight: "18px",
          color: active ? "var(--cc-foundation-blue)" : "var(--cc-grey-four)",
          // Only ever struck through over a real value; striking the placeholder
          // prose would just look like a rendering glitch.
          textDecoration: state === "gone" && value ? "line-through" : "none",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {/* Absent and GONE are different facts, and conflating them would
            undercut the whole point: an order id does not exist yet in phase 1,
            whereas eventId genuinely existed and has dropped off the logs. */}
        {value ?? (state === "gone" ? "— no longer on the logs" : "— not yet minted")}
      </span>
    </div>
  );
}

export function JourneyStatusBar({
  phase,
  caption,
  step,
  total,
  finished,
}: JourneyStatusBarProps) {
  const ids = idsForPhase(phase);
  const accent = PHASE_ACCENT[phase];

  // How each id should read at this moment. `eventId` in phase 2 is "gone"
  // rather than merely absent: it was there and has dropped off the logs, which
  // is exactly the fact the animation is teaching.
  const eventState = ids.eventId ? "present" : "gone";
  const orderState =
    phase === "creation" ? "text-only" : ids.orderId ? "present" : "absent";

  return (
    <div
      role="status"
      aria-live="polite"
      style={{
        position: "absolute",
        left: "12px",
        right: "12px",
        bottom: "12px",
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "12px 24px",
        padding: "12px 16px",
        background: "var(--cc-cloud-white)",
        border: "1px solid var(--cc-grey-five)",
        borderLeft: `4px solid ${accent}`,
        borderRadius: "8px",
        boxShadow: "var(--cc-shadow-md)",
        zIndex: 4,
      }}
    >
      <div style={{ flex: "1 1 340px", minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            fontSize: "12px",
            fontWeight: 600,
            lineHeight: "16px",
            color: accent === "var(--cc-horizon-blue)" ? "#005C99" : "var(--cc-grey-two)",
            textTransform: "uppercase",
            letterSpacing: "1.6px",
          }}
        >
          {finished ? "Journey complete · SUCCESS" : PHASE_LABEL[phase]}
          <span
            style={{
              fontWeight: 500,
              letterSpacing: 0,
              textTransform: "none",
              color: "var(--cc-grey-three)",
            }}
          >
            {step}/{total}
          </span>
        </div>
        <div
          style={{
            fontSize: "16px",
            lineHeight: "22px",
            color: "var(--cc-grey-one)",
            marginTop: "2px",
          }}
        >
          {caption}
        </div>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(168px, 1fr))",
          gap: "8px 20px",
          flex: "1 1 420px",
          minWidth: 0,
        }}
      >
        <IdChip label="eventId" value={ids.eventId} state={eventState} />
        <IdChip label="orderId" value={ids.orderId} state={orderState} />
        <IdChip label="cartHeaderId" value={ids.cartHeaderId} state={orderState} />
      </div>
    </div>
  );
}
