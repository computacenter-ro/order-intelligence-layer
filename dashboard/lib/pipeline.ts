import type { Journey, JourneyStatus } from "@/lib/types";

// The trail's stage order under the five-hop ping-pong pipeline: FIRST-TOUCH
// order, one row per service touch-point, fixed length for every journey so
// rows line up between journeys.
//
//   Inbound receives → Order Engine's first (defaults-only) turn → Inbound's
//   pre-creation leg (Settings → JAM → SOLR) → creation (Order Engine again,
//   no new row: rows are first-touch) → Track & Trace (MID-flow now, right
//   after creation) → SPT → RSM → Validator → [Avalara, US only] → Checker →
//   Outbound OSW → and the flow ENDS BACK AT INBOUND (order_created).
//
// Two deliberate row decisions:
//
// * cc-settings-service has NO row. Settings failures exist (scenarios
//   15/16/17) but are deliberately UNRECOGNIZED_FAILUREs — the demo's
//   "anomaly" case — so they have no entry in OUTCOME_STAGES, and the
//   fallback attribution below uses the last event's app_name, which for
//   those journeys is cc-inbound-service (the failure line is an
//   Inbound-identity client log). A Settings row could therefore never be
//   painted red by any real journey — it would render as permanent
//   done/skipped decoration — so it is omitted. The anomaly correctly
//   surfaces on the Inbound row instead.
// * The trail ends with a REPEATED terminal row, `inbound-ack`: the corrected
//   flow's whole point is that SUCCESS ends back at Inbound, and a
//   first-touch-only list would misleadingly end at outbound-osw. The list
//   stays fixed-length (the row is always present), so rows still line up.
//   Its state derives PURELY from journey.status — never from events — which
//   is what keeps inbound's two touches from needing per-log disambiguation:
//   done on SUCCESS, pending while IN_PROGRESS, skipped otherwise. It is
//   never "current" and never a fail/warn stage. The cost: for the one poll
//   where the close log has arrived but completion hasn't, the row still
//   reads pending — a one-beat lag, accepted.
//
// cc-avalara-service sits at its documented position between Validator and
// Checker (auto-approval rules 1 → 1 → 3), and is US-only: a non-US journey
// has no avalara event, so the stage renders as "skipped" — which is honest
// (the order genuinely did not need US address verification) and is exactly
// how the trail already treats any stage a flow legitimately bypasses. We do
// NOT try to detect US-ness to omit the row: journey data carries no country
// field, and a stage list whose LENGTH varied per journey would make the
// trail's rows stop lining up between journeys.
export const CANONICAL_STAGES: { appName: string; label: string }[] = [
  { appName: "cc-inbound-service", label: "inbound" },
  { appName: "cc-order-engine", label: "order-engine" },
  { appName: "cc-jam-service", label: "jam" },
  { appName: "cc-solr-service", label: "solr" },
  { appName: "cc-track-trace", label: "track-trace" },
  { appName: "cc-spt-service", label: "spt" },
  { appName: "cc-rsm-service", label: "rsm" },
  { appName: "cc-validator-service", label: "validator" },
  { appName: "cc-avalara-service", label: "avalara" },
  { appName: "cc-checker-service", label: "checker" },
  { appName: "cc-outbound-osw", label: "outbound-osw" },
  // The return leg: order_created lands back at Inbound. See the header
  // comment for why this repeated terminal row exists and how it is scored.
  { appName: "cc-inbound-service", label: "inbound-ack" },
];

/** Index of the repeated terminal row (always the last row). */
const TERMINAL_INDEX = CANONICAL_STAGES.length - 1;

export type PipelineStageState =
  | "done"
  | "current"
  | "stopped"
  | "stalled"
  | "skipped"
  | "pending"
  | "warned";

export interface PipelineStage {
  label: string;
  state: PipelineStageState;
}

const STOP_STATE: Record<JourneyStatus, PipelineStageState> = {
  SUCCESS: "done",
  FAILED: "stopped",
  TIMED_OUT: "stalled",
  IN_PROGRESS: "current",
};

const AFTER_STATE: Record<JourneyStatus, PipelineStageState> = {
  SUCCESS: "skipped",
  FAILED: "skipped",
  TIMED_OUT: "skipped",
  IN_PROGRESS: "pending",
};

/** How the terminal inbound-ack row reads for each journey status. */
const TERMINAL_STATE: Record<JourneyStatus, PipelineStageState> = {
  SUCCESS: "done",
  FAILED: "skipped",
  TIMED_OUT: "skipped",
  IN_PROGRESS: "pending",
};

