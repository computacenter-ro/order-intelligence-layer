import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { stoppedAt } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
import type { Journey } from "@/lib/types";

interface OutcomeBannerProps {
  journey: Journey;
}

const ID_MONO: React.CSSProperties = { fontFamily: "ui-monospace, Menlo, monospace" };

export function OutcomeBanner({ journey }: OutcomeBannerProps) {
  const tone = badgeColors[JOURNEY_STATUS_BADGE[journey.status]];
  const orderLabel = journey.order_id ?? journey.event_id ?? "—";
  const locationLabel = journey.status === "SUCCESS" ? "completed" : `stopped at ${stoppedAt(journey)}`;

  // All three correlation ids, each independently nullable (a pre-creation
  // failure has only event id; the bridge may expose only one order id).
  const ids: { label: string; value: string | null }[] = [
    { label: "Order", value: journey.order_id },
    { label: "Cart header", value: journey.cart_header_id },
    { label: "Event", value: journey.event_id },
  ];

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
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "16px",
          fontSize: "13px",
          color: "var(--cc-grey-three)",
          marginTop: "8px",
        }}
      >
        {ids.map(({ label, value }) => (
          <span key={label}>
            {label}: <span style={ID_MONO}>{value ?? "—"}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
