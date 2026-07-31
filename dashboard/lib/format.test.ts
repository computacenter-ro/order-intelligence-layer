/**
 * Tests for lib/format.ts's notification-count cap.
 *
 * Run with `npm test` (Node's built-in runner; Node executes TypeScript directly).
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { formatNotificationCount } from "./format.ts";

test("counts at or below the cap print exactly", () => {
  assert.equal(formatNotificationCount(0), "0");
  assert.equal(formatNotificationCount(1), "1");
  assert.equal(formatNotificationCount(20), "20");
});

test("counts past the cap collapse to 20+", () => {
  assert.equal(formatNotificationCount(21), "20+");
  assert.equal(formatNotificationCount(99), "20+");
  assert.equal(formatNotificationCount(231), "20+");
  assert.equal(formatNotificationCount(9999), "20+");
});
