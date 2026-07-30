"use client";

import { useEffect, useId, useRef, useState } from "react";
import { CaretDownIcon, CaretUpIcon } from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { capitalize } from "@/lib/format";
import type { AlertFacets, AlertsFilter } from "@/lib/api";
import { FilterDropdown, type FilterOption } from "@/components/alerts/FilterDropdown";
import { MultiFilterDropdown } from "@/components/alerts/MultiFilterDropdown";
import { SearchInput } from "@/components/alerts/SearchInput";
import type { Department, ProcessedAlert, Severity } from "@/lib/types";

// --- filter model ------------------------------------------------------------
//
// "all" is the UI-only sentinel meaning "no filter"; the page maps it to
// `undefined` before calling fetchAlerts (which omits the query param entirely).
// The concrete values mirror the backend contract exactly — Department values,
// ProcessedAlert.source, the WARN/ERROR levels, and app_name — so a selection
// round-trips to /alerts unchanged.

// department / severity / app_name are MULTI-select: arrays of concrete values
// with NO "all" sentinel. An empty array means "no filter" — the same convention
// the backend uses (build_alerts_query treats [] and None alike), so "nothing
// ticked" shows everything rather than matching nothing.
export type AppNameFilter = (typeof APP_NAMES)[number];
// Single-select filters keep the "all" sentinel.
export type SourceFilter = "all" | "ai" | "fallback";
export type LevelFilter = "all" | "WARN" | "ERROR";
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
  // Multi-select; [] = no filter.
  department: Department[];
  severity: Severity[];
  app_name: AppNameFilter[];
  // Single-select; "all" = no filter.
  source: SourceFilter;
  level: LevelFilter;
  cached: CachedFilter;
  time: TimeFilter;
  // Free-text substring search over message OR explanation, matched server-side
  // (ILIKE). "" = no filter. Deliberately NOT restored from localStorage — see
  // the rehydration comment in the pages.
  search: string;
}

