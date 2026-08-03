/**
 * Tests for the AI Performance page's freshness logic.
 *
 * Run with `npm test` (Node's built-in test runner — Node 24 executes TypeScript
 * directly, so this needs no transpiler, no jest, no vitest, no new dependency).
 *
 * The imports below carry explicit `.ts` extensions because Node resolves the real
 * file rather than a bundler alias; that is why `allowImportingTsExtensions` is set
 * in tsconfig.json, and it is also why they are relative instead of using the `@/`
 * alias (which only the bundler understands).
 *
 * Two things are under test and they are the two that can lie to the reader:
 * the age label, and the post-click message. Both render as confident English
 * sentences whatever they compute, so a wrong branch produces a plausible
 * falsehood rather than a visible bug — which is precisely why they are pure
 * functions in `lib/` instead of inline in the component.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  FEEDBACK_DISMISS_MS,
  SNAPSHOT_POLL_MS,
  STALE_INTERVALS,
  ageSeconds,
  isStale,
  secondsUntilNextUpdate,
  updateFeedback,
} from "./aiPerformance.ts";
import { formatUpdatedAt } from "./format.ts";

// A fixed frame of reference. All timestamps below are offsets from this.
const NOW = Date.parse("2026-07-30T14:32:10Z");
const INTERVAL = 60;

/** An ISO stamp `seconds` in the past relative to NOW. */
function ago(seconds: number): string {
  return new Date(NOW - seconds * 1000).toISOString();
}

// --- the age label -----------------------------------------------------------

test("under a minute reads as just now, not as 0 min", () => {
  assert.equal(formatUpdatedAt(ago(0), NOW), "Updated just now");
  assert.equal(formatUpdatedAt(ago(1), NOW), "Updated just now");
  assert.equal(formatUpdatedAt(ago(59), NOW), "Updated just now");
});

test("minutes are whole minutes", () => {
  assert.equal(formatUpdatedAt(ago(60), NOW), "Updated 1 min ago");
  assert.equal(formatUpdatedAt(ago(119), NOW), "Updated 1 min ago");
  assert.equal(formatUpdatedAt(ago(180), NOW), "Updated 3 min ago");
  assert.equal(formatUpdatedAt(ago(59 * 60), NOW), "Updated 59 min ago");
});

test("past an hour it switches to an absolute time", () => {
  // 90 minutes before 14:32:10Z. Rendered in the RUNNER's local zone, like every
  // other timestamp in the app, so the expectation is derived the same way rather
  // than hardcoded to UTC.
  const iso = ago(90 * 60);
  const expected = new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  assert.equal(formatUpdatedAt(iso, NOW), `Updated ${expected}`);
  // Not a minute count: "Updated 90 min ago" is the thing we're avoiding.
  assert.ok(!formatUpdatedAt(iso, NOW).includes("min ago"));
});

test("the hour boundary belongs to the absolute branch", () => {
  assert.equal(formatUpdatedAt(ago(3599), NOW), "Updated 59 min ago");
  assert.ok(formatUpdatedAt(ago(3600), NOW).match(/^Updated \d{2}:\d{2}$/));
});

test("a null timestamp is Collecting, never a zero age", () => {
  assert.equal(formatUpdatedAt(null, NOW), "Collecting…");
  assert.ok(!formatUpdatedAt(null, NOW).includes("0"));
});

test("an unparseable timestamp degrades to Collecting rather than NaN", () => {
  assert.equal(formatUpdatedAt("not a date", NOW), "Collecting…");
});

test("clock skew into the future clamps to just now", () => {
  // The server stamps it, the browser subtracts — a second or two of disagreement
  // between two machines is normal and must not render as a future time.
  assert.equal(formatUpdatedAt(ago(-5), NOW), "Updated just now");
});

// --- age / staleness arithmetic ---------------------------------------------

