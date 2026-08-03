/**
 * Tests for the journeys pipeline trail (lib/pipeline.ts) under the five-hop
 * pipeline.
 *
 * Run with `npm test` (Node's built-in test runner — Node executes TypeScript
 * directly; relative import with an explicit `.ts` extension for the same
 * reason as aiPerformance.test.ts).
 *
 * What is pinned here and why:
 *  - the first-touch stage order, with Track & Trace MID-flow and the repeated
 *    `inbound-ack` terminal row (the flow ends back at Inbound);
 *  - the two-caller warn model: Inbound owns the pre-creation leg, so
 *    AUTH_FAILED warns *inbound*; the OE-called failures warn *order-engine*;
 *    and the three no-warn outcomes stay no-warn;
 *  - the terminal row is status-derived only — never a fail/warn stage.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CANONICAL_STAGES,
  OUTCOME_STAGES,
  pipelineStages,
} from "./pipeline.ts";
import type { Journey, JourneyEvent, JourneyStatus } from "./types.ts";

function journey(
  status: JourneyStatus,
  outcome: string | null,
  appNames: string[]
): Journey {
  return {
    journey_id: "j-1",
    status,
    outcome,
    first_ts: "2026-07-31T08:00:00.000Z",
    last_ts: "2026-07-31T08:01:00.000Z",
    event_id: "evt-1",
    order_id: null,
    cart_header_id: null,
    summary: null,
    events: appNames.map((app_name, i) => ({
      log_id: `log-${i}`,
      ts: "2026-07-31T08:00:00.000Z",
      // Only app_name matters to the trail; the rest of LogLine is irrelevant.
      raw: { app_name } as JourneyEvent["raw"],
    })),
  };
}

/** stage label -> state, for readable assertions. */
function states(j: Journey): Record<string, string> {
  return Object.fromEntries(pipelineStages(j).map((s) => [s.label, s.state]));
}

// App-name streams for common journey shapes (order matters only for the last
// event, which drives the fallback attribution).
const UK_SUCCESS = [
  "cc-inbound-service", "cc-order-engine", "cc-settings-service",
  "cc-jam-service", "cc-solr-service", "cc-order-engine", "cc-track-trace",
  "cc-spt-service", "cc-rsm-service", "cc-validator-service",
  "cc-checker-service", "cc-outbound-osw", "cc-inbound-service",
];
const US_SUCCESS = [...UK_SUCCESS.slice(0, -2), "cc-avalara-service",
  "cc-outbound-osw", "cc-inbound-service"];

test("stage order is first-touch with track-trace mid-flow and a terminal inbound-ack row", () => {
  assert.deepEqual(
    CANONICAL_STAGES.map((s) => s.label),
    [
      "inbound", "order-engine", "jam", "solr", "track-trace",
      "spt", "rsm", "validator", "avalara", "checker", "outbound-osw",
      "inbound-ack",
    ]
  );
  // No Settings row — settings failures are the deliberately-unrecognized
  // anomaly, attributed to Inbound via the last-event fallback (see the
  // header comment in lib/pipeline.ts).
  assert.ok(!CANONICAL_STAGES.some((s) => s.appName === "cc-settings-service"));
});

test("SUCCESS (UK): everything done, avalara honestly skipped, terminal done", () => {
  const s = states(journey("SUCCESS", "SUCCESS", UK_SUCCESS));
  assert.equal(s["inbound-ack"], "done");
  assert.equal(s["avalara"], "skipped");
  for (const label of ["inbound", "order-engine", "jam", "solr", "track-trace",
    "spt", "rsm", "validator", "checker", "outbound-osw"]) {
    assert.equal(s[label], "done", label);
  }
});

test("SUCCESS (US): avalara done when its events exist", () => {
  const s = states(journey("SUCCESS", "SUCCESS", US_SUCCESS));
  assert.equal(s["avalara"], "done");
  assert.equal(s["inbound-ack"], "done");
});

