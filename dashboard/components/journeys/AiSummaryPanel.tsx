import { badgeColors } from "@computacenter-ro/style-guide/tokens";

interface AiSummaryPanelProps {
  summary: string | null;
}

export function AiSummaryPanel({ summary }: AiSummaryPanelProps) {
  if (!summary) {
    return (
      <div
        style={{
          border: "1px dashed var(--cc-grey-four)",
          borderRadius: "8px",
          padding: "16px 24px",
          textAlign: "center",
          color: "var(--cc-grey-three)",
          fontSize: "14px",
          marginBottom: "24px",
        }}
      >
        Still assembling — this journey hasn&apos;t completed yet.
      </div>
    );
  }

  const tone = badgeColors.other;

  return (
    <div
      style={{
        background: tone.bg,
        border: `1px solid ${tone.border}`,
        borderRadius: "8px",
        padding: "16px 24px",
        marginBottom: "24px",
      }}
    >
      <div
        style={{
          fontSize: "14px",
          fontWeight: 500,
          color: tone.text,
          textTransform: "uppercase",
          letterSpacing: "1.6px",
          marginBottom: "6px",
        }}
      >
        AI journey summary
      </div>
      <p style={{ fontSize: "16px", lineHeight: "22px", color: "var(--cc-grey-one)", margin: 0 }}>{summary}</p>
    </div>
  );
}
