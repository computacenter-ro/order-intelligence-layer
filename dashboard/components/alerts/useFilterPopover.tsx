"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";

// Panel width floor, and the gap kept from the viewport edge when the panel has
// to be pulled left to stay on screen. Needed as numbers (not just CSS) because
// the clamp is computed in JS against window.innerWidth.
const PANEL_MIN_WIDTH_PX = 180;
const VIEWPORT_MARGIN_PX = 8;

/**
 * The popover shell shared by every filter control in the bar.
 *
 * It exists as ONE implementation on purpose. The panel has to be rendered
 * through a portal onto `document.body`: the filter bar collapses to a single
 * row with `overflow: hidden`, which clips any absolutely positioned
 * descendant, so an in-flow popover gets cut off at the row's edge. Escaping to
 * the body sidesteps the clip and every ancestor stacking context, at the cost
 * of tracking the trigger's position by hand — and every fiddly part of that
 * (fixed positioning from a measured rect, click-outside that has to account
 * for the panel no longer being a descendant, reposition on scroll/resize)
 * lives here rather than being copied per control, so the clipping bug cannot
 * come back through a second implementation.
 *
 * Returns the refs to attach, the open state, and `renderPanel` — call it with
 * the panel's children and it produces the portal (or null while closed).
 */
export function useFilterPopover(label: string) {
  const [open, setOpen] = useState(false);
  // The trigger's viewport rect, captured when the popover opens and refreshed
  // on scroll/resize. Null until first opened — which is also why the portal
  // never runs during SSR: it takes a click to get here.
  const [rect, setRect] = useState<DOMRect | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  // Measuring on open (rather than in an effect) means the panel's first paint
  // is already in the right place — no frame where it sits at 0,0 and jumps.
  const toggle = useCallback(() => {
    setOpen((prev) => {
      if (prev) return false;
      setRect(triggerRef.current?.getBoundingClientRect() ?? null);
      return true;
    });
  }, []);

  // Listeners only exist while the popover is open. Clicking another control's
  // trigger counts as an outside click here, so at most one popover is ever
  // open without the parents having to coordinate.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      // The panel lives on document.body, OUTSIDE containerRef — so it has to be
      // excluded explicitly, or clicking inside it would register as an outside
      // click and close the popover before its own handler ran.
      if (containerRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      // Escape must not strand focus inside a now-unmounted panel — hand it
      // back to the control the user opened.
      triggerRef.current?.focus();
    };
    const reposition = () => {
      const next = triggerRef.current?.getBoundingClientRect();
      if (next) setRect(next);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    // `scroll` does not bubble, and the real scroll container is AppShell's
    // <main> (overflow-y: auto), not the window — so this MUST be a capturing
    // listener. A bubble-phase window listener would never fire and the fixed
    // panel would float away from its button as the page scrolled.
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
    };
  }, [open]);

  const close = useCallback(() => {
    setOpen(false);
    triggerRef.current?.focus();
  }, []);

  const renderPanel = (children: React.ReactNode, role: "listbox" | "group") => {
    if (!open || rect === null) return null;
    return createPortal(
      <div
        ref={panelRef}
        id={panelId}
        role={role}
        aria-label={label}
        style={{
          // `fixed`, not `absolute`: on document.body there is no positioned
          // ancestor to anchor to, and viewport coordinates are exactly what
          // getBoundingClientRect returns.
          position: "fixed",
          top: rect.bottom + 4,
          // Left-aligned to the trigger, but pulled back so a panel opened from
          // a control near the right edge cannot run off-screen — the portal
          // escapes the row's clip, not the viewport's.
          left: Math.max(
            VIEWPORT_MARGIN_PX,
            Math.min(rect.left, window.innerWidth - PANEL_MIN_WIDTH_PX - VIEWPORT_MARGIN_PX)
          ),
          zIndex: 20,
          minWidth: `${PANEL_MIN_WIDTH_PX}px`,
          maxHeight: "min(60vh, 420px)",
          overflowY: "auto",
          padding: semanticSpacing.xs,
          background: "var(--cc-cloud-white)",
          border: "1px solid var(--cc-grey-four)",
          borderRadius: radii.md,
          // shadow-lg is the token the guidelines assign to dropdown panels.
          boxShadow: "var(--cc-shadow-lg)",
        }}
      >
        {children}
      </div>,
      document.body
    );
  };

  return { open, toggle, close, containerRef, triggerRef, panelId, renderPanel };
}

/** Shared trigger-button chrome: 32px, Cloud White, md radius, caret at the end. */
export const popoverTriggerStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: semanticSpacing.sm,
  height: "32px",
  padding: `0 ${semanticSpacing.md}`,
  fontSize: "14px",
  fontWeight: 500,
  fontFamily: "inherit",
  backgroundColor: "var(--cc-cloud-white)",
  border: "1px solid var(--cc-grey-four)",
  borderRadius: radii.md,
  cursor: "pointer",
  whiteSpace: "nowrap",
};

/** Shared style for one row inside a popover panel. */
export function popoverRowStyle(emphasized: boolean): React.CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    gap: semanticSpacing.sm,
    width: "100%",
    textAlign: "left",
    padding: `${semanticSpacing.xs} ${semanticSpacing.sm}`,
    border: "none",
    borderRadius: radii.sm,
    background: emphasized ? "var(--cc-table-header-bg)" : "transparent",
    color: "var(--cc-grey-one)",
    fontSize: "14px",
    fontWeight: emphasized ? 600 : 400,
    fontFamily: "inherit",
    cursor: "pointer",
    whiteSpace: "nowrap",
  };
}
