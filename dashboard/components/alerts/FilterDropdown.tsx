"use client";

import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CaretDownIcon } from "@phosphor-icons/react";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";

export interface FilterOption {
  value: string;
  label: string;
}

interface FilterDropdownProps {
  /** The filter's NAME, shown in the box. Stays put when a value is selected —
   *  the box is a labelled control, not a display of the current value. */
  label: string;
  value: string;
  options: FilterOption[];
  onChange: (value: string) => void;
  /** The "no filter" value (e.g. "all"). Anything else marks the control active. */
  defaultValue?: string;
}

// Panel width floor, and the gap kept from the viewport edge when the panel has
// to be pulled left to stay on screen. Both are needed as numbers (not just CSS)
// because the clamp is computed in JS against window.innerWidth.
const PANEL_MIN_WIDTH_PX = 180;
const VIEWPORT_MARGIN_PX = 8;

const triggerStyle: React.CSSProperties = {
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
};

/**
 * Single-select filter control: the filter's name sits in the box, with a caret
 * and — once something other than the default is picked — a filled dot marking
 * it active.
 *
 * The name-in-box form is what makes the bar readable at a glance: seven
 * controls each showing their *value* ("All Levels", "All Sources", …) forces
 * the reader to decode which filter is which, while seven showing their *name*
 * with a dot on the two that are set answers "what am I filtering by?"
 * immediately.
 *
 * Because the box hides the selected value, the trigger carries an
 * `aria-label` of "<name>: <selected label>" — a screen-reader user gets the
 * value that sighted users get from the dot plus the open popover.
 *
 * Behaviour is lifted from the Journeys table's Status header (button +
 * popover + click-outside + Escape) and generalized, so both read the same.
 *
 * The popover is rendered through a PORTAL onto document.body, positioned
 * `fixed` against the trigger's viewport rect. It has to be: the filter bar
 * collapses to one row with `overflow: hidden`, which clips any absolutely
 * positioned descendant — an in-flow popover was cut off at the row's edge.
 * Escaping to the body sidesteps the clip (and every ancestor stacking context)
 * at the cost of having to track the trigger's position by hand, which is what
 * the reposition listeners below do.
 */
export function FilterDropdown({
  label,
  value,
  options,
  onChange,
  defaultValue = "all",
}: FilterDropdownProps) {
  const [open, setOpen] = useState(false);
  // The trigger's viewport rect, captured when the popover opens and refreshed
  // on scroll/resize. Null until first opened — which is also why the portal
  // never runs during SSR: it takes a click to get here.
  const [rect, setRect] = useState<DOMRect | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const listboxId = useId();

  const isActive = value !== defaultValue;
  const selectedLabel = options.find((opt) => opt.value === value)?.label ?? value;

  // Measuring on open (rather than in an effect) means the panel's first paint
  // is already in the right place — no frame where it sits at 0,0 and jumps.
  const toggle = () => {
    if (open) {
      setOpen(false);
      return;
    }
    setRect(triggerRef.current?.getBoundingClientRect() ?? null);
    setOpen(true);
  };

  // Listeners only exist while the popover is open. Clicking another
  // dropdown's trigger counts as an outside click here, so at most one popover
  // is ever open without the parents having to coordinate.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      // The panel now lives on document.body, OUTSIDE containerRef — so it has
      // to be excluded explicitly, or clicking an option would register as an
      // outside click and close the popover before the option's own handler ran.
      if (containerRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      // Escape must not strand focus on a now-hidden option — hand it back to
      // the control the user opened.
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

  return (
    <div ref={containerRef} style={{ position: "relative" }}>
      <button
        ref={triggerRef}
        type="button"
        // Reuses the filter-bar control class for the hover border and the
        // Heritage Blue focus ring, so this button and every other control in
        // the bar respond identically.
        className="oil-filter-select"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-label={`${label}: ${selectedLabel}`}
        style={{
          ...triggerStyle,
          // Active filters take Heritage Blue text; the dot below repeats it, so
          // "this one is set" is never carried by color alone.
          color: isActive ? "var(--cc-heritage-blue)" : "var(--cc-grey-one)",
          borderColor: isActive ? "var(--cc-heritage-blue)" : "var(--cc-grey-four)",
        }}
      >
        {label}
        {isActive && (
          <span
            aria-hidden="true"
            style={{
              width: "6px",
              height: "6px",
              borderRadius: radii.full,
              background: "var(--cc-heritage-blue)",
              flexShrink: 0,
            }}
          />
        )}
        <CaretDownIcon size={16} aria-hidden="true" />
      </button>

      {open &&
        rect !== null &&
        createPortal(
          <div
            ref={panelRef}
            id={listboxId}
            role="listbox"
            aria-label={label}
            style={{
              // `fixed`, not `absolute`: on document.body there is no positioned
              // ancestor to anchor to, and viewport coordinates are exactly what
              // getBoundingClientRect returns.
              position: "fixed",
              top: rect.bottom + 4,
              // Left-aligned to the trigger, but pulled back so a panel opened
              // from a control near the right edge cannot run off-screen — the
              // portal escapes the row's clip, not the viewport's.
              left: Math.max(
                VIEWPORT_MARGIN_PX,
                Math.min(rect.left, window.innerWidth - PANEL_MIN_WIDTH_PX - VIEWPORT_MARGIN_PX)
              ),
              zIndex: 20,
              minWidth: `${PANEL_MIN_WIDTH_PX}px`,
              padding: semanticSpacing.xs,
              background: "var(--cc-cloud-white)",
              border: "1px solid var(--cc-grey-four)",
              borderRadius: radii.md,
              // shadow-lg is the token the guidelines assign to dropdown panels.
              boxShadow: "var(--cc-shadow-lg)",
            }}
          >
            {options.map((opt) => {
              const selected = opt.value === value;
              return (
                <button
                  key={opt.value}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => {
                    onChange(opt.value);
                    setOpen(false);
                    triggerRef.current?.focus();
                  }}
                  style={{
                    display: "block",
                    width: "100%",
                    textAlign: "left",
                    padding: `${semanticSpacing.xs} ${semanticSpacing.sm}`,
                    border: "none",
                    borderRadius: radii.sm,
                    background: selected ? "var(--cc-table-header-bg)" : "transparent",
                    color: "var(--cc-grey-one)",
                    fontSize: "14px",
                    fontWeight: selected ? 600 : 400,
                    fontFamily: "inherit",
                    cursor: "pointer",
                    whiteSpace: "nowrap",
                  }}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>,
          document.body
        )}
    </div>
  );
}
