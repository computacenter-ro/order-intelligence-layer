import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { alerts } from "@/lib/fixtures";
import { formatTime } from "@/lib/format";
import type { Journey, LogLevel } from "@/lib/types";

interface JourneyTimelineProps {
  journey: Journey;
}

const DOT_COLOR: Record<LogLevel, string> = {
  DEBUG: "var(--cc-grey-four)",
  INFO: "var(--cc-circuit-green)",
  WARN: "var(--cc-fibre-orange)",
  ERROR: "var(--cc-united-red)",
};

export function JourneyTimeline({ journey }: JourneyTimelineProps) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
      {journey.events.map((event) => {
        const alert = alerts.find((a) => a.log.log_id === event.raw.log_id);
        const tone = alert ? (alert.source === "ai" ? badgeColors.other : badgeColors.inactive) : null;

        return (
          <div key={event.log_id} style={{ display: "flex", gap: "12px" }}>
            <div
              style={{
                width: "10px",
                height: "10px",
                borderRadius: "9999px",
                background: DOT_COLOR[event.raw.level],
                marginTop: "6px",
                flexShrink: 0,
              }}
            />
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: "12px", color: "var(--cc-grey-three)", display: "flex", gap: "8px" }}>
                <span style={{ fontFamily: "ui-monospace, Menlo, monospace" }}>{event.raw.app_name}</span>
                <span>{formatTime(event.ts)}</span>
              </div>
              <div
                style={{
                  fontFamily: "ui-monospace, Menlo, monospace",
                  fontSize: "13px",
                  color: "var(--cc-grey-one)",
                  margin: "2px 0",
                }}
              >
                {event.raw.message}
              </div>
              {alert && tone && (
                <div
                  style={{
                    borderLeft: `2px ${alert.source === "ai" ? "solid" : "dashed"} ${tone.border}`,
                    background: tone.bg,
                    padding: "8px 12px",
                    borderRadius: "0 8px 8px 0",
                    fontSize: "13px",
                    color: tone.text,
                    marginTop: "6px",
                  }}
                >
                  {alert.explanation ?? "Unprocessed — LLM unavailable."}
                </div>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
