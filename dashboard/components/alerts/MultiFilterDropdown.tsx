"use client";

import { CaretDownIcon, CheckIcon } from "@phosphor-icons/react";
import { radii } from "@computacenter-ro/style-guide/tokens";
import {
  popoverRowStyle,
  popoverTriggerStyle,
  useFilterPopover,
} from "@/components/alerts/useFilterPopover";
import type { FilterOption } from "@/components/alerts/FilterDropdown";

interface MultiFilterDropdownProps {
  /** The filter's NAME, shown in the box — never replaced by the selection. */
  label: string;
  /** Currently ticked values. EMPTY MEANS NO FILTER (not "none of them"). */
  selected: string[];
  options: FilterOption[];
  onChange: (next: string[]) => void;
  /**
   * How many alerts each value would match under the OTHER active filters
   * (backend `GET /alerts/facets`, which applies exclude-self so this facet's own
   * ticks don't zero out its siblings). Sparse — a missing key means 0.
   *
   * `undefined` = not loaded yet, and the pills are omitted entirely rather than
   * shown as 0: a wrong count is worse than no count, since "0" reads as "there
   * is nothing here" and would talk the user out of a selection that works.
   */
  counts?: Record<string, number>;
}

/**
 * MULTI-select filter control: checkbox rows, a count badge in the box, and the
 * popover deliberately STAYS OPEN as you tick — picking three departments should
 * be three clicks, not three open-pick-reopen cycles.
 *
 * Empty `selected` means "no filter", matching the backend: `build_alerts_query`
 * treats an empty list and `None` alike, because "nothing ticked" has to show
 * everything. There is therefore no "All" sentinel option — clearing the ticks
 * *is* selecting all, and a "Clear" row is offered once something is ticked.
 *
 * Values are stored as an array in the order they were ticked; the backend ORs
 * them into a single `IN (...)`, so order carries no meaning.
 *
 * Shares the portal shell with `FilterDropdown` via `useFilterPopover` — see
 * that file for why the panel cannot be an ordinary absolutely positioned child.
 */
export function MultiFilterDropdown({
  label,
  selected,
  options,
  onChange,
  counts,
}: MultiFilterDropdownProps) {
  const { open, toggle, containerRef, triggerRef, panelId, renderPanel } =
    useFilterPopover(label);

  const count = selected.length;
  const isActive = count > 0;

  const toggleValue = (value: string) => {
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value]
    );
  };

  // Spelled out for screen readers, which get no benefit from the count badge.
  const summary = isActive
    ? options
        .filter((opt) => selected.includes(opt.value))
        .map((opt) => opt.label)
        .join(", ")
    : "any";

  return (
    <div ref={containerRef} style={{ position: "relative" }}>
      <button
        ref={triggerRef}
        type="button"
        className="oil-filter-select"
        onClick={toggle}
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={`${label}: ${summary}`}
        style={{
          ...popoverTriggerStyle,
          color: isActive ? "var(--cc-heritage-blue)" : "var(--cc-grey-one)",
          borderColor: isActive ? "var(--cc-heritage-blue)" : "var(--cc-grey-four)",
        }}
      >
        {label}
        {isActive && (
          // The count badge replaces the single-select dot: with several values
          // possible, "how many" is the useful signal. aria-hidden because the
          // trigger's aria-label already names every ticked value.
          <span
            aria-hidden="true"
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              minWidth: "18px",
              height: "18px",
              padding: "0 5px",
              borderRadius: radii.full,
              background: "var(--cc-heritage-blue)",
              color: "var(--cc-cloud-white)",
              fontSize: "12px",
              fontWeight: 600,
              lineHeight: "16px",
              flexShrink: 0,
            }}
          >
            {count}
          </span>
        )}
        <CaretDownIcon size={16} aria-hidden="true" />
      </button>

      {renderPanel(
        <>
          {options.map((opt) => {
            const checked = selected.includes(opt.value);
            return (
              <button
                key={opt.value}
                type="button"
                // role=checkbox + aria-checked, not role=option: these toggle
                // independently and the panel is not a single-choice listbox.
                role="checkbox"
                aria-checked={checked}
                // No close() here — that is the point of a multi-select.
                onClick={() => toggleValue(opt.value)}
                style={popoverRowStyle(checked)}
              >
                {/* A drawn box rather than <input type="checkbox"> so the tick
                    inherits Heritage Blue without fighting native control
                    styling. The row is the whole hit target. */}
                <span
                  aria-hidden="true"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    justifyContent: "center",
                    width: "16px",
                    height: "16px",
                    flexShrink: 0,
                    borderRadius: radii.sm,
                    border: `1px solid ${
                      checked ? "var(--cc-heritage-blue)" : "var(--cc-grey-four)"
                    }`,
                    background: checked ? "var(--cc-heritage-blue)" : "var(--cc-cloud-white)",
                    color: "var(--cc-cloud-white)",
                  }}
                >
                  {checked && <CheckIcon size={12} />}
                </span>
                {opt.label}
                {counts !== undefined && (
                  // Muted pill pushed to the right edge of the row. Grey, not the
                  // accent — it is context for the label beside it, not something
                  // to click, and it must not compete with the tick for attention.
                  <span
                    style={{
                      marginLeft: "auto",
                      flexShrink: 0,
                      padding: "1px 6px",
                      borderRadius: radii.full,
                      background: "var(--cc-grey-six)",
                      color: "var(--cc-grey-two)",
                      fontSize: "11px",
                      fontWeight: 500,
                      // Counts sit in a column, so equal-width digits keep the
                      // pills from jittering in width down the list.
                      fontVariantNumeric: "tabular-nums",
                    }}
                  >
                    {counts[opt.value] ?? 0}
                  </span>
                )}
              </button>
            );
          })}

          {isActive && (
            <>
              <span
                aria-hidden="true"
                style={{
                  display: "block",
                  height: "1px",
                  margin: "4px 0",
                  background: "var(--cc-grey-six)",
                }}
              />
              {/* Clearing every tick is what "show all" means here, so it gets an
                  explicit control instead of making the user untick one by one. */}
              <button
                type="button"
                onClick={() => onChange([])}
                style={{
                  ...popoverRowStyle(false),
                  color: "var(--cc-heritage-blue)",
                  fontWeight: 600,
                }}
              >
                Clear {label}
              </button>
            </>
          )}
        </>,
        "group"
      )}
    </div>
  );
}
