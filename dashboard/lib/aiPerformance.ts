/**
 * Pure logic behind the AI Performance page's freshness UI.
 *
 * Split out of `app/ai-performance/page.tsx` because this is where the page's real
 * decisions live — whether the background refresher looks dead, and what (if
 * anything) to tell the reader after they click Update. Every one of those
 * decisions has an off-by-one or a wrong-branch failure that renders as a
 * confident, plausible sentence, so they are worth testing directly rather than
 * through a component.
 *
 * No React, no DOM, no imports: runnable under `node --test`.
 */

/**
 * How often the page re-reads the snapshot.
 *
 * This costs nothing — `GET /llm-stats` is a dict lookup on the AI service, which
 * is the entire point of the background refresher — so the interval is chosen for
 * the reader, not for the provider. 30s is comfortably under the ~60s refresh
 * period, so a new snapshot is picked up within half a cycle of appearing.
 */
export const SNAPSHOT_POLL_MS = 30_000;

/** How long a post-click confirmation stays on screen. */
export const FEEDBACK_DISMISS_MS = 4_000;

/**
 * How many refresh intervals may elapse before the refresher is presumed dead.
 *
 * 3 rather than 1, and the margin is not arbitrary. `fetched_at` is stamped at the
 * END of a cycle and a cycle itself takes ~18s (12 requests, spaced), so successive
 * stamps are `interval + cycle` apart — already past one interval by design. A
 * threshold of 1 would cry wolf on every single cycle. 3 is loose enough to never
 * false-alarm and tight enough that a genuinely dead task is called out within a
 * few minutes.
 *
 * This is the ONLY definition of "probably dead" in the UI; both the label's
 * warning tone and the Update message derive from it, so the page cannot contradict
 * itself about whether the refresher is healthy.
 */
export const STALE_INTERVALS = 3;

/** Age of `iso` in seconds, or `null` if there is no usable timestamp. */
export function ageSeconds(iso: string | null, now: number): number | null {
  if (iso === null) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  // Clamped: a little clock skew between server and browser is normal, and a
  // negative age must not read as "the future".
  return Math.max(0, (now - then) / 1000);
}

/**
 * Whether the data is old enough that the background refresher has probably died.
 *
 * This is why the timestamp is worth displaying at all. It is the only health
 * signal the refresher has: it publishes into a snapshot, so if the task dies the
 * page keeps serving the last numbers it collected — plausible, well-formed, and
 * increasingly wrong — with no error anywhere. Without an age on screen nobody
 * would notice for hours.
 *
 * A missing timestamp is NOT stale: nothing has been collected yet, which is the
 * cold-start state, not a failure.
 */
export function isStale(
  iso: string | null,
  refreshIntervalS: number,
  now: number
): boolean {
  const age = ageSeconds(iso, now);
  if (age === null) return false;
  return age > refreshIntervalS * STALE_INTERVALS;
}

/**
 * Seconds until the next refresh is due. Negative once the deadline has passed.
 *
 * Callers must not render a negative value — see `updateFeedback`, which switches
 * to different wording instead.
 */
export function secondsUntilNextUpdate(
  iso: string | null,
  refreshIntervalS: number,
  now: number
): number | null {
  const age = ageSeconds(iso, now);
  if (age === null) return null;
  return Math.round(refreshIntervalS - age);
}

export interface UpdateFeedbackInput {
  /** Whether the fetch SUCCEEDED. False produces no message at all. */
  ok: boolean;
  /** `fetched_at` before this fetch. */
  previousFetchedAt: string | null;
  /** `fetched_at` after it. */
  nextFetchedAt: string | null;
  refreshIntervalS: number;
  now: number;
}

/**
 * What to tell the reader after they click Update — or `null` for "say nothing".
 *
 * The asymmetry is the design: when the numbers actually change, THE CHANGE IS THE
 * FEEDBACK, and a message on top of it would be redundant chrome. A message appears
 * only when the click produced no visible delta, which is the case the reader would
 * otherwise experience as a dead button.
 *
 * Four traps this function exists to avoid, all of which produce a confidently
 * wrong sentence rather than an obvious bug:
 *
 * 1. **Compare `fetched_at`, never the payload.** In a quiet period two genuine
 *    refreshes yield byte-identical numbers, so a payload comparison would report
 *    "no new data" when the refresher had in fact just run.
 * 2. **Only compute on the success branch.** A failed fetch keeps the previous
 *    stats on screen, so `fetched_at` is trivially unchanged — a naive
 *    implementation says "no new data" when the truth is "the request failed".
 *    Hence the explicit `ok`, which makes the invariant testable in one line.
 * 3. **Never a negative or zero countdown.** Past the deadline the wording changes
 *    rather than the number going negative or freezing at zero.
 * 4. **A missing timestamp is its own case**, checked before everything else:
 *    on a cold start `previous` and `next` are both null, so "unchanged" is
 *    technically true but "still collecting" is what is actually happening.
 */
export function updateFeedback({
  ok,
  previousFetchedAt,
  nextFetchedAt,
  refreshIntervalS,
  now,
}: UpdateFeedbackInput): string | null {
  // Trap 2. The error message owns the slot; anything from here would either
  // contradict it or push it aside.
  if (!ok) return null;

  // Trap 4.
  if (nextFetchedAt === null) return "Still collecting…";

  // Trap 1: identity of the stamp, not equality of the numbers.
  if (nextFetchedAt !== previousFetchedAt) return null;

  // Trap 3. Overdue is expected for part of every cycle (the stamp lands at the
  // END of an ~18s cycle, so it always arrives after `interval` has elapsed), which
  // is why "late" and "stopped" are different sentences with the shared staleness
  // threshold deciding between them.
  if (isStale(nextFetchedAt, refreshIntervalS, now)) {
    return "No new data — the background refresh may have stopped";
  }

  const seconds = secondsUntilNextUpdate(nextFetchedAt, refreshIntervalS, now);
  if (seconds === null || seconds <= 0) {
    return "No new data yet — the next update is due";
  }
  // The tilde is load-bearing: the cycle takes time and the interval is a sleep
  // between cycles, so this is an estimate and must not read as a promise.
  return `No new data yet — next update in ~${seconds}s`;
}
