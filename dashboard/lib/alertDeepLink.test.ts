/**
 * Tests for the `/?alert=<alert_id>` deep-link param.
 *
 * Run with `npm test` (Node's built-in runner; Node executes TypeScript directly).
 *
 * This value crosses a trust boundary: it comes from a URL — pasted, edited, or
 * years-old in a Teams channel — and is interpolated into an API path. Rejecting
 * junk up front means the page shows no drawer, rather than firing a request that
 * cannot succeed. The failure mode being guarded is quiet: a param that parses to
 * something plausible but wrong produces an "alert could not be loaded" message
 * that looks like a backend fault.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { ALERT_PARAM, alertIdFromParam } from "./alertDeepLink.ts";

test("the param name matches the one the Teams card builds", () => {
  // backend/teams.py::_alert_link builds `{DASHBOARD_URL}/?alert=<alert_id>`.
  // If these ever disagree, every card in every channel becomes a dead link, so
  // the literal is pinned on both sides.
  assert.equal(ALERT_PARAM, "alert");
});

test("a uuid alert_id round-trips", () => {
  // The real shape: ProcessedAlert.alert_id is str(uuid4()).
  const id = "3f2b1c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
  assert.equal(alertIdFromParam(id), id);
});

test("the short fixture/test id shape round-trips", () => {
  assert.equal(alertIdFromParam("al-1"), "al-1");
});

test("surrounding whitespace is trimmed", () => {
  // Copy-pasting a URL out of a chat message picks up spaces surprisingly often.
  assert.equal(alertIdFromParam("  al-1  "), "al-1");
});

test("an absent or empty param opens no drawer", () => {
  assert.equal(alertIdFromParam(null), null);
  assert.equal(alertIdFromParam(undefined), null);
  assert.equal(alertIdFromParam(""), null);
  assert.equal(alertIdFromParam("   "), null);
});

test("a path separator is refused, not escaped", () => {
  // The id is interpolated into `/alerts/{id}`. `..%2f` style input has no
  // legitimate reading, so it is rejected here rather than encoded and sent.
  assert.equal(alertIdFromParam("../journeys/J1"), null);
  assert.equal(alertIdFromParam("al-1/extra"), null);
  assert.equal(alertIdFromParam("%2e%2e%2fadmin"), null);
});

test("query and fragment characters are refused", () => {
  assert.equal(alertIdFromParam("al-1&department=backend"), null);
  assert.equal(alertIdFromParam("al-1?x=1"), null);
  assert.equal(alertIdFromParam("al-1#frag"), null);
});

test("an absurdly long value is refused", () => {
  // No real alert_id is anywhere near this; the cap keeps a pathological URL from
  // becoming a pathological request path.
  assert.equal(alertIdFromParam("a".repeat(129)), null);
  assert.equal(alertIdFromParam("a".repeat(128))?.length, 128);
});

test("the accepted character set is exactly what an id can contain", () => {
  for (const ok of ["abc", "ABC", "123", "a-b", "a_b", "a.b", "al-1"]) {
    assert.equal(alertIdFromParam(ok), ok, `should accept ${ok}`);
  }
  for (const bad of ["a b", "a+b", "a@b", "a:b", "a;b", "a,b", "<a>", "a'b", 'a"b']) {
    assert.equal(alertIdFromParam(bad), null, `should reject ${bad}`);
  }
});