test("AUTH_FAILED: jam red, INBOUND warned (not order-engine)", () => {
  const s = states(journey("FAILED", "AUTH_FAILED", [
    "cc-inbound-service", "cc-order-engine", "cc-settings-service",
    "cc-jam-service", "cc-inbound-service",
  ]));
  assert.equal(s["jam"], "stopped");
  assert.equal(s["inbound"], "warned");
  assert.equal(s["order-engine"], "done");
  assert.equal(s["solr"], "skipped");
  assert.equal(s["track-trace"], "skipped");
  assert.equal(s["inbound-ack"], "skipped");
});

test("OE-called failures warn order-engine; earlier stages incl. track-trace read done", () => {
  for (const [outcome, fail] of [
    ["ENRICHMENT_FAILED", "spt"],
    ["VALIDATION_FAILED", "validator"],
    ["MARGIN_CHECK_FAILED", "checker"],
  ] as const) {
    const s = states(journey("FAILED", outcome, UK_SUCCESS.slice(0, 7)));
    assert.equal(s[fail], "stopped", outcome);
    assert.equal(s["order-engine"], "warned", outcome);
    assert.equal(s["inbound"], "done", outcome);
    assert.equal(s["track-trace"], "done", outcome);
    assert.equal(s["inbound-ack"], "skipped", outcome);
  }
});

test("the three no-warn outcomes paint exactly one non-green stage", () => {
  const cases: [string, string, string[]][] = [
    ["INBOUND_TRANSFORM_FAILED", "inbound", ["cc-inbound-service"]],
    ["ORDER_CREATION_FAILED", "order-engine",
      ["cc-inbound-service", "cc-order-engine", "cc-settings-service",
       "cc-jam-service", "cc-solr-service", "cc-order-engine"]],
    ["SAP_SUBMISSION_FAILED", "outbound-osw", UK_SUCCESS.slice(0, 12)],
  ];
  for (const [outcome, fail, apps] of cases) {
    const all = pipelineStages(journey("FAILED", outcome, apps));
    assert.ok(!all.some((s) => s.state === "warned"), `${outcome} must warn nobody`);
    const stopped = all.filter((s) => s.state === "stopped").map((s) => s.label);
    assert.deepEqual(stopped, [fail], outcome);
  }
});

test("UNRECOGNIZED_FAILURE (the Settings anomaly) falls back to the last event's app — Inbound", () => {
  // The settings failure line is an Inbound-identity client log, so the
  // journey's last event is cc-inbound-service and the anomaly surfaces on
  // the Inbound row (there is no Settings row by design).
  const s = states(journey("FAILED", "UNRECOGNIZED_FAILURE", [
    "cc-inbound-service", "cc-order-engine", "cc-inbound-service",
  ]));
  assert.equal(s["inbound"], "stopped");
  assert.equal(s["inbound-ack"], "skipped");
});

test("IN_PROGRESS: current at the last event's stage, pending after, terminal pending", () => {
  const s = states(journey("IN_PROGRESS", null, UK_SUCCESS.slice(0, 9)));
  assert.equal(s["rsm"], "current");
  assert.equal(s["spt"], "done");
  assert.equal(s["validator"], "pending");
  assert.equal(s["inbound-ack"], "pending");
});

test("TIMED_OUT: stalled at the last event's stage", () => {
  const s = states(journey("TIMED_OUT", "TIMED_OUT", UK_SUCCESS.slice(0, 8)));
  assert.equal(s["spt"], "stalled");
  assert.equal(s["inbound-ack"], "skipped");
});

test("the terminal row is never a fail or warn stage", () => {
  for (const { failStage, warnStage } of Object.values(OUTCOME_STAGES)) {
    assert.notEqual(failStage, "inbound-ack");
    assert.notEqual(warnStage, "inbound-ack");
  }
});

test("every OUTCOME_STAGES entry names real stage labels", () => {
  const labels = new Set(CANONICAL_STAGES.map((s) => s.label));
  for (const [outcome, { failStage, warnStage }] of Object.entries(OUTCOME_STAGES)) {
    assert.ok(labels.has(failStage), `${outcome}: unknown failStage ${failStage}`);
    if (warnStage !== undefined) {
      assert.ok(labels.has(warnStage), `${outcome}: unknown warnStage ${warnStage}`);
    }
  }
});
