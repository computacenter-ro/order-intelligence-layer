import { Card } from "@computacenter-ro/style-guide/components";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { Badge } from "@/components/ui/Badge";
import { ConfidenceBar } from "@/components/ui/ConfidenceBar";
import { SeverityPill } from "@/components/ui/SeverityPill";
import { formatTime, capitalize } from "@/lib/format";
import { renderInlineMarkdown } from "@/lib/richText";
import type { ProcessedAlert } from "@/lib/types";

interface AlertCardProps {
  alert: ProcessedAlert;
  onOpen: (alert: ProcessedAlert) => void;
  isSelected?: boolean;
}

const FALLBACK_EXPLANATION =
  "Unprocessed — LLM unavailable. Raw log passed straight through; no explanation or routing.";

export function AlertCard({ alert, onOpen, isSelected = false }: AlertCardProps) {
  const isFallback = alert.source === "fallback";

  const accentColor = isFallback
    ? "var(--cc-grey-four)"
    : alert.level === "ERROR"
      ? "var(--cc-united-red)"
      : "var(--cc-fibre-orange)";

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(alert)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onOpen(alert);
      }}
    >
      <Card
        style={{
          marginBottom: "12px",
          cursor: "pointer",
          borderLeft: `3px solid ${accentColor}`,
          ...(isFallback ? { border: "1px dashed var(--cc-grey-four)", opacity: 0.85 } : {}),
          ...(isSelected
            ? { boxShadow: `0 0 0 2px ${badgeColors.primary.border}`, background: badgeColors.primary.bg }
            : {}),
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px", flexWrap: "wrap" }}>
          <SeverityPill level={alert.level} severity={alert.severity} />
          <span style={{ fontFamily: "ui-monospace, Menlo, monospace", fontSize: "12px", color: "var(--cc-grey-two)" }}>
            {alert.app_name}
          </span>
          <span style={{ marginLeft: "auto", fontSize: "12px", color: "var(--cc-grey-three)" }}>
            {formatTime(alert.emitted_at)}
          </span>
        </div>
        <p
          style={{
            fontSize: "16px",
            lineHeight: "22px",
            color: isFallback ? "var(--cc-grey-three)" : "var(--cc-grey-one)",
            fontStyle: isFallback ? "italic" : "normal",
            margin: "0 0 12px",
          }}
        >
          {alert.explanation ? renderInlineMarkdown(alert.explanation) : FALLBACK_EXPLANATION}
        </p>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          {alert.source === "ai" ? (
            <>
              <Badge status="other">AI-analyzed</Badge>
              {alert.department && <Badge status="info">{capitalize(alert.department)}</Badge>}
              {alert.confidence != null && <ConfidenceBar confidence={alert.confidence} />}
            </>
          ) : (
            <>
              <Badge status="inactive">Fallback</Badge>
              <Badge status="inactive">General</Badge>
            </>
          )}
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "ui-monospace, Menlo, monospace",
              fontSize: "11px",
              color: "var(--cc-grey-three)",
            }}
          >
            order {alert.order_id ?? "—"} · evt {alert.event_id ?? "—"}
          </span>
        </div>
      </Card>
    </div>
  );
}
