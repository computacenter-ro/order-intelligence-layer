"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { CaretDownIcon, CaretRightIcon } from "@phosphor-icons/react";
import type { LogLevel } from "@/lib/types";
import type { OrderGroup } from "@/lib/incidents";

// Same level -> colour mapping already used in JourneyTimeline.tsx (kept local
// rather than extracted, matching that file's own precedent of a local map).
const LEVEL_COLOR: Record<LogLevel, string> = {
  DEBUG: "var(--cc-grey-four)",
  INFO: "var(--cc-circuit-green)",
  WARN: "var(--cc-fibre-orange)",
  ERROR: "var(--cc-united-red)",
};

interface OrderGroupRowProps {
  group: OrderGroup;
  /** Detail page passes true so the breakdown reads fully expanded on load;
   * the card's inline accordion defaults to collapsed. */
  defaultExpanded?: boolean;
}

export function OrderGroupRow({ group, defaultExpanded = false }: OrderGroupRowProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const router = useRouter();
  const clickable = group.journeyId !== null;

  return (
    <div
      style={{
        border: "1px solid var(--cc-grey-six)",
        borderRadius: "8px",
        marginTop: "8px",
        overflow: "hidden",
      }}
    >
      <div
        role={clickable ? "button" : undefined}
        tabIndex={clickable ? 0 : undefined}
        onClick={() => {
          if (clickable) router.push(`/journeys/${group.journeyId}`);
        }}
        onKeyDown={(e) => {
          if (clickable && (e.key === "Enter" || e.key === " ")) {
            router.push(`/journeys/${group.journeyId}`);
          }
        }}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          padding: "10px 12px",
          cursor: clickable ? "pointer" : "default",
        }}
      >
        <button
          type="button"
          aria-label={expanded ? "Collapse order" : "Expand order"}
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          style={{
            display: "inline-flex",
            border: "none",
            background: "transparent",
            color: "var(--cc-heritage-blue)",
            cursor: "pointer",
            padding: 0,
          }}
        >
          {expanded ? <CaretDownIcon size={16} /> : <CaretRightIcon size={16} />}
        </button>
        <span
          style={{
            fontFamily: "ui-monospace, Menlo, monospace",
            fontSize: "14px",
            color: "var(--cc-grey-one)",
          }}
        >
          {group.label}
        </span>
        <span style={{ fontSize: "14px", color: LEVEL_COLOR[group.outcome.level] }}>
          {group.outcome.message}
        </span>
        <span style={{ marginLeft: "auto", fontSize: "12px", color: "var(--cc-grey-three)" }}>
          {group.alerts.length} alerts
        </span>
      </div>
      {expanded && (
        <div
          style={{
            padding: "0 12px 12px 36px",
            display: "flex",
            flexDirection: "column",
            gap: "6px",
          }}
        >
          {group.alerts.map((alert) => (
            <div key={alert.alert_id} style={{ display: "flex", gap: "8px", fontSize: "13px" }}>
              <span
                style={{
                  fontWeight: 600,
                  color: LEVEL_COLOR[alert.level],
                  minWidth: "48px",
                  flexShrink: 0,
                }}
              >
                {alert.level}
              </span>
              <span
                style={{
                  color: "var(--cc-grey-one)",
                  fontFamily: "ui-monospace, Menlo, monospace",
                }}
              >
                {alert.message}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