test("ageSeconds clamps and handles the unusable cases", () => {
  assert.equal(ageSeconds(ago(30), NOW), 30);
  assert.equal(ageSeconds(ago(-30), NOW), 0);
  assert.equal(ageSeconds(null, NOW), null);
  assert.equal(ageSeconds("garbage", NOW), null);
});

test("staleness triggers only past the multiple of the interval", () => {
  assert.equal(isStale(ago(INTERVAL), INTERVAL, NOW), false);
  // Just inside: the refresher is late but a cycle takes time, so this is normal.
  assert.equal(isStale(ago(INTERVAL * STALE_INTERVALS), INTERVAL, NOW), false);
  assert.equal(isStale(ago(INTERVAL * STALE_INTERVALS + 1), INTERVAL, NOW), true);
});

test("a never-collected snapshot is not stale", () => {
  // Cold start is not a failure, and calling it one would send the reader hunting
  // for a dead task seconds after a restart.
  assert.equal(isStale(null, INTERVAL, NOW), false);
});

test("staleness scales with the configured interval", () => {
  // The reason the interval crosses the wire at all: a hardcoded threshold would
  // silently mis-report the moment someone retunes the refresher after a 429.
  const age = 400;
  assert.equal(isStale(ago(age), 60, NOW), true); // 400 > 180
  assert.equal(isStale(ago(age), 600, NOW), false); // 400 < 1800
});

test("secondsUntilNextUpdate counts down and goes negative past the deadline", () => {
  assert.equal(secondsUntilNextUpdate(ago(5), INTERVAL, NOW), 55);
  assert.equal(secondsUntilNextUpdate(ago(60), INTERVAL, NOW), 0);
  assert.ok((secondsUntilNextUpdate(ago(75), INTERVAL, NOW) as number) < 0);
  assert.equal(secondsUntilNextUpdate(null, INTERVAL, NOW), null);
});

// --- the update-click state table -------------------------------------------
//
// All five rows. The asymmetry is the design: a visible delta IS the feedback, so a
// message appears only when the click produced no visual change.

test("row 1 — fetched_at changed: no message at all", () => {
  assert.equal(
    updateFeedback({
      ok: true,
      previousFetchedAt: ago(120),
      nextFetchedAt: ago(5),
      refreshIntervalS: INTERVAL,
      now: NOW,
    }),
    null
  );
});

test("row 2 — unchanged, next update still ahead: countdown", () => {
  assert.equal(
    updateFeedback({
      ok: true,
      previousFetchedAt: ago(5),
      nextFetchedAt: ago(5),
      refreshIntervalS: INTERVAL,
      now: NOW,
    }),
    "No new data yet — next update in ~55s"
  );
});

test("row 3 — unchanged and stale: the refresh may have stopped", () => {
  assert.equal(
    updateFeedback({
      ok: true,
      previousFetchedAt: ago(1000),
      nextFetchedAt: ago(1000),
      refreshIntervalS: INTERVAL,
      now: NOW,
    }),
    "No new data — the background refresh may have stopped"
  );
});

test("row 4 — a failed fetch produces NO message, ever", () => {
  // The classic bug this guards: on failure the old stats stay on screen, so
  // `fetched_at` is trivially unchanged, and a naive implementation reports
  // "no new data" when the truth is "the request failed".
  for (const stamp of [ago(5), ago(1000), null]) {
    assert.equal(
      updateFeedback({
        ok: false,
        previousFetchedAt: stamp,
        nextFetchedAt: stamp,
        refreshIntervalS: INTERVAL,
        now: NOW,
      }),
      null,
      `ok:false must be silent (stamp ${stamp})`
    );
  }
});

test("row 5 — fetched_at null: still collecting", () => {
  assert.equal(
    updateFeedback({
      ok: true,
      previousFetchedAt: null,
      nextFetchedAt: null,
      refreshIntervalS: INTERVAL,
      now: NOW,
    }),
    "Still collecting…"
  );
});

