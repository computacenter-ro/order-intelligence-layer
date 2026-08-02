import type { Journey, JourneyStatus } from "@/lib/types";

/**
 * The journeys list's filter model + the predicate that mirrors the server query.
 *
 * In `lib/` rather than in the page for a concrete reason: `npm test` runs
 * `node --test 'lib/**' ` with no jsdom and no React Testing Library, so a pure
 * function here is testable with zero new infrastructure while the same logic
 * inlined in a component is not testable at all. The WebSocket gate this replaces
 * WAS a one-line inline expression (`status === "all" || event.data.status ===
 * status`); with three filters it stops being something you can verify by reading.
 */

// The ten outcome values, mirroring the module constants in backend/journeys.py
// (the "Outcome vocabulary" block: SUCCESS / TIMED_OUT / UNRECOGNIZED_FAILURE plus
// the seven scenario-specific *_FAILED strings, which themselves match
// shared/scenarios.py). Listed here the same way STATUS_OPTIONS lists the four
// statuses — the backend takes a free string, so a drift shows up as a filter that
// silently matches nothing.
export const JOURNEY_OUTCOMES = [
  "SUCCESS",
  "TIMED_OUT",
  "INBOUND_TRANSFORM_FAILED",
  "ORDER_CREATION_FAILED",
  "MARGIN_CHECK_FAILED",
  "VALIDATION_FAILED",
  "ENRICHMENT_FAILED",
  "AUTH_FAILED",
  "SAP_SUBMISSION_FAILED",
  "UNRECOGNIZED_FAILURE",
] as const;

export type JourneyOutcome = (typeof JOURNEY_OUTCOMES)[number];

/**
 * "all" is the UI-only sentinel for "no filter" (the convention the alert bar
 * uses); the page maps it to `undefined` so the query param is omitted entirely.
 */
export interface JourneyFilters {
  status: JourneyStatus | "all";
  outcome: JourneyOutcome | "all";
  search: string;
}

export const DEFAULT_JOURNEY_FILTERS: JourneyFilters = {
  status: "all",
  outcome: "all",
  search: "",
};

/**
 * True when any filter is set away from its default.
 *
 * Spelled out field by field rather than looped over the defaults — the same
 * reasoning as `hasActiveFilters` in AlertFilterBar: a loop invites reference
 * comparison bugs the moment a field stops being a primitive, and it silently
 * stops covering a field that is added without being thought about.
 *
 * Exported so the empty state can tell "no results under these filters" apart from
 * "no journeys at all", which are different messages.
 */
export function hasActiveJourneyFilters(filters: JourneyFilters): boolean {
  return (
    filters.status !== "all" ||
    filters.outcome !== "all" ||
    filters.search.trim() !== ""
  );
}

/**
 * The single source of truth for "does this journey belong in the list under the
 * active filters?".
 *
 * The contract is the alert feed's, exactly: a journey arriving live over the
 * WebSocket is admitted **if and only if** a re-fetch with the same filters would
 * have returned it. So a journey whose outcome doesn't match, or whose ids don't
 * contain the search term, must not be prepended — otherwise the list shows rows
 * that vanish on the next reload, which reads as data corruption.
 *
 * Mirroring `build_journeys_query` means mirroring three of SQL's behaviours:
 *
 * - `status` / `outcome` are exact equality, and a non-"all" selection therefore
 *   EXCLUDES nulls, as `outcome = 'X'` does. A journey still in progress has no
 *   outcome and so matches no outcome filter — which is right, and why there is no
 *   NULL bucket: `status=IN_PROGRESS` already selects exactly those.
 * - The search is case-INsensitive, because the server uses `ILIKE` rather than
 *   `LIKE`. Matching case-sensitively here would admit fewer live journeys than a
 *   re-fetch returns, so rows would appear only after a reload.
 * - A null id does not match. `ILIKE` on a NULL column yields NULL, not true, so a
 *   journey with no `order_id` is absent from an order-id search on the server too.
 */
export function journeyMatchesFilters(
  journey: Journey,
  filters: JourneyFilters
): boolean {
  if (filters.status !== "all" && journey.status !== filters.status) return false;
  if (filters.outcome !== "all" && journey.outcome !== filters.outcome) return false;

  const term = filters.search.trim().toLowerCase();
  if (term === "") return true; // blank = no filter, never a match-everything

  // The same three columns the server ORs over, in the same order.
  const ids = [journey.event_id, journey.order_id, journey.cart_header_id];
  return ids.some((id) => id != null && id.toLowerCase().includes(term));
}
