import { SERIES_MUTED, SERIES_ONE } from "@/lib/chartColors";

interface SplitBarProps {
  /** What the split is over, e.g. "Level". */
  label: string;
  /** The part that carries the story — gets the accent hue. */
  primary: { label: string; count: number };
  /** The remainder — de-emphasis grey, so the eye lands on `primary`. */
  secondary: { label: string; count: number };
}

/**
 * A two-part share bar for a binary breakdown (ERROR/WARN, AI/fallback,
 * open/resolved).
 *
 * Deliberately NOT a two-hue chart: with only two classes, one accent hue plus
 * de-emphasis grey is the *emphasis* form — it says which number matters
 * instead of asking the reader to decode two colors. It also sidesteps a real
 * problem: red-vs-orange (ERROR vs WARN) scores below the normal-vision
 * separation floor, so as fills they would be hard to tell apart even with full
 * color vision.
 *
 * Both counts are written out beside the bar, so nothing here is color-only.
 */
export function SplitBar({ label, primary, secondary }: SplitBarProps) {
  const total = primary.count + secondary.count;
  const pct = total > 0 ? (primary.count / total) * 100 : 0;

  return (
    <div style={{ marginBottom: "14px" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: "12px",
          marginBottom: "5px",
        }}
      >
        <span style={{ color: "var(--cc-grey-three)" }}>{label}</span>
        <span style={{ color: "var(--cc-grey-two)" }}>
          <strong style={{ fontWeight: 600 }}>{primary.count}</strong> {primary.label}
          <span style={{ color: "var(--cc-grey-four)" }}> · </span>
          <strong style={{ fontWeight: 600 }}>{secondary.count}</strong> {secondary.label}
        </span>
      </div>
      <div style={{ display: "flex", height: "8px", borderRadius: "var(--cc-radius-sm)", overflow: "hidden" }}>
        {total === 0 ? (
          <div style={{ flex: 1, background: "var(--cc-grey-six)" }} />
        ) : (
          <>
            <div style={{ width: `${pct}%`, background: SERIES_ONE }} />
            {/* 2px of surface separates the two fills — the gap does the
                separating, never a border stroke around a mark. */}
            {pct > 0 && pct < 100 && (
              <div style={{ width: "2px", background: "var(--cc-cloud-white)", flexShrink: 0 }} />
            )}
            <div style={{ flex: 1, background: SERIES_MUTED }} />
          </>
        )}
      </div>
    </div>
  );
}
