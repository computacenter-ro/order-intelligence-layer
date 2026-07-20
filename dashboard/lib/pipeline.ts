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

export type PipelineStageState = "done" | "current" | "stopped" | "stalled" | "skipped" | "pending";

export interface PipelineStage {
  label: string;
  state: PipelineStageState;
}

const STOP_STATE: Record<JourneyStatus, PipelineStageState> = {
  success: "done",
  failed: "stopped",
  timed_out: "stalled",
  in_progress: "current",
};

const AFTER_STATE: Record<JourneyStatus, PipelineStageState> = {
  success: "skipped",
  failed: "skipped",
  timed_out: "skipped",
  in_progress: "pending",
};

export function pipelineStages(journey: Journey): PipelineStage[] {
  const lastEvent = journey.events[journey.events.length - 1];
  const lastAppName = lastEvent ? lastEvent.raw.app_name : CANONICAL_STAGES[0].appName;
  const rawIndex = CANONICAL_STAGES.findIndex((stage) => stage.appName === lastAppName);
  const stopIndex = rawIndex === -1 ? 0 : rawIndex;

  return CANONICAL_STAGES.map((stage, index) => {
    if (index < stopIndex) return { label: stage.label, state: "done" as const };
    if (index === stopIndex) return { label: stage.label, state: STOP_STATE[journey.status] };
    return { label: stage.label, state: AFTER_STATE[journey.status] };
  });
}