export const DEFAULT_ALERT_FILTERS: AlertFilters = {
  department: [],
  severity: [],
  app_name: [],
  source: "all",
  level: "all",
  cached: "all",
  time: "all",
  search: "",
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

const SOURCE_FILTERS: SourceFilter[] = ["all", "ai", "fallback"];
const LEVEL_FILTERS: LevelFilter[] = ["all", "WARN", "ERROR"];

// Router LLM severities (shared/models.py Severity), most→least urgent. No "all"
// entry: for the multi-selects, "none ticked" already means all.
const SEVERITIES: Severity[] = ["critical", "high", "medium", "low"];
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

/**
 * Map the UI filter model onto the API query for BOTH the list and the facet
 * counts.
 *
 * One mapping, four call sites (feed + History, each fetching a list and its
 * counts). Copies would drift, and a drift here is subtle rather than loud: the
 * counts would describe a slightly different query than the list they annotate —
 * numbers that look authoritative and are quietly wrong. The backend guards the
 * same hazard with a test pinning `/alerts` and `/alerts/facets` to one param set.
 *
 * `resolved` is the caller's, because it is the one thing the two pages genuinely
 * disagree on: the feed shows open alerts, History resolved ones.
 *
 * Call this per request, never memoize it: `sinceForTimeFilter` re-anchors the
 * rolling time window to the current clock each time it runs.
 */
export function toAlertsQuery(filters: AlertFilters, resolved: boolean): AlertsFilter {
  return {
    // Multi-select arrays go through as-is: empty = no filter, several values =
    // one IN (...) server-side. No "all" sentinel to unmap for these three.
    department: filters.department,
    app_name: filters.app_name,
    severity: filters.severity,
    // Single-select: "all" is the UI-only sentinel, mapped to an omitted param.
    source: filters.source === "all" ? undefined : filters.source,
    level: filters.level === "all" ? undefined : filters.level,
    // "all" omits the param; otherwise "cached" -> true, "fresh" -> false.
    cached: filters.cached === "all" ? undefined : filters.cached === "cached",
    since: sinceForTimeFilter(filters.time),
    // Trimmed, and blank collapses to undefined so the param is omitted rather
    // than sent as an empty string (the backend ignores blanks either way, but a
    // clean URL makes the request log readable).
    search: filters.search.trim() || undefined,
    resolved,
  };
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

const SEVERITY_LABELS: Record<Severity, string> = {
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
function coerceList<T extends string>(raw: unknown, allowed: readonly T[]): T[] {
  // Not an array (including the single strings persisted by the pre-multi-select
  // version of this bar) -> no filter. Unknown members are dropped rather than
  // rejecting the whole list, and duplicates are collapsed so a hand-edited entry
  // can't produce "Severity 3" over two real values.
  if (!Array.isArray(raw)) return [];
  const seen = new Set<T>();
  for (const item of raw) {
    if (allowed.includes(item as T)) seen.add(item as T);
  }
  return [...seen];
}

export function sanitizeAlertFilters(raw: unknown): AlertFilters {
  const obj = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  // Multi-select: coerce to a list of known values, defaulting to [] (no filter).
  // A blob saved when these were single-valued holds a string like "backend" or
  // the old "all" sentinel — both are not arrays, so both land on [], which is
  // the honest reading of "we can't trust this, filter nothing".
  const department = coerceList(obj.department, DEPARTMENTS);
  const severity = coerceList(obj.severity, SEVERITIES);
  const app_name = coerceList(obj.app_name, APP_NAMES);
  const source = SOURCE_FILTERS.includes(obj.source as SourceFilter)
    ? (obj.source as SourceFilter)
    : "all";
  const level = LEVEL_FILTERS.includes(obj.level as LevelFilter)
    ? (obj.level as LevelFilter)
    : "all";
  const cached = CACHED_FILTERS.includes(obj.cached as CachedFilter)
    ? (obj.cached as CachedFilter)
    : "all";
  // A filter bar persisted before `time` existed has no such key — it coerces to
  // "all", which is exactly the pre-feature behaviour.
  const time = TIME_FILTERS.includes(obj.time as TimeFilter)
    ? (obj.time as TimeFilter)
    : "all";
  // Coerced to a string, defaulting to "". Note the pages force this back to ""
  // when rehydrating, so a persisted search never reappears — this coercion is
  // about not letting a non-string into the model, not about restoring it.
  const search = typeof obj.search === "string" ? obj.search : "";
  return { department, severity, app_name, source, level, cached, time, search };
}

/**
 * The single source of truth for "does this alert belong in the feed under the
 * active filter?". Used both to guard live WS alerts (`alert.new`) and as the
 * mirror of the backend query — a live alert is admitted iff a re-fetch with
 * the same filters would have returned it: AND across categories, OR within a
 * multi-select category (the server's `IN`), with an empty list meaning "any".
 * Fallback alerts have a null department and null severity, so any non-empty
 * department/severity filter excludes them — exactly as SQL `IN` does.
 */
export function alertMatchesFilters(alert: ProcessedAlert, filters: AlertFilters): boolean {
  // Multi-select: an empty list is no filter; a non-empty one is OR-within-category
  // and — like SQL IN — excludes nulls. So filtering by any department drops
  // fallback alerts (they have none), matching the server exactly.
  if (
    filters.department.length > 0 &&
    (alert.department === null || !filters.department.includes(alert.department as Department))
  ) {
    return false;
  }
  if (
    filters.severity.length > 0 &&
    (alert.severity === null || !filters.severity.includes(alert.severity as Severity))
  ) {
    return false;
  }
  if (
    filters.app_name.length > 0 &&
    !filters.app_name.includes(alert.app_name as AppNameFilter)
  ) {
    return false;
  }
  if (filters.source !== "all" && alert.source !== filters.source) return false;
  if (filters.level !== "all" && alert.level !== filters.level) return false;
  // Mirrors `Alert.cached == cached` server-side. Fallback alerts are never
  // cache hits, so "cached" excludes them and "fresh" admits them — the same
  // partition the backend applies.
  if (filters.cached !== "all" && alert.cached !== (filters.cached === "cached")) return false;
  // Mirrors the server's ILIKE over message OR explanation. Case-insensitive on
  // both sides, and a live alert that doesn't contain the term is excluded — a
  // search result set must not gain rows that don't match just because they
  // arrived over the WebSocket. `explanation` is null on fallback alerts, so it
  // contributes nothing and the message alone decides, exactly as `ILIKE` on NULL
  // does server-side.
  const term = filters.search.trim().toLowerCase();
  if (term !== "") {
    // Each field tested separately, NOT against a joined string: concatenating
    // them would let a term straddle the boundary (message ending "…failed" plus
    // an explanation starting "The …" would match "failed the"), which the
    // server's OR-of-two-ILIKEs never would.
    const inMessage = alert.message.toLowerCase().includes(term);
    const inExplanation = (alert.explanation ?? "").toLowerCase().includes(term);
    if (!inMessage && !inExplanation) return false;
  }
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
export function hasActiveFilters(value: AlertFilters): boolean {
  return (
    value.department.length > 0 ||
    value.severity.length > 0 ||
    value.app_name.length > 0 ||
    value.source !== "all" ||
    value.level !== "all" ||
    value.cached !== "all" ||
    value.time !== "all" ||
    value.search.trim() !== ""
  );
}
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
