import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { stoppedAt } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
import type { Journey } from "@/lib/types";

interface OutcomeBannerProps {
  journey: Journey;
}

export function OutcomeBanner({ journey }: OutcomeBannerProps) {
  const tone = badgeColors[JOURNEY_STATUS_BADGE[journey.status]];
  const orderLabel = journey.order_id ?? journey.event_id ?? "—";
  const locationLabel = journey.status === "SUCCESS" ? "completed" : `stopped at ${stoppedAt(journey)}`;

  return (
    <div
      style={{
        background: tone.bg,
        border: `1px solid ${tone.border}`,
        borderRadius: "8px",
        padding: "16px 24px",
        marginBottom: "16px",
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: "8px", flexWrap: "wrap" }}>
        <span style={{ fontSize: "20px", fontWeight: 600, color: tone.text }}>
          {JOURNEY_STATUS_LABEL[journey.status]}
        </span>
        {journey.outcome && (
          <span style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: "14px", color: tone.text }}>
            {journey.outcome}
          </span>
        )}
      </div>
      <div style={{ fontSize: "14px", color: "var(--cc-grey-two)", marginTop: "4px" }}>
        Order {orderLabel} · {locationLabel}
      </div>
    </div>
  );
}
