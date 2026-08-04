/**
 * The alert filter MODEL — types, domain lists, and the pure functions over them.
 *
 * Extracted from `components/alerts/AlertFilterBar.tsx` (which still re-exports
 * every name here, so no call site changed) for one reason: `npm test` runs
 * Node's built-in runner over `lib/**` and Node cannot strip JSX, so nothing
 * inside a `.tsx` is reachable from a test. This logic mirrors a server contract
 * in four places and sanitises untrusted persisted input — exactly the kind of
 * thing that needs pinning. Same split as `lib/journeyFilters.ts`.
 *
 * The component keeps what is genuinely presentational: the labels, the dropdown
 * option lists, and the bar itself.
 *
 * "all" is the UI-only sentinel meaning "no filter"; `toAlertsQuery` maps it to
 * `undefined` so the query param is omitted entirely. The concrete values mirror
 * the backend contract exactly — Department values, ProcessedAlert.source, the
 * WARN/ERROR levels, and app_name — so a selection round-trips to /alerts
 * unchanged.
 */
import type { AlertsFilter } from "@/lib/api";
import type { Department, ProcessedAlert, Severity } from "@/lib/types";

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

// The five Department values (shared/models.py). `business` was called `general`
// until the department was renamed to match the Teams channel it routes to — so a
// filter persisted before that rename holds a value that no longer exists, which
// is precisely what `sanitizeAlertFilters` drops. Adding a stale value back here
// would send it to an enum-typed query param and earn a 422.
export const DEPARTMENTS: Department[] = [
  "networking",
  "devops",
  "backend",
  "database",
  "business",
];

// Fixed roster of the pipeline's emitters (CLAUDE.md [1] Services). app_name is
// a free string server-side, but the UI offers this closed list so the control
// is a dropdown rather than free text.
export const APP_NAMES = [
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

export const SOURCE_FILTERS: SourceFilter[] = ["all", "ai", "fallback"];
export const LEVEL_FILTERS: LevelFilter[] = ["all", "WARN", "ERROR"];

// Router LLM severities (shared/models.py Severity), most→least urgent. No "all"
// entry: for the multi-selects, "none ticked" already means all.
export const SEVERITIES: Severity[] = ["critical", "high", "medium", "low"];
export const CACHED_FILTERS: CachedFilter[] = ["all", "cached", "fresh"];
export const TIME_FILTERS: TimeFilter[] = ["all", "1h", "24h", "7d"];

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
  //
  // Dropping unknown MEMBERS (rather than keeping them, or discarding the whole
  // blob) is what makes a renamed department survivable: a user who had `general`
  // ticked before the general -> business rename would otherwise send
  // `?department=general` to an enum-typed query param and get a 422 — a feed that
  // is simply broken, with nothing on screen hinting that stale storage is why.
  // The cost of dropping is one un-ticked box, which is visible and self-fixing.
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
