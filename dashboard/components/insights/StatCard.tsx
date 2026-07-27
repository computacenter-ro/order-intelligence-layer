import { Card } from "@computacenter-ro/style-guide/components";

interface StatCardProps {
  /** Sentence case, no trailing colon. */
  label: string;
  /** Pre-formatted for display — the caller owns "68%" / "1m 20s" / "—". */
  value: string;
  /** Optional one-line qualifier under the value (what the number is over). */
  hint?: string;
  /**
   * Signal color for the value. Only pass one when the number *means*
   * good/bad (a non-zero critical count, a failure rate) — never decoratively.
   * The label always carries the meaning too, so color is never the only cue.
   */
  tone?: string;
}

/**
 * One KPI tile: label + value (+ hint). A row of these is the right form for a
 * handful of headline numbers — a one-bar chart per number would say less in
 * more space.
 *
 * The value uses the font's default proportional figures, NOT `tabular-nums`:
 * equal-width digits make a large standalone number like "121" read loose.
 * Tabular figures belong in columns that must align vertically (the journeys
 * table), not here.
 */
export function StatCard({ label, value, hint, tone }: StatCardProps) {
  return (
    <Card style={{ padding: "16px 18px" }}>
      <div
        style={{
          fontSize: "12px",
          fontWeight: 500,
          letterSpacing: "0.02em",
          textTransform: "uppercase",
          color: "var(--cc-grey-three)",
        }}
      >
        {label}
      </div>
      <div
        style={{
          fontSize: "30px",
          fontWeight: 600,
          lineHeight: "38px",
          marginTop: "4px",
          color: tone ?? "var(--cc-foundation-blue)",
        }}
      >
        {value}
      </div>
      {hint && (
        <div style={{ fontSize: "12px", color: "var(--cc-grey-three)", marginTop: "2px" }}>
          {hint}
        </div>
      )}
    </Card>
  );
}
