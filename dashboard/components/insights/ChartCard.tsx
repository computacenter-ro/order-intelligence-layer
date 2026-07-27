import { Card } from "@computacenter-ro/style-guide/components";

interface ChartCardProps {
  title: string;
  /** What the chart plots — carries the identity a single-series chart needs
   *  instead of a one-swatch legend box. */
  subtitle?: string;
  /** Small status/ordinal key, where color carries meaning beyond the axis labels. */
  keyRow?: React.ReactNode;
  children: React.ReactNode;
}

/** A titled surface for one chart. Padding is generous on purpose — the marks
 *  are thin and the chrome recessive, so the breathing room is what makes it
 *  read as considered rather than cramped. */
export function ChartCard({ title, subtitle, keyRow, children }: ChartCardProps) {
  return (
    <Card style={{ padding: "20px 22px" }}>
      <div style={{ fontSize: "16px", fontWeight: 600, color: "var(--cc-foundation-blue)" }}>
        {title}
      </div>
      {subtitle && (
        <div style={{ fontSize: "13px", color: "var(--cc-grey-three)", marginTop: "2px" }}>
          {subtitle}
        </div>
      )}
      {keyRow && <div style={{ marginTop: "10px" }}>{keyRow}</div>}
      <div style={{ marginTop: "14px" }}>{children}</div>
    </Card>
  );
}

interface ColorKeyProps {
  items: { color: string; label: string }[];
}

/** Inline color key: a swatch plus its meaning. Used where the fill colors say
 *  something the axis labels don't (good vs stopped, severity escalation). */
export function ColorKey({ items }: ColorKeyProps) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: "14px" }}>
      {items.map((item) => (
        <span
          key={item.label}
          style={{ display: "flex", alignItems: "center", gap: "6px", fontSize: "12px", color: "var(--cc-grey-two)" }}
        >
          <span
            style={{ width: "10px", height: "10px", borderRadius: "2px", background: item.color, flexShrink: 0 }}
          />
          {item.label}
        </span>
      ))}
    </div>
  );
}
