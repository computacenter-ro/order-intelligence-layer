"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import {
  CHART_AXIS_TEXT,
  CHART_GRID,
} from "@/lib/chartColors";

/** One bar: the backend's raw bucket key, its display label, count and fill. */
export interface BreakdownRow {
  key: string;
  label: string;
  count: number;
  color: string;
}

interface BreakdownBarChartProps {
  rows: BreakdownRow[];
  /** Denominator for the tooltip's share-of-total line. */
  total: number;
  /** Width reserved for the category labels; widen for long outcome names. */
  yAxisWidth?: number;
  emptyMessage?: string;
}

// Bars are horizontal so long category names ("Inbound transform failed") read
// left-to-right instead of being rotated or truncated under a column.
const ROW_HEIGHT = 34;
// Reserved for the x-axis tick band. Counted into the container height so the
// axis labels are never cut off (a plot-only height gives the card its own tiny
// nested scrollbar).
const AXIS_BAND = 34;
// Cap the mark thickness and let the band's leftover be air, rather than
// filling the slot with a heavy block. Also keeps adjacent bars from touching,
// so no separator stroke is needed.
const MAX_BAR = 22;

function ChartTooltip({ active, payload, total }: TooltipContentProps & { total: number }) {
  if (!active || payload.length === 0) return null;
  const row = payload[0].payload as BreakdownRow;
  const share = total > 0 ? Math.round((row.count / total) * 100) : 0;
  return (
    <div
      style={{
        background: "var(--cc-cloud-white)",
        border: "1px solid var(--cc-grey-five)",
        borderRadius: "var(--cc-radius-md)",
        boxShadow: "var(--cc-shadow-sm)",
        padding: "8px 10px",
        fontSize: "12px",
      }}
    >
      {/* A swatch beside the text carries identity — the text itself stays in an
          ink token, never the series color (a light fill is illegible as text). */}
      <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "2px" }}>
        <span
          style={{
            width: "8px",
            height: "8px",
            borderRadius: "2px",
            background: row.color,
            flexShrink: 0,
          }}
        />
        <span style={{ fontWeight: 600, color: "var(--cc-grey-one)" }}>{row.label}</span>
      </div>
      <div style={{ color: "var(--cc-grey-two)" }}>
        {total > 0 ? `${row.count} of ${total} · ${share}%` : row.count}
      </div>
      {/* The exact backend key, so the display label never hides what to filter on. */}
      <div style={{ color: "var(--cc-grey-three)", marginTop: "2px", fontFamily: "monospace" }}>
        {row.key}
      </div>
    </div>
  );
}

/**
 * A horizontal bar chart over one categorical breakdown.
 *
 * Every bar carries a visible value label at its tip and a text label on the
 * axis, so no value is reachable only by hovering — and the light fills
 * (Circuit Green sits at 2.09:1 on this surface) have the relief channel the
 * contrast rule requires. Do not remove the labels to "clean it up".
 */
export function BreakdownBarChart({
  rows,
  total,
  yAxisWidth = 172,
  emptyMessage = "No data yet",
}: BreakdownBarChartProps) {
  if (rows.length === 0) {
    return (
      <p style={{ fontSize: "14px", color: "var(--cc-grey-three)", margin: "8px 0 0" }}>
        {emptyMessage}
      </p>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={rows.length * ROW_HEIGHT + AXIS_BAND}>
      <BarChart
        data={rows}
        layout="vertical"
        margin={{ top: 4, right: 36, bottom: 0, left: 0 }}
      >
        {/* Hairline, solid, one step off the surface — never dashed. Only the
            value axis gets gridlines; a line per category would be noise. */}
        <CartesianGrid horizontal={false} stroke={CHART_GRID} />
        <XAxis
          type="number"
          allowDecimals={false}
          tick={{ fontSize: 11, fill: CHART_AXIS_TEXT }}
          stroke={CHART_GRID}
        />
        <YAxis
          type="category"
          dataKey="label"
          width={yAxisWidth}
          tick={{ fontSize: 12, fill: CHART_AXIS_TEXT }}
          stroke={CHART_GRID}
        />
        <Tooltip
          content={(props: TooltipContentProps) => (
            <ChartTooltip {...props} total={total} />
          )}
          // Recharts' default hover fill would tint the whole category band;
          // the tooltip is enough feedback and the tint muddies the bar colors.
          cursor={{ fill: "var(--cc-grey-six)", fillOpacity: 0.5 }}
        />
        <Bar
          dataKey="count"
          maxBarSize={MAX_BAR}
          isAnimationActive={false}
          // Rounded data-end, square at the baseline (top-left/bottom-left 0).
          radius={[0, 4, 4, 0]}
        >
          {rows.map((row) => (
            <Cell key={row.key} fill={row.color} />
          ))}
          <LabelList
            dataKey="count"
            position="right"
            style={{ fontSize: 12, fontWeight: 600, fill: "var(--cc-grey-two)" }}
          />
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
