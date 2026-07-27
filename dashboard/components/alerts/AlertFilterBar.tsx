"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CaretDownIcon, CaretUpIcon } from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { capitalize } from "@/lib/format";
import { FilterDropdown, type FilterOption } from "@/components/alerts/FilterDropdown";
import type { Department, ProcessedAlert } from "@/lib/types";

// --- filter model ------------------------------------------------------------
//
// "all" is the UI-only sentinel meaning "no filter"; the page maps it to
// `undefined` before calling fetchAlerts (which omits the query param entirely).
// The concrete values mirror the backend contract exactly — Department values,
// ProcessedAlert.source, the WARN/ERROR levels, and app_name — so a selection
// round-trips to /alerts unchanged.

export type DepartmentFilter = Department | "all";
export type SourceFilter = "all" | "ai" | "fallback";
export type LevelFilter = "all" | "WARN" | "ERROR";
export type AppNameFilter = "all" | (typeof APP_NAMES)[number];
export type SeverityFilter = "all" | "critical" | "high" | "medium" | "low";
// Semantic-cache provenance. Orthogonal to SourceFilter — cached alerts are all
// source="ai" — so this narrows within AI answers rather than competing with it.
// "all" is the same UI-only sentinel; the page maps it to `undefined`.
export type CachedFilter = "all" | "cached" | "fresh";
// A ROLLING window, not an absolute range: the selection names a span, and the
// `since` bound is recomputed from `Date.now()` on every fetch (see
// `sinceForTimeFilter`). Storing a resolved timestamp instead would freeze the
// window at selection time, so a tab left open would slowly stop showing recent
// alerts.
export type TimeFilter = "all" | "1h" | "24h" | "7d";

export interface AlertFilters {
  department: DepartmentFilter;
  source: SourceFilter;
  level: LevelFilter;
  app_name: AppNameFilter;
  severity: SeverityFilter;
  cached: CachedFilter;
  time: TimeFilter;
}

export const DEFAULT_ALERT_FILTERS: AlertFilters = {
  department: "all",
  source: "all",
  level: "all",
  app_name: "all",
  severity: "all",
  cached: "all",
  time: "all",
};

const DEPARTMENTS: Department[] = ["networking", "devops", "backend", "database", "general"];

// Fixed roster of the pipeline's emitters (CLAUDE.md [1] Services). app_name is
// a free string server-side, but the UI offers this closed list so the control
// is a dropdown rather than free text.
const APP_NAMES = [
  "cc-inbound-service",
  "cc-order-engine",
  "cc-spt-service",
  "cc-rsm-service",
  "cc-solr-service",
  "cc-jam-service",
  "cc-settings-service",
  "cc-checker-service",
  "cc-avalara-service",
  "cc-validator-service",
  "cc-outbound-osw",
  "cc-track-trace",
] as const;

const DEPARTMENT_FILTERS: DepartmentFilter[] = ["all", ...DEPARTMENTS];
const SOURCE_FILTERS: SourceFilter[] = ["all", "ai", "fallback"];
const LEVEL_FILTERS: LevelFilter[] = ["all", "WARN", "ERROR"];
const APP_NAME_FILTERS: AppNameFilter[] = ["all", ...APP_NAMES];

// Router LLM severities (shared/models.py Severity), most→least urgent.
const SEVERITIES = ["critical", "high", "medium", "low"] as const;
const SEVERITY_FILTERS: SeverityFilter[] = ["all", ...SEVERITIES];
const CACHED_FILTERS: CachedFilter[] = ["all", "cached", "fresh"];
const TIME_FILTERS: TimeFilter[] = ["all", "1h", "24h", "7d"];

/** Span of each rolling window, in ms. "all" has no span — hence the Exclude. */
const TIME_WINDOW_MS: Record<Exclude<TimeFilter, "all">, number> = {
  "1h": 3_600_000,
  "24h": 86_400_000,
  "7d": 604_800_000,
};

/**
 * The `?since=` bound for a time filter, or `undefined` for "all time" (the
 * param is then omitted entirely).
 *
 * Lives here, beside the type and the spans, rather than being copied into each
 * page: the feed and History must agree on what "Last 24h" means, and two
 * copies of the table are two things to drift apart.
 *
 * Call it INSIDE the fetch (not in render or a memo) — the point is that every
 * request re-anchors the window to the current clock.
 */
export function sinceForTimeFilter(time: TimeFilter): string | undefined {
  if (time === "all") return undefined;
  return new Date(Date.now() - TIME_WINDOW_MS[time]).toISOString();
}

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

