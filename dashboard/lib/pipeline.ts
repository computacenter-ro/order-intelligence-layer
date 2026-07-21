import type { Journey, JourneyStatus } from "@/lib/types";

export const CANONICAL_STAGES: { appName: string; label: string }[] = [
  { appName: "cc-inbound-service", label: "inbound" },
  { appName: "cc-order-engine", label: "order-engine" },
  { appName: "cc-spt-service", label: "spt" },
  { appName: "cc-rsm-service", label: "rsm" },
  { appName: "cc-solr-service", label: "solr" },
  { appName: "cc-jam-service", label: "jam" },
  { appName: "cc-checker-service", label: "checker" },
  { appName: "cc-validator-service", label: "validator" },
  { appName: "cc-outbound-osw", label: "outbound-osw" },
  { appName: "cc-track-trace", label: "track-trace" },
];

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

// Maps a FAILED journey's outcome subtype (backend/journeys.py's outcome
// vocabulary) to the stage actually responsible for the failure. This can't
// be derived from "which app_name logged the terminal message" - several
// failure paths are deliberately reported through order-engine's own
// client-side logging rather than the satellite's (see e.g.
// pipeline/services/spt.py's docstring: "the satellite emits nothing
// itself... this block, on failure, emits those order-engine-identity
// lines"), which would otherwise misattribute the failure to order-engine
// and greyed out every later stage the flow genuinely passed through.
const OUTCOME_FAIL_STAGE: Record<string, string> = {
  INBOUND_TRANSFORM_FAILED: "inbound",
  ORDER_CREATION_FAILED: "order-engine",
  ENRICHMENT_FAILED: "spt",
  MARGIN_CHECK_FAILED: "checker",
  AUTH_FAILED: "jam",
  VALIDATION_FAILED: "validator",
  SAP_SUBMISSION_FAILED: "outbound-osw",
};

// order-engine logs a trailing wrap-up line for these outcomes (it receives
// the satellite's failure and halts submission) even though it isn't itself
// at fault - "warned" distinguishes it from a stage the flow never reached
// (grey) and from the stage that actually failed (red). Not true for
// SAP_SUBMISSION_FAILED (outbound_osw.py never logs through order-engine) or
// ORDER_CREATION_FAILED/INBOUND_TRANSFORM_FAILED (order-engine is either the
// failing stage itself or never reached).
const ORDER_ENGINE_WARNED_FOR = new Set([
  "ENRICHMENT_FAILED",
  "MARGIN_CHECK_FAILED",
  "AUTH_FAILED",
  "VALIDATION_FAILED",
]);

function stageIndexByLabel(label: string): number {
  return CANONICAL_STAGES.findIndex((stage) => stage.label === label);
}

export function pipelineStages(journey: Journey): PipelineStage[] {
  const events = journey.events ?? [];
  const lastEvent = events[events.length - 1];
  const lastAppName = lastEvent ? lastEvent.raw.app_name : CANONICAL_STAGES[0].appName;
  const rawIndex = CANONICAL_STAGES.findIndex((stage) => stage.appName === lastAppName);

  // The curated outcome table is authoritative for FAILED journeys; fall back
  // to the previous last-event-based guess for outcomes it doesn't cover
  // (the generic FAILED subtype) and for SUCCESS/TIMED_OUT/IN_PROGRESS, where
  // there is no single "stage at fault" to look up.
  const failLabel = journey.status === "FAILED" ? OUTCOME_FAIL_STAGE[journey.outcome ?? ""] : undefined;
  const failIndex = failLabel !== undefined ? stageIndexByLabel(failLabel) : -1;
  const stopIndex = failIndex !== -1 ? failIndex : rawIndex === -1 ? 0 : rawIndex;

  const orderEngineWarned =
    journey.status === "FAILED" && ORDER_ENGINE_WARNED_FOR.has(journey.outcome ?? "");
  const orderEngineIndex = stageIndexByLabel("order-engine");

  return CANONICAL_STAGES.map((stage, index) => {
    if (index === orderEngineIndex && orderEngineWarned && index < stopIndex) {
      return { label: stage.label, state: "warned" as const };
    }
    if (index < stopIndex) return { label: stage.label, state: "done" as const };
    if (index === stopIndex) return { label: stage.label, state: STOP_STATE[journey.status] };
    return { label: stage.label, state: AFTER_STATE[journey.status] };
  });
}
