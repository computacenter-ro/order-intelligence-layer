/**
 * Tests for the alert filter model — specifically the sanitisation of persisted
 * filters, which is what stands between a renamed backend value and a broken feed.
 *
 * Run with `npm test` (Node's built-in runner; Node executes TypeScript directly).
 *
 * The hazard is asymmetric and delayed. Filter selections live in localStorage
 * (`oil.alertFilters` / `oil.historyFilters`) on each user's machine, so they
 * outlive any deploy: when `Department.general` was renamed to `business`, every
 * user who had "General" ticked still had `["general"]` on disk. That value is sent
 * as `?department=general` to a query param typed as the `Department` enum, which
 * answers 422 — so the feed shows nothing, with no hint that stale storage is the
 * cause. Dropping unknown values on load costs one un-ticked box instead.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_ALERT_FILTERS,
  DEPARTMENTS,
  SEVERITIES,
  alertMatchesFilters,
  hasActiveFilters,
  sanitizeAlertFilters,
  toAlertsQuery,
  type AlertFilters,
  // Relative specifier, not the "@/" alias: Node resolves this at runtime and
  // knows nothing about tsconfig paths. Type-only imports are erased, so those
  // may use the alias (see ./journeyFilters.test.ts).
} from "./alertFilters.ts";
import type { ProcessedAlert } from "./types.ts";

// --- the department roster ----------------------------------------------------

test("the department roster is the five backend Department values", () => {
  // Pinned against shared/models.py. `business` (was `general`) is a department;
  // `fallback` and `general` are Teams CHANNELS and must never appear here.
  assert.deepEqual(DEPARTMENTS, [
    "networking",
    "devops",
    "backend",
    "database",
    "business",
  ]);
  assert.ok(!DEPARTMENTS.includes("general" as never));
  assert.ok(!DEPARTMENTS.includes("fallback" as never));
});

// --- sanitisation of persisted filters ---------------------------------------

test("a persisted department that no longer exists is dropped", () => {
  // The exact shape left on disk by a user who filtered on the old "General".
  const stored = { ...DEFAULT_ALERT_FILTERS, department: ["general"] };
  const filters = sanitizeAlertFilters(stored);
  assert.deepEqual(filters.department, []);
  // Dropped to "no filter", NOT to something that would still 422.
  assert.equal(hasActiveFilters(filters), false);
  assert.deepEqual(toAlertsQuery(filters, false).department, []);
});

test("the renamed value survives alongside the still-valid ones", () => {
  // Partial staleness is the common case: one bad member must not discard the
  // user's other, still-meaningful selections.
  const filters = sanitizeAlertFilters({
    department: ["backend", "general", "devops"],
  });
  assert.deepEqual(filters.department, ["backend", "devops"]);
});

test("the new value is accepted", () => {
  const filters = sanitizeAlertFilters({ department: ["business"] });
  assert.deepEqual(filters.department, ["business"]);
  assert.equal(hasActiveFilters(filters), true);
});

test("unknown values are dropped across every multi-select, not just department", () => {
  const filters = sanitizeAlertFilters({
    department: ["general", "nonsense"],
    severity: ["critical", "catastrophic"],
    app_name: ["cc-spt-service", "cc-retired-service"],
  });
  assert.deepEqual(filters.department, []);
  assert.deepEqual(filters.severity, ["critical"]);
  assert.deepEqual(filters.app_name, ["cc-spt-service"]);
});

test("every roster value round-trips through sanitisation", () => {
  // Guards the reverse mistake: a sanitiser that drops values it should keep is
  // just as broken, and equally quiet.
  assert.deepEqual(sanitizeAlertFilters({ department: DEPARTMENTS }).department, DEPARTMENTS);
  assert.deepEqual(sanitizeAlertFilters({ severity: SEVERITIES }).severity, SEVERITIES);
});

test("a non-array department (the pre-multi-select shape) means no filter", () => {
  assert.deepEqual(sanitizeAlertFilters({ department: "general" }).department, []);
  assert.deepEqual(sanitizeAlertFilters({ department: "all" }).department, []);
});

test("garbage input yields the defaults rather than throwing", () => {
  assert.deepEqual(sanitizeAlertFilters(null), DEFAULT_ALERT_FILTERS);
  assert.deepEqual(sanitizeAlertFilters("nope"), DEFAULT_ALERT_FILTERS);
  assert.deepEqual(sanitizeAlertFilters({}), DEFAULT_ALERT_FILTERS);
});

test("duplicates are collapsed", () => {
  const filters = sanitizeAlertFilters({ department: ["business", "business"] });
  assert.deepEqual(filters.department, ["business"]);
});

// --- the live-alert predicate still mirrors the query ------------------------

function alert(over: Partial<ProcessedAlert> = {}): ProcessedAlert {
  return {
    alert_id: "al-1",
    emitted_at: "2026-08-03T08:00:00Z",
    log_id: "log-1",
    level: "ERROR",
    app_name: "cc-checker-service",
    logger: "c.c.checker.MarginService",
    message: "Margin check FAILED for order ORD-6042",
    event_id: null,
    order_id: "ORD-6042",
    cart_header_id: null,
    account_number: "81036533",
    explanation: "The order was rejected because its margin is below threshold.",
    department: "business",
    severity: "medium",
    source: "ai",
    cached: false,
    journey_id: "J1",
    incident_id: null,
    is_resolved: false,
    resolved_at: null,
    ...over,
  } as ProcessedAlert;
}

test("a business-department filter admits a business alert", () => {
  const filters: AlertFilters = { ...DEFAULT_ALERT_FILTERS, department: ["business"] };
  assert.equal(alertMatchesFilters(alert(), filters), true);
  assert.equal(alertMatchesFilters(alert({ department: "backend" }), filters), false);
});

test("any department filter still excludes fallback alerts (they have none)", () => {
  // Mirrors SQL IN, which never matches NULL — the property the backend relies on.
  const filters: AlertFilters = { ...DEFAULT_ALERT_FILTERS, department: ["business"] };
  const fallback = alert({ source: "fallback", department: null, explanation: null, severity: null });
  assert.equal(alertMatchesFilters(fallback, filters), false);
  assert.equal(alertMatchesFilters(fallback, DEFAULT_ALERT_FILTERS), true);
});