const SEVERITY_LABELS: Record<SeverityFilter, string> = {
  all: "All Severities",
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/**
 * Coerce an untrusted value (e.g. parsed localStorage) into valid filters,
 * falling back to the defaults for anything outside the known domain. Keeps a
 * stale or hand-edited `oil.alertFilters` entry from poisoning the UI.
 */
export function sanitizeAlertFilters(raw: unknown): AlertFilters {
  const obj = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const department = DEPARTMENT_FILTERS.includes(obj.department as DepartmentFilter)
    ? (obj.department as DepartmentFilter)
    : "all";
  const source = SOURCE_FILTERS.includes(obj.source as SourceFilter)
    ? (obj.source as SourceFilter)
    : "all";
  const level = LEVEL_FILTERS.includes(obj.level as LevelFilter)
    ? (obj.level as LevelFilter)
    : "all";
  const app_name = APP_NAME_FILTERS.includes(obj.app_name as AppNameFilter)
    ? (obj.app_name as AppNameFilter)
    : "all";
  const severity = SEVERITY_FILTERS.includes(obj.severity as SeverityFilter)
    ? (obj.severity as SeverityFilter)
    : "all";
  const cached = CACHED_FILTERS.includes(obj.cached as CachedFilter)
    ? (obj.cached as CachedFilter)
    : "all";
  // A filter bar persisted before `time` existed has no such key — it coerces to
  // "all", which is exactly the pre-feature behaviour.
  const time = TIME_FILTERS.includes(obj.time as TimeFilter)
    ? (obj.time as TimeFilter)
    : "all";
  return { department, source, level, app_name, severity, cached, time };
}

/**
 * The single source of truth for "does this alert belong in the feed under the
 * active filter?". Used both to guard live WS alerts (`alert.new`) and as the
 * mirror of the backend query — a live alert is admitted iff a re-fetch with
 * the same filters would have returned it (department AND source AND level AND
 * app_name, "all" = any). Fallback alerts have a null department, so any
 * non-"all" department filter excludes them — exactly as `Alert.department ==
 * department` does server-side.
 */
export function alertMatchesFilters(alert: ProcessedAlert, filters: AlertFilters): boolean {
  if (filters.department !== "all" && alert.department !== filters.department) return false;
  if (filters.source !== "all" && alert.source !== filters.source) return false;
  if (filters.level !== "all" && alert.level !== filters.level) return false;
  if (filters.app_name !== "all" && alert.app_name !== filters.app_name) return false;
  if (filters.severity !== "all" && alert.severity !== filters.severity) return false;
  // Mirrors `Alert.cached == cached` server-side. Fallback alerts are never
  // cache hits, so "cached" excludes them and "fresh" admits them — the same
  // partition the backend applies.
  if (filters.cached !== "all" && alert.cached !== (filters.cached === "cached")) return false;
  // `filters.time` is deliberately NOT checked, and adding it would be a bug.
  // This function guards LIVE alerts arriving over the WebSocket, which are by
  // definition "now" — and every window is `[now - span, now]`, so it always
  // includes the present. The iff-a-re-fetch-would-return-it property therefore
  // still holds: a re-fetch re-anchors `since` to the current clock, and a
  // just-emitted alert is inside every one of those windows.
  return true;
}

// --- component ---------------------------------------------------------------

interface AlertFilterBarProps {
  value: AlertFilters;
  onChange: (next: AlertFilters) => void;
}

// Option lists handed to FilterDropdown. Each pairs the value the filter model
// uses with the label already defined above, so the dropdowns and the filter
// contract cannot drift apart.
const DEPARTMENT_OPTIONS: FilterOption[] = DEPARTMENT_FILTERS.map((d) => ({
  value: d,
  label: d === "all" ? "All Departments" : capitalize(d),
}));

// app_name has no label map — the service names ARE the labels (they are the
// literal app_name values the backend stores).
const APP_NAME_OPTIONS: FilterOption[] = APP_NAME_FILTERS.map((name) => ({
  value: name,
  label: name === "all" ? "All Services" : name,
}));

const SEVERITY_OPTIONS: FilterOption[] = SEVERITY_FILTERS.map((s) => ({
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

export function AlertFilterBar({ value, onChange }: AlertFilterBarProps) {
  const isDefault = (Object.keys(DEFAULT_ALERT_FILTERS) as (keyof AlertFilters)[]).every(
    (key) => value[key] === DEFAULT_ALERT_FILTERS[key]
  );

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
        <FilterDropdown
          label="Severity"
          value={value.severity}
          defaultValue={DEFAULT_ALERT_FILTERS.severity}
          options={SEVERITY_OPTIONS}
          onChange={(v) => onChange({ ...value, severity: v as SeverityFilter })}
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
        <FilterDropdown
          label="Service"
          value={value.app_name}
          defaultValue={DEFAULT_ALERT_FILTERS.app_name}
          options={APP_NAME_OPTIONS}
          onChange={(v) => onChange({ ...value, app_name: v as AppNameFilter })}
        />
        <FilterDropdown
          label="Department"
          value={value.department}
          defaultValue={DEFAULT_ALERT_FILTERS.department}
          options={DEPARTMENT_OPTIONS}
          onChange={(v) => onChange({ ...value, department: v as DepartmentFilter })}
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
                background: "transparent",
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
