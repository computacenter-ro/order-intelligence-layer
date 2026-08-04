"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CaretDownIcon, CaretUpIcon } from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { capitalize } from "@/lib/format";
import type { AlertFacets } from "@/lib/api";
import { FilterDropdown, type FilterOption } from "@/components/alerts/FilterDropdown";
import { MultiFilterDropdown } from "@/components/alerts/MultiFilterDropdown";
import { SearchInput } from "@/components/alerts/SearchInput";
import type { Department, Severity } from "@/lib/types";
import {
  APP_NAMES,
  CACHED_FILTERS,
  DEFAULT_ALERT_FILTERS,
  DEPARTMENTS,
  LEVEL_FILTERS,
  SEVERITIES,
  SOURCE_FILTERS,
  TIME_FILTERS,
  hasActiveFilters,
  type AlertFilters,
  type AppNameFilter,
  type CachedFilter,
  type LevelFilter,
  type SourceFilter,
  type TimeFilter,
} from "@/lib/alertFilters";

// --- filter model ------------------------------------------------------------
//
// The model itself — types, domain lists, and the pure functions over them — lives
// in `lib/alertFilters.ts` and is re-exported here so every existing import site
// (`from "@/components/alerts/AlertFilterBar"`) keeps working. It was moved out
// because `npm test` runs Node's test runner over `lib/**` and Node cannot strip
// JSX, so nothing defined in this file is reachable from a test — and this logic
// mirrors a server contract and sanitises untrusted localStorage. Same split as
// `lib/journeyFilters.ts`.
//
// What stays below is presentational only: labels, dropdown options, the bar.

// Re-exported wholesale so every existing import site keeps working unchanged.
export * from "@/lib/alertFilters";

// Labels spelled out where capitalize() would mangle them ("ai" -> "Ai").
const SOURCE_LABELS: Record<SourceFilter, string> = {
  all: "All Sources",
  ai: "AI",
  fallback: "Fallback",
};

const LEVEL_LABELS: Record<LevelFilter, string> = {
  all: "All Levels",
  WARN: "WARN",
  ERROR: "ERROR",
};

const CACHED_LABELS: Record<CachedFilter, string> = {
  all: "All Answers",
  cached: "Cached",
  fresh: "Freshly analyzed",
};

const TIME_LABELS: Record<TimeFilter, string> = {
  all: "All time",
  "1h": "Last 1h",
  "24h": "Last 24h",
  "7d": "Last 7 days",
};

const SEVERITY_LABELS: Record<Severity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

// --- component ---------------------------------------------------------------

interface AlertFilterBarProps {
  value: AlertFilters;
  onChange: (next: AlertFilters) => void;
  /**
   * Per-value counts from `GET /alerts/facets`, or null while loading. Passed
   * straight through to the three multi-selects; `undefined` there hides the
   * count pills, so a loading bar shows plain options rather than a row of 0s.
   */
  facets?: AlertFacets | null;
}

// Option lists handed to FilterDropdown. Each pairs the value the filter model
// uses with the label already defined above, so the dropdowns and the filter
// contract cannot drift apart.
// The three multi-selects list concrete values only — no "All" row, because
// clearing every tick already means "all" (and an "All" checkbox would beg the
// question of what "All + Backend" means).
const DEPARTMENT_OPTIONS: FilterOption[] = DEPARTMENTS.map((d) => ({
  value: d,
  label: capitalize(d),
}));

// app_name has no label map — the service names ARE the labels (they are the
// literal app_name values the backend stores).
const APP_NAME_OPTIONS: FilterOption[] = APP_NAMES.map((name) => ({
  value: name,
  label: name,
}));

const SEVERITY_OPTIONS: FilterOption[] = SEVERITIES.map((s) => ({
  value: s,
  label: SEVERITY_LABELS[s],
}));

const LEVEL_OPTIONS: FilterOption[] = LEVEL_FILTERS.map((l) => ({
  value: l,
  label: LEVEL_LABELS[l],
}));

const SOURCE_OPTIONS: FilterOption[] = SOURCE_FILTERS.map((s) => ({
  value: s,
  label: SOURCE_LABELS[s],
}));

const CACHED_OPTIONS: FilterOption[] = CACHED_FILTERS.map((c) => ({
  value: c,
  label: CACHED_LABELS[c],
}));

const TIME_OPTIONS: FilterOption[] = TIME_FILTERS.map((t) => ({
  value: t,
  label: TIME_LABELS[t],
}));

// Height of the collapsed filter row. The FilterDropdown triggers are 32px and
// the container's row-gap is 8px, so the second row starts at y=40 — any cap in
// [32, 40) shows row one whole and row two not at all. 36px sits in the middle,
// leaving a little tolerance for font-rendering differences without letting a
// sliver of row two peek through.
const COLLAPSED_MAX_PX = 36;

/**
 * True when any filter is set away from its default. Spelled out rather than
 * looped over the defaults: the three multi-selects are arrays, and
 * `value.department === DEFAULT_ALERT_FILTERS.department` compares references —
 * always false for a fresh []. Exported so pages can tell "no results because
 * of filters" apart from "no data at all" in their empty states.
 */
