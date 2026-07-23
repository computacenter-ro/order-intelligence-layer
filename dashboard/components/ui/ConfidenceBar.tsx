function toneFor(confidence: number): string {
  if (confidence >= 0.8) return "var(--cc-circuit-green)";
  if (confidence >= 0.6) return "var(--cc-fibre-orange)";
  return "var(--cc-united-red)";
}

interface ConfidenceBarProps {
  confidence: number;
}

export function ConfidenceBar({ confidence }: ConfidenceBarProps) {
  const pct = Math.round(confidence * 100);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "11px", color: "var(--cc-grey-three)" }}>
      <span>Conf</span>
      <div style={{ width: "46px", height: "5px", borderRadius: "3px", backgroundColor: "var(--cc-grey-six)", overflow: "hidden" }}>
        <div
          data-testid="confidence-fill"
          style={{ width: `${pct}%`, height: "100%", backgroundColor: toneFor(confidence) }}
        />
      </div>
      <span>{confidence.toFixed(2)}</span>
    </div>
  );
}
