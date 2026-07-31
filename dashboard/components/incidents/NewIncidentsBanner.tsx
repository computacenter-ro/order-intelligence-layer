"use client";

import { useEffect, useRef, useState } from "react";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { formatNotificationCount } from "@/lib/format";

interface NewIncidentsBannerProps {
  count: number;
  onReveal: () => void;
}

/** Nearest ancestor that actually scrolls (the app content scrolls inside
 *  AppShell's <main>, not the window), or null to fall back to the window. */
function getScrollParent(node: HTMLElement): HTMLElement | null {
  let el: HTMLElement | null = node.parentElement;
  while (el) {
    const { overflowY } = getComputedStyle(el);
    if (overflowY === "auto" || overflowY === "scroll") return el;
    el = el.parentElement;
  }
  return null;
}

/** Same live-count-pill pattern as NewAlertsBanner, for incident.new events. */
export function NewIncidentsBanner({ count, onReveal }: NewIncidentsBannerProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [scrolled, setScrolled] = useState(false);
  const visible = count > 0;

  useEffect(() => {
    if (!visible) return;
    const el = ref.current;
    if (!el) return;
    const scroller = getScrollParent(el);
    const target: HTMLElement | Window = scroller ?? window;
    const read = () =>
      setScrolled((scroller ? scroller.scrollTop : window.scrollY) > 8);
    read(); // set initial state
    target.addEventListener("scroll", read, { passive: true });
    return () => target.removeEventListener("scroll", read);
  }, [visible]);

  if (!visible) return null;

  const tone = badgeColors.pending;
  // Pluralization and "1 new incident" reflect the REAL count — only the
  // printed number itself is capped.
  const label = count === 1 ? "1 new incident" : `${formatNotificationCount(count)} new incidents`;

  return (
    // Full-width sticky strip; transparent + click-through so only the pill
    // is interactive and it never blocks the list behind it.
    <div
      ref={ref}
      style={{
        position: "sticky",
        top: 0,
        zIndex: 10,
        display: "flex",
        justifyContent: "flex-end",
        marginBottom: "12px",
        pointerEvents: "none",
      }}
    >
      <button
        type="button"
        className="oil-new-incidents-banner"
        onClick={onReveal}
        style={{
          pointerEvents: "auto",
          width: "100%",
          // Animatable width via max-width: full row at top of page, collapsing
          // to a ~220px pill (right-aligned) once the user scrolls.
          maxWidth: scrolled ? "220px" : "2000px",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          gap: "6px",
          padding: scrolled ? "6px 14px" : "10px 16px",
          borderRadius: "9999px",
          border: `1px solid ${tone.border}`,
          backgroundColor: tone.bg,
          color: tone.text,
          fontSize: "13px",
          fontWeight: 600,
          cursor: "pointer",
          boxShadow: scrolled ? "0 1px 4px rgba(0, 0, 0, 0.12)" : "none",
          transition: "max-width 0.35s ease, padding 0.35s ease, box-shadow 0.35s ease",
        }}
      >
        {scrolled && <span aria-hidden="true">↑</span>}
        {label}
      </button>
    </div>
  );
}