export function AlertFilterBar({ value, onChange, facets }: AlertFilterBarProps) {
  const isDefault = !hasActiveFilters(value);

  const [expanded, setExpanded] = useState(false);
  // Whether the seven controls need more than one row at the current width —
  // i.e. whether collapsing actually hides anything. Drives the toggle's
  // existence, so on a wide screen where everything fits there is no toggle.
  const [overflows, setOverflows] = useState(false);
  const filtersRef = useRef<HTMLDivElement>(null);
  const filtersId = useId();

  useEffect(() => {
    const el = filtersRef.current;
    if (!el) return;
    // ResizeObserver fires once immediately on observe(), so the first
    // measurement and every later resize take the same path — no setState in
    // the effect body.
    //
    // Re-created when `value` changes because a filter becoming active widens
    // its trigger (the dot plus its gap), which can push a control onto a
    // second row without the container itself resizing — so the observer alone
    // would never hear about it.
    const observer = new ResizeObserver(() => {
      // Compared against the cap rather than clientHeight: scrollHeight is the
      // full content height in BOTH states (the maxHeight cap doesn't shrink
      // it), whereas `scrollHeight > clientHeight` degenerates to false once
      // expanded — which would make the "Fewer Filters" button disappear the
      // moment it was needed.
      setOverflows(el.scrollHeight > COLLAPSED_MAX_PX);
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [value]);

  // Nothing hidden, nothing to toggle.
  const showToggle = overflows;

  return (
    <div style={{ marginBottom: semanticSpacing.lg }}>
      {/* Search sits OUTSIDE the collapsible container on purpose: inside it, a
          narrow window could push it onto the hidden second row, leaving the box
          the user is typing in behind a "More Filters" toggle. */}
      <div style={{ marginBottom: semanticSpacing.sm }}>
        <SearchInput
          value={value.search}
          onChange={(search) => onChange({ ...value, search })}
        />
      </div>

      {/* flex-wrap makes the row count follow the viewport on its own; the cap
          below is what turns "wraps onto more rows" into "hidden behind a
          toggle". Ordered by triage relevance: what an on-call agent reaches
          for first (how bad / what kind / how recent) comes before the
          narrowing-down filters. */}
      <div
        ref={filtersRef}
        id={filtersId}
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: semanticSpacing.sm,
          maxHeight: expanded ? "none" : `${COLLAPSED_MAX_PX}px`,
          overflow: "hidden",
        }}
      >
        <MultiFilterDropdown
          label="Severity"
          selected={value.severity}
          options={SEVERITY_OPTIONS}
          counts={facets?.severity}
          onChange={(next) => onChange({ ...value, severity: next as Severity[] })}
        />
        <FilterDropdown
          label="Level"
          value={value.level}
          defaultValue={DEFAULT_ALERT_FILTERS.level}
          options={LEVEL_OPTIONS}
          onChange={(v) => onChange({ ...value, level: v as LevelFilter })}
        />
        <FilterDropdown
          label="Time"
          value={value.time}
          defaultValue={DEFAULT_ALERT_FILTERS.time}
          options={TIME_OPTIONS}
          onChange={(v) => onChange({ ...value, time: v as TimeFilter })}
        />
        <MultiFilterDropdown
          label="Service"
          selected={value.app_name}
          options={APP_NAME_OPTIONS}
          counts={facets?.app_name}
          onChange={(next) => onChange({ ...value, app_name: next as AppNameFilter[] })}
        />
        <MultiFilterDropdown
          label="Department"
          selected={value.department}
          options={DEPARTMENT_OPTIONS}
          counts={facets?.department}
          onChange={(next) => onChange({ ...value, department: next as Department[] })}
        />
        <FilterDropdown
          label="Source"
          value={value.source}
          defaultValue={DEFAULT_ALERT_FILTERS.source}
          options={SOURCE_OPTIONS}
          onChange={(v) => onChange({ ...value, source: v as SourceFilter })}
        />
        <FilterDropdown
          label="Answer"
          value={value.cached}
          defaultValue={DEFAULT_ALERT_FILTERS.cached}
          options={CACHED_OPTIONS}
          onChange={(v) => onChange({ ...value, cached: v as CachedFilter })}
        />
      </div>

      {/* Actions row. Rendered only when it would hold something, so a default,
          fits-on-one-row bar carries no chrome at all. */}
      {(showToggle || !isDefault) && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: semanticSpacing.sm,
            marginTop: semanticSpacing.sm,
          }}
        >
          {showToggle && (
            // A plain <button> rather than the style-guide Button: a disclosure
            // control needs aria-expanded / aria-controls, which that component
            // does not forward. The ghost variant's look and states come from
            // the shared .oil-filter-toggle class, so it stays consistent.
            <button
              type="button"
              className="oil-filter-toggle"
              onClick={() => setExpanded((prev) => !prev)}
              aria-expanded={expanded}
              aria-controls={filtersId}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: semanticSpacing.xs,
                height: "32px",
                padding: `0 ${semanticSpacing.md}`,
                // No `background` here on purpose — the transparent resting fill
                // comes from .oil-filter-toggle. Setting it inline would win on
                // specificity and kill that class's :hover / :active states.
                border: "none",
                borderRadius: radii.md,
                color: "var(--cc-heritage-blue)",
                fontSize: "14px",
                fontWeight: 600,
                fontFamily: "inherit",
                cursor: "pointer",
              }}
            >
              {expanded ? "Fewer Filters" : "More Filters"}
              {expanded ? (
                <CaretUpIcon size={16} aria-hidden="true" />
              ) : (
                <CaretDownIcon size={16} aria-hidden="true" />
              )}
            </button>
          )}

          {showToggle && !isDefault && (
            <span
              aria-hidden="true"
              style={{
                width: "1px",
                height: "20px",
                background: "var(--cc-grey-five)",
                flexShrink: 0,
              }}
            />
          )}

          {/* Shown only when something is set (phase 1): the button's presence
              is itself the signal that there is something to clear. */}
          {!isDefault && (
            <Button variant="secondary" size="compact" onClick={() => onChange(DEFAULT_ALERT_FILTERS)}>
              Reset Filters
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
