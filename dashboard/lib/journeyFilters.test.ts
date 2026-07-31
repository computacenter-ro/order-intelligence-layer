/**
 * Tests for the journeys list's filter predicate.
 *
 * Run with `npm test` (Node's built-in runner; Node executes TypeScript directly).
 *
 * What matters here is the contract this predicate shares with the server: a
 * journey arriving live over the WebSocket is admitted **iff** a re-fetch with the
 * same filters would have returned it. Every divergence shows up as rows that
 * appear and then vanish on reload (predicate too loose) or that only appear after
 * a reload (too strict) — both of which read as a broken list rather than as a
 * filter bug, so they are worth pinning directly.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_JOURNEY_FILTERS,
  JOURNEY_OUTCOMES,
  hasActiveJourneyFilters,
  journeyMatchesFilters,
  type JourneyFilters,
} from "./journeyFilters.ts";
import type { Journey } from "./types.ts";

function journey(over: Partial<Journey> = {}): Journey {
  return {
    journey_id: "J1",
    status: "SUCCESS",
    outcome: "SUCCESS",
    first_ts: "2026-07-30T08:00:00Z",
    last_ts: "2026-07-30T08:00:05Z",
    event_id: "evt-372656a7-aaaa",
    order_id: "ORD-6001",
    cart_header_id: "1840927365018240001",
    summary: "all good",
    ...over,
  } as Journey;
}

function filters(over: Partial<JourneyFilters> = {}): JourneyFilters {
  return { ...DEFAULT_JOURNEY_FILTERS, ...over };
}

// --- "all" / blank means no filter -------------------------------------------

test("the default filters admit everything", () => {
  assert.equal(journeyMatchesFilters(journey(), DEFAULT_JOURNEY_FILTERS), true);
  // Including a journey with no ids and no outcome at all — the pre-creation case
  // that has only an event id, and the in-progress case with no outcome.
  assert.equal(
    journeyMatchesFilters(
      journey({ outcome: null, order_id: null, cart_header_id: null }),
      DEFAULT_JOURNEY_FILTERS
    ),
    true
  );
});

test("all is a sentinel, not a matchable value", () => {
  // Guards the mistake of comparing the sentinel against the column: a journey
  // whose outcome is literally the string "all" is not what "All Outcomes" means,
  // and more importantly every OTHER journey must still pass.
  assert.equal(journeyMatchesFilters(journey({ outcome: "TIMED_OUT" }), filters()), true);
});

// --- status ------------------------------------------------------------------

test("a non-matching status is rejected", () => {
  assert.equal(
    journeyMatchesFilters(journey({ status: "SUCCESS" }), filters({ status: "FAILED" })),
    false
  );
  assert.equal(
    journeyMatchesFilters(journey({ status: "FAILED" }), filters({ status: "FAILED" })),
    true
  );
});

// --- outcome -----------------------------------------------------------------

test("a non-matching outcome is rejected", () => {
  assert.equal(
    journeyMatchesFilters(
      journey({ outcome: "MARGIN_CHECK_FAILED" }),
      filters({ outcome: "AUTH_FAILED" })
    ),
    false
  );
});

test("a matching outcome is admitted", () => {
  assert.equal(
    journeyMatchesFilters(
      journey({ outcome: "SAP_SUBMISSION_FAILED" }),
      filters({ outcome: "SAP_SUBMISSION_FAILED" })
    ),
    true
  );
});

test("a null outcome matches no outcome filter", () => {
  // Mirrors SQL: `outcome = 'X'` excludes NULLs. An in-progress journey has no
  // outcome, and `status=IN_PROGRESS` is how you select those — which is why there
  // is deliberately no NULL bucket in the dropdown.
  for (const outcome of JOURNEY_OUTCOMES) {
    assert.equal(
      journeyMatchesFilters(journey({ outcome: null }), filters({ outcome })),
      false,
      outcome
    );
  }
});

test("every outcome in the dropdown is matchable", () => {
  // Catches a typo in the hand-maintained list mirroring backend/journeys.py: a
  // misspelled value would be a filter that silently matches nothing.
  for (const outcome of JOURNEY_OUTCOMES) {
    assert.equal(journeyMatchesFilters(journey({ outcome }), filters({ outcome })), true, outcome);
  }
});

// --- search ------------------------------------------------------------------

test("search matches on each of the three ids", () => {
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "ORD-6001" })), true);
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "1840927365018240001" })), true);
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "372656a7" })), true);
});

test("search is a substring match, not equality", () => {
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "6001" })), true);
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "ORD-" })), true);
});

test("a term matching none of the ids is rejected", () => {
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "ORD-9999" })), false);
});

test("search is case-insensitive, like the backend's ILIKE", () => {
  // The server uses ILIKE, so a case-SENSITIVE check here would admit fewer live
  // journeys than a re-fetch returns, and rows would appear only after a reload.
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "ord-6001" })), true);
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "EVT-372656A7" })), true);
});

test("a null id does not match", () => {
  // ILIKE on a NULL column yields NULL, not true, so the server omits this row
  // too. A journey that never got an order id genuinely has none.
  const j = journey({ order_id: null, cart_header_id: null });
  assert.equal(journeyMatchesFilters(j, filters({ search: "ORD-6001" })), false);
  // ...but it is still findable by the id it does have.
  assert.equal(journeyMatchesFilters(j, filters({ search: "372656a7" })), true);
});

test("a journey with no ids at all matches no search", () => {
  const j = journey({ event_id: null, order_id: null, cart_header_id: null });
  assert.equal(journeyMatchesFilters(j, filters({ search: "anything" })), false);
  // And is still admitted when nothing is being searched.
  assert.equal(journeyMatchesFilters(j, filters()), true);
});

test("a blank search applies no filter", () => {
  for (const search of ["", "   ", "\t\n"]) {
    assert.equal(
      journeyMatchesFilters(journey(), filters({ search })),
      true,
      JSON.stringify(search)
    );
  }
});

test("the search term is trimmed, matching the server", () => {
  assert.equal(journeyMatchesFilters(journey(), filters({ search: "  ORD-6001  " })), true);
});

// --- filters combine with AND ------------------------------------------------

test("the three filters AND together", () => {
  const j = journey({ status: "FAILED", outcome: "AUTH_FAILED" });
  const all: JourneyFilters = { status: "FAILED", outcome: "AUTH_FAILED", search: "ORD-6001" };
  assert.equal(journeyMatchesFilters(j, all), true);
  // Each one alone is enough to reject — the AND must not degrade into an OR.
  assert.equal(journeyMatchesFilters(j, { ...all, status: "SUCCESS" }), false);
  assert.equal(journeyMatchesFilters(j, { ...all, outcome: "TIMED_OUT" }), false);
  assert.equal(journeyMatchesFilters(j, { ...all, search: "ORD-9999" }), false);
});

// --- hasActiveJourneyFilters --------------------------------------------------

test("hasActiveJourneyFilters is false only for the defaults", () => {
  assert.equal(hasActiveJourneyFilters(DEFAULT_JOURNEY_FILTERS), false);
  assert.equal(hasActiveJourneyFilters(filters({ status: "FAILED" })), true);
  assert.equal(hasActiveJourneyFilters(filters({ outcome: "SUCCESS" })), true);
  assert.equal(hasActiveJourneyFilters(filters({ search: "ORD" })), true);
});

test("a whitespace-only search does not count as an active filter", () => {
  // It sends no query param, so the empty state must not blame filters for it.
  assert.equal(hasActiveJourneyFilters(filters({ search: "   " })), false);
});