// Maps a FAILED journey's outcome subtype (backend/journeys.py's outcome
// vocabulary) to the stage responsible for the failure AND the caller that
// gets the yellow "warned" row. This can't be derived from "which app_name
// logged the terminal message" — several failure paths are deliberately
// reported through the CALLER's own client-side logging rather than the
// satellite's (see e.g. pipeline/services/spt.py: the satellite emits nothing
// itself on failure), which would misattribute the failure to the caller and
// grey out every later stage the flow genuinely passed through.
//
// warnStage: the trail paints two stages on a satellite failure — the
// satellite goes red, and its CALLER goes yellow, because the caller logs a
// wrap-up line and halts the flow without itself being at fault. There are
// TWO callers under the five-hop pipeline: Inbound owns the pre-creation leg
// (Settings/JAM/SOLR), the Order Engine owns the post-creation one
// (SPT/RSM/Validator/Avalara/Checker) — so AUTH_FAILED warns *inbound*, not
// order-engine.
//
// Outcomes with NO warnStage, each re-derived under the new chain:
// * INBOUND_TRANSFORM_FAILED — inbound is itself the failing stage; there is
//   no caller upstream of it.
// * ORDER_CREATION_FAILED — nothing is published back on a failed creation
//   (the only post-creation return hop is the terminal order_created, which
//   never fires), so Inbound logs no wrap-up on the caller's behalf.
// * SAP_SUBMISSION_FAILED — outbound_osw fails on its own side of a queue;
//   the engine's dispatch already succeeded and it never logs a wrap-up.
export const OUTCOME_STAGES: Record<
  string,
  { failStage: string; warnStage?: string }
> = {
  INBOUND_TRANSFORM_FAILED: { failStage: "inbound" },
  ORDER_CREATION_FAILED: { failStage: "order-engine" },
  AUTH_FAILED: { failStage: "jam", warnStage: "inbound" },
  ENRICHMENT_FAILED: { failStage: "spt", warnStage: "order-engine" },
  VALIDATION_FAILED: { failStage: "validator", warnStage: "order-engine" },
  MARGIN_CHECK_FAILED: { failStage: "checker", warnStage: "order-engine" },
  SAP_SUBMISSION_FAILED: { failStage: "outbound-osw" },
};

function stageIndexByLabel(label: string): number {
  return CANONICAL_STAGES.findIndex((stage) => stage.label === label);
}

// Stages that a journey can legitimately BYPASS rather than fail at, so being
// before the stop index is not enough to call them "done" — they must actually
// have an event. Today that is only avalara (US-only). Every other stage is
// either reached by all flows or is the failure point itself.
//
// This is deliberately a narrow allow-list rather than a blanket "no event =>
// skipped" rule: several failure paths report through the caller's OWN client
// logging instead of the satellite's (see the OUTCOME_STAGES comment above),
// so a satellite with no events of its own is NOT reliable evidence that the
// order never got there.
const OPTIONAL_STAGES = new Set(["avalara"]);

export function pipelineStages(journey: Journey): PipelineStage[] {
  const events = journey.events ?? [];
  const lastEvent = events[events.length - 1];
  const lastAppName = lastEvent ? lastEvent.raw.app_name : CANONICAL_STAGES[0].appName;
  // findIndex takes the FIRST row for an app_name, so inbound's second touch
  // never resolves here — the terminal row is scored from status alone.
  const rawIndex = CANONICAL_STAGES.findIndex((stage) => stage.appName === lastAppName);
  const seenAppNames = new Set(events.map((event) => event.raw.app_name));

  // The curated outcome table is authoritative for FAILED journeys; fall back
  // to the last-event-based guess for outcomes it doesn't cover (the
  // UNRECOGNIZED_FAILURE anomaly and any future subtype) and for
  // TIMED_OUT/IN_PROGRESS, where there is no single "stage at fault".
  const outcome = journey.status === "FAILED" ? OUTCOME_STAGES[journey.outcome ?? ""] : undefined;
  const failIndex = outcome ? stageIndexByLabel(outcome.failStage) : -1;
  const stopIndex = failIndex !== -1 ? failIndex : rawIndex === -1 ? 0 : rawIndex;

  // The caller is only warned when it sits BEFORE the stop index — a caller
  // that is itself the failure point stays red, not yellow.
  const warnIndex =
    outcome?.warnStage !== undefined ? stageIndexByLabel(outcome.warnStage) : -1;

  return CANONICAL_STAGES.map((stage, index) => {
    // The repeated terminal row: status-derived, never event-derived.
    if (index === TERMINAL_INDEX) {
      return { label: stage.label, state: TERMINAL_STATE[journey.status] };
    }
    // A SUCCESS journey walked the whole pipeline: everything is done except
    // an optional stage it legitimately bypassed. (The last-event fallback
    // would be wrong here — a successful flow ENDS at Inbound, which maps to
    // row 0.)
    if (journey.status === "SUCCESS") {
      if (OPTIONAL_STAGES.has(stage.label) && !seenAppNames.has(stage.appName)) {
        return { label: stage.label, state: "skipped" as const };
      }
      return { label: stage.label, state: "done" as const };
    }
    if (index === warnIndex && index < stopIndex) {
      return { label: stage.label, state: "warned" as const };
    }
    // An optional stage the order never actually visited (a non-US journey's
    // avalara) reads as skipped, not done: position alone would paint it green
    // and claim the order was address-verified when it never was.
    if (OPTIONAL_STAGES.has(stage.label) && !seenAppNames.has(stage.appName)) {
      return { label: stage.label, state: "skipped" as const };
    }
    if (index < stopIndex) return { label: stage.label, state: "done" as const };
    if (index === stopIndex) return { label: stage.label, state: STOP_STATE[journey.status] };
    return { label: stage.label, state: AFTER_STATE[journey.status] };
  });
}
