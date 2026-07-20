import { CheckCircleIcon, XCircleIcon, ClockIcon, CircleDashedIcon } from "@phosphor-icons/react";
import { pipelineStages } from "@/lib/pipeline";
import type { PipelineStageState } from "@/lib/pipeline";
import type { Journey } from "@/lib/types";

interface PipelineTrailProps {
  journey: Journey;
}

const STAGE_COLOR: Record<PipelineStageState, string> = {
  done: "var(--cc-circuit-green)",
  current: "var(--cc-fibre-orange)",
  stopped: "var(--cc-united-red)",
  stalled: "var(--cc-voltage-yellow)",
  skipped: "var(--cc-grey-four)",
  pending: "var(--cc-grey-four)",
};

function StageIcon({ state }: { state: PipelineStageState }) {
  const color = STAGE_COLOR[state];
  if (state === "done") return <CheckCircleIcon size={16} color={color} />;
  if (state === "stopped") return <XCircleIcon size={16} color={color} />;
  if (state === "stalled") return <ClockIcon size={16} color={color} />;
  if (state === "current") return <CircleDashedIcon size={16} color={color} className="oil-pipeline-current" />;
  return null;
}

export function PipelineTrail({ journey }: PipelineTrailProps) {
  const stages = pipelineStages(journey);

  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "6px" }}>
      {stages.map((stage, index) => (
        <div key={stage.label} style={{ display: "flex", alignItems: "center", gap: "6px" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "6px",
              padding: "6px 12px",
              borderRadius: "8px",
              border: `1px solid ${STAGE_COLOR[stage.state]}`,
              opacity: stage.state === "skipped" || stage.state === "pending" ? 0.5 : 1,
              fontFamily: "ui-monospace, Menlo, monospace",
              fontSize: "12px",
              color: STAGE_COLOR[stage.state],
            }}
          >
            <StageIcon state={stage.state} />
            {stage.label}
          </div>
          {index < stages.length - 1 && <span style={{ color: "var(--cc-grey-four)" }}>→</span>}
        </div>
      ))}
    </div>
  );
}
