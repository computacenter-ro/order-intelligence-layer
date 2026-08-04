"use client";

import { CaretDownIcon } from "@phosphor-icons/react";
import { radii } from "@computacenter-ro/style-guide/tokens";
import {
  popoverRowStyle,
  popoverTriggerStyle,
  useFilterPopover,
} from "@/components/alerts/useFilterPopover";

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
  /**
   * Offer a "Clear <label>" row once a value is picked, so `defaultValue` can be
   * a state the user returns to rather than a row they select. Lets a filter start
   * with NOTHING selected and still get back there — the same contract
   * `MultiFilterDropdown` has, where clearing the ticks *is* selecting all.
   *
   * Opt-in, and deliberately so: the Alert Feed's filters carry an explicit
   * "All Levels"/"All Sources" row, which is a pickable option rather than an
   * empty state, and must keep behaving that way.
   */
  clearable?: boolean;
}

/**
 * SINGLE-select filter control: the filter's name sits in the box, with a caret
 * and — once something other than the default is picked — a filled dot marking
 * it active. Picking an option closes the popover.
 *
 * See `MultiFilterDropdown` for the checkbox variant (severity / service /
 * department), and `useFilterPopover` for the portal shell both share.
 *
 * The name-in-box form is what makes the bar readable at a glance: seven
 * controls each showing their *value* ("All Levels", "All Sources", …) forces
 * the reader to decode which filter is which, while seven showing their *name*
 * with a dot on the ones that are set answers "what am I filtering by?"
 * immediately.
 *
 * Because the box hides the selected value, the trigger carries an
 * `aria-label` of "<name>: <selected label>" — a screen-reader user gets the
 * value that sighted users get from the dot plus the open popover.
 */
export function FilterDropdown({
  label,
  value,
  options,
  onChange,
  defaultValue = "all",
  clearable = false,
}: FilterDropdownProps) {
  const { open, toggle, close, containerRef, triggerRef, panelId, renderPanel } =
    useFilterPopover(label);

  const isActive = value !== defaultValue;
  // Under `clearable` the default is an EMPTY selection, which matches no option —
  // so fall back to "any" rather than letting the trigger announce "Status: " and
  // tell a screen-reader user nothing. Mirrors MultiFilterDropdown's `summary`.
  const selectedLabel =
    options.find((opt) => opt.value === value)?.label ?? (value || "any");

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
        aria-controls={open ? panelId : undefined}
        aria-label={`${label}: ${selectedLabel}`}
        style={{
          ...popoverTriggerStyle,
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

      {renderPanel(
        <>
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
                close();
              }}
              style={popoverRowStyle(selected)}
            >
              {/* Radio circle — the single-select counterpart to
                  MultiFilterDropdown's checkbox square: a ring with a filled
                  dot when chosen, so "pick one" reads differently from "pick
                  many" at a glance. */}
              <span
                aria-hidden="true"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  width: "16px",
                  height: "16px",
                  flexShrink: 0,
                  borderRadius: radii.full,
                  border: `1px solid ${
                    selected ? "var(--cc-heritage-blue)" : "var(--cc-grey-four)"
                  }`,
                  background: "var(--cc-cloud-white)",
                }}
              >
                {selected && (
                  <span
                    style={{
                      width: "8px",
                      height: "8px",
                      borderRadius: radii.full,
                      background: "var(--cc-heritage-blue)",
                    }}
                  />
                )}
              </span>
              {opt.label}
            </button>
          );
        })}

        {clearable && isActive && (
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
            {/* Returning to "no filter" needs its own control once the default is
                an empty selection — there is no row to re-pick. Still role=option
                (selecting "none" IS one of the choices), which keeps the panel a
                valid listbox rather than a listbox with a stray button in it. */}
            <button
              type="button"
              role="option"
              aria-selected={false}
              onClick={() => {
                onChange(defaultValue);
                close();
              }}
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
        "listbox"
      )}
    </div>
  );
}