test("the countdown is never negative and never a frozen zero", () => {
  // Between the deadline and the staleness threshold the refresher is merely late —
  // which is the NORMAL state for part of every cycle, because the stamp lands at
  // the END of an ~18s cycle. Neither "-18s" nor a stuck "0s" may appear.
  for (const age of [61, 70, 90, 120, 179]) {
    const message = updateFeedback({
      ok: true,
      previousFetchedAt: ago(age),
      nextFetchedAt: ago(age),
      refreshIntervalS: INTERVAL,
      now: NOW,
    }) as string;
    assert.equal(message, "No new data yet — the next update is due", `age ${age}`);
    assert.ok(!message.includes("-"), `negative countdown at age ${age}: ${message}`);
    assert.ok(!message.includes("~0s"), `zero countdown at age ${age}: ${message}`);
  }
});

test("a late refresher is not yet reported as stopped", () => {
  // The false-alarm guard. One interval past the deadline happens every cycle; only
  // the staleness threshold may accuse the task of having died.
  const message = updateFeedback({
    ok: true,
    previousFetchedAt: ago(80),
    nextFetchedAt: ago(80),
    refreshIntervalS: INTERVAL,
    now: NOW,
  }) as string;
  assert.ok(!message.includes("may have stopped"), message);
});

test("the countdown keeps the tilde", () => {
  // The interval is a sleep BETWEEN cycles and a cycle itself takes time, so this
  // is an estimate. Dropping the tilde would make it a promise the server never made.
  const message = updateFeedback({
    ok: true,
    previousFetchedAt: ago(10),
    nextFetchedAt: ago(10),
    refreshIntervalS: INTERVAL,
    now: NOW,
  }) as string;
  assert.ok(message.includes("~"), message);
});

test("identical payloads with a NEW timestamp still count as changed", () => {
  // In a quiet period two genuine refreshes return byte-identical numbers. Comparing
  // payloads instead of stamps would report "no new data" when the refresher had in
  // fact just run — the reason this function takes timestamps and nothing else.
  assert.equal(
    updateFeedback({
      ok: true,
      previousFetchedAt: "2026-07-30T14:30:00+00:00",
      nextFetchedAt: "2026-07-30T14:31:00+00:00",
      refreshIntervalS: INTERVAL,
      now: NOW,
    }),
    null
  );
});

test("a first-ever collection landing on this click counts as changed", () => {
  assert.equal(
    updateFeedback({
      ok: true,
      previousFetchedAt: null,
      nextFetchedAt: ago(2),
      refreshIntervalS: INTERVAL,
      now: NOW,
    }),
    null
  );
});

// --- the page's timers -------------------------------------------------------

test("the page registers no one-second timer", () => {
  // A source-level guard, because the requirement is about what the component must
  // NOT do and there is no DOM/renderer here to observe it. Crude, but it fails if
  // someone reintroduces the per-second ticker this design deliberately removed.
  const source = readFileSync(new URL("../app/ai-performance/page.tsx", import.meta.url), "utf8");
  const intervals = [...source.matchAll(/setInterval\s*\(([\s\S]*?)\)\s*;/g)].map((m) => m[1]);
  assert.equal(intervals.length, 1, "expected exactly one setInterval on the page");
  assert.ok(
    intervals[0].includes("SNAPSHOT_POLL_MS"),
    `the only interval must be the snapshot poll, got: ${intervals[0]}`
  );
  assert.ok(!/\b1000\b/.test(source), "a 1000ms timer is back on the page");
});

test("the poll interval is well under the refresh period, and the dismiss is brief", () => {
  assert.ok(SNAPSHOT_POLL_MS <= 30_000, "a new snapshot should be picked up promptly");
  assert.ok(SNAPSHOT_POLL_MS >= 5_000, "polling a free endpoint still shouldn't be a firehose");
  assert.ok(FEEDBACK_DISMISS_MS >= 3_000 && FEEDBACK_DISMISS_MS <= 6_000);
});
