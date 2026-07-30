"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { fetchStats } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import { NewAlertsBanner } from "@/components/alerts/NewAlertsBanner";
import { formatDuration, humanizeKey } from "@/lib/format";
import {
  OUTCOME_FAILED,
  OUTCOME_SUCCESS,
  SERIES_ONE,
  severityColor,
  SEVERITY_RAMP,
  SEVERITY_UNRATED,
} from "@/lib/chartColors";
import { StatCard } from "@/components/insights/StatCard";
import { ChartCard, ColorKey } from "@/components/insights/ChartCard";
import { BreakdownBarChart, type BreakdownRow } from "@/components/insights/BreakdownBarChart";
import { SplitBar } from "@/components/insights/SplitBar";
import type { OverviewStats, WsEvent } from "@/lib/types";

// Backend bucket keys for journeys that have not stopped anywhere yet. The
// outcome chart answers "where do orders stop?", so an order still moving
// through the pipeline has no answer to contribute — including it would invent
// a phantom outcome and make the bars sum to something the chart isn't about.
// ("none" is the backend's explicit null-outcome bucket; see stats.py.)
const NON_TERMINAL_OUTCOMES = new Set(["none", "IN_PROGRESS", "unknown"]);

// Severity is an ORDERED scale, so its bars are drawn in escalation order and
// not sorted by count — re-ordering an ordinal axis by value destroys the thing
// the reader is looking for. "unrated" trails as the absence of a severity.
const SEVERITY_ORDER = [...Object.keys(SEVERITY_RAMP), "unrated"];

/** The alerts-per-incident ratio as a tile value. `null` means no incident has
 *  been raised yet, which is not a ratio of zero — the em dash says "nothing to
 *  measure" where "0.0x" would claim clustering achieved nothing. */
function formatRatio(ratio: number | null): string {
  return ratio === null ? "—" : `${ratio.toFixed(1)}×`;
}

/** `{key: count}` -> chart rows, largest first (nominal categories, so ranking
 *  by value is the useful order). */
function toRows(
  counts: Record<string, number>,
  color: (key: string) => string
): BreakdownRow[] {
  return Object.entries(counts)
    .map(([key, count]) => ({ key, label: humanizeKey(key), count, color: color(key) }))
    .sort((a, b) => b.count - a.count);
}

export default function InsightsPage() {
  const [stats, setStats] = useState<OverviewStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Alerts that arrived since the numbers on screen were computed. This page is
  // a snapshot, not a live view — refetching per alert would redraw every chart
  // mid-read — so it counts them and lets the reader choose when to catch up.
  const [newCount, setNewCount] = useState(0);

  const loadStats = useCallback(() => {
    fetchStats()
      .then((next) => {
        setStats(next);
        // Clear a stale error from a previous failed attempt, so a recovered
        // backend doesn't leave the error screen up behind fresh data.
        setError(null);
      })
      .catch((err) => {
        console.error("Failed to load insights stats:", err);
        setError("Could not load the insights stats.");
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  // Both event types move these numbers: alert.new changes the alert breakdowns,
  // journey.completed changes the outcome chart and the success rate. journey.updated
  // is ignored — an in-progress journey is already counted and its status hasn't
  // changed, so it would inflate the badge without anything to refresh.
  const handleEvent = useCallback((event: WsEvent) => {
    if (event.type === "alert.new" || event.type === "journey.completed") {
      setNewCount((count) => count + 1);
    }
  }, []);

  useWebSocket(handleEvent);

  const handleRefresh = useCallback(() => {
    loadStats();
    setNewCount(0);
  }, [loadStats]);

  const outcomeRows = useMemo<BreakdownRow[]>(() => {
    if (!stats) return [];
    return Object.entries(stats.journeys.by_outcome)
      .filter(([key]) => !NON_TERMINAL_OUTCOMES.has(key))
      .map(([key, count]) => ({
        key,
        label: humanizeKey(key),
        count,
        // Status coloring: the outcome *means* fulfilled or stopped. SUCCESS is
        // the only positive value the backend emits; everything else is a stop.
        color: key === "SUCCESS" ? OUTCOME_SUCCESS : OUTCOME_FAILED,
      }))
      // SUCCESS pinned to the top, failures below it by size — so the eye reads
      // "how many made it" first, then the biggest failure mode.
      .sort((a, b) => {
        if (a.key === "SUCCESS") return -1;
        if (b.key === "SUCCESS") return 1;
        return b.count - a.count;
      });
  }, [stats]);

  const departmentRows = useMemo<BreakdownRow[]>(
    // Departments have no natural order, so every bar takes the same hue: bar
    // length already encodes the count, and coloring by value would spend the
    // identity channel re-stating it.
    () => (stats ? toRows(stats.alerts.by_department, () => SERIES_ONE) : []),
    [stats]
  );

  const severityRows = useMemo<BreakdownRow[]>(() => {
    if (!stats) return [];
    const counts = stats.alerts.by_severity;
    return SEVERITY_ORDER.filter((key) => key in counts).map((key) => ({
      key,
      label: humanizeKey(key),
      count: counts[key],
      color: severityColor(key),
    }));
  }, [stats]);

  if (loading) {
    return <p style={{ fontSize: "16px", color: "var(--cc-grey-three)" }}>Loading overview…</p>;
  }

  // Gated on `!stats`, not on `error`: now that a refresh can fail too, keying
  // this on the error flag would tear down a screen full of valid charts because
  // one refetch failed. With stats in hand the page keeps showing them (the
  // failure is logged) and the reader can click the pill again.
  if (!stats) {
    return (
      <div>
        <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
          Insights
        </h1>
        <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "8px" }}>
          {error ?? "No stats available."} The server may be down — the alert feed and
          journeys pages will be empty too if so.
        </p>
      </div>
    );
  }

  const { journeys, alerts, incidents } = stats;
  const criticalCount = alerts.by_severity.critical ?? 0;
  const timedOut = journeys.by_status.TIMED_OUT ?? 0;
  const inProgress = journeys.by_status.IN_PROGRESS ?? 0;
  const finished = journeys.total - inProgress;
  const openIncidents = incidents.by_status.open ?? 0;

  return (
    <div>
      {/* Same pill the feed uses (NewAlertsBanner takes a bare onClick, so the
          "reveal" meaning is the caller's). Here clicking refetches the stats,
          which redraws the KPIs and every chart, and resets the count. "update"
          because the count mixes alerts and journey completions. */}
      <NewAlertsBanner count={newCount} onClick={handleRefresh} noun="update" />
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Insights
      </h1>
      <p
        style={{
          fontSize: "16px",
          color: "var(--cc-grey-three)",
          marginTop: "4px",
          marginBottom: "24px",
        }}
      >
        Order pipeline health and alert load across everything ingested so far
      </p>

      {/* KPI row — eight headline numbers. A tile per number, not a chart per
          number: a one-bar bar chart says less in more space. */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(168px, 1fr))",
          gap: "16px",
          marginBottom: "24px",
        }}
      >
        <StatCard
          label="Total journeys"
          value={journeys.total.toLocaleString()}
          hint={inProgress > 0 ? `${inProgress} still in progress` : undefined}
        />
        <StatCard
          label="Success rate"
          value={`${Math.round(journeys.success_rate * 100)}%`}
          hint={finished > 0 ? `of ${finished} finished` : "nothing finished yet"}
        />
        <StatCard
          label="Active alerts"
          value={alerts.open.toLocaleString()}
          hint={`${alerts.resolved} resolved`}
        />
        <StatCard
          label="Critical"
          value={criticalCount.toLocaleString()}
          hint="highest AI severity"
          // Signal color only when there is something to signal; the label says
          // "Critical" either way, so color is never the only cue.
          tone={criticalCount > 0 ? OUTCOME_FAILED : undefined}
        />
        {/* How much alert noise clustering absorbed. The hint carries BOTH the
            numerator and the alert total on purpose: "N of M alerts" is what
            stops the ratio being read as if every alert had been clustered,
            when in fact only failed/timed-out journeys are. */}
        <StatCard
          label="Alerts per incident"
          value={formatRatio(incidents.alerts_per_incident)}
          hint={
            incidents.total > 0
              ? `${incidents.alerts_clustered.toLocaleString()} of ${alerts.total.toLocaleString()} alerts, in ${incidents.total.toLocaleString()} incidents`
              : "no incidents raised yet"
          }
        />
        <StatCard
          label="Open incidents"
          value={openIncidents.toLocaleString()}
          hint={`${incidents.total.toLocaleString()} raised in total`}
          tone={openIncidents > 0 ? OUTCOME_FAILED : undefined}
        />
        <StatCard
          label="Avg duration"
          value={formatDuration(journeys.avg_duration_seconds)}
          hint="first to last log"
        />
        <StatCard
          label="Timed out"
          value={timedOut.toLocaleString()}
          hint="stalled 90s+"
          tone={timedOut > 0 ? OUTCOME_FAILED : undefined}
        />
      </div>

      {/* The lead chart: where orders stop. Horizontal bars because the outcome
          names are long — rotated column labels would be unreadable. */}
      <div style={{ marginBottom: "24px" }}>
        <ChartCard
          title="Where orders stop"
          subtitle="Journeys by final outcome — in-progress journeys are excluded, they haven't stopped anywhere yet"
          keyRow={
            <ColorKey
              items={[
                { color: OUTCOME_SUCCESS, label: "Fulfilled" },
                { color: OUTCOME_FAILED, label: "Stopped by a failure" },
              ]}
            />
          }
        >
          <BreakdownBarChart
            rows={outcomeRows}
            total={outcomeRows.reduce((sum, row) => sum + row.count, 0)}
            yAxisWidth={186}
            emptyMessage="No journey has finished yet — fire the injector to start some flows."
          />
        </ChartCard>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
          gap: "24px",
        }}
      >
        <ChartCard
          title="Alerts by department"
          subtitle="Who the router sent the work to — 'unassigned' is a fallback alert the LLM never routed"
        >
          <BreakdownBarChart
            rows={departmentRows}
            total={alerts.total}
            yAxisWidth={112}
            emptyMessage="No alerts yet."
          />
        </ChartCard>

        <ChartCard
          title="Alerts by severity"
          subtitle="Per-log technical urgency as judged by the router"
          keyRow={
            <ColorKey
              items={[
                { color: SEVERITY_RAMP.critical, label: "More severe" },
                { color: SEVERITY_RAMP.low, label: "Less severe" },
                { color: SEVERITY_UNRATED, label: "Unrated (fallback)" },
              ]}
            />
          }
        >
          <BreakdownBarChart
            rows={severityRows}
            total={alerts.total}
            yAxisWidth={112}
            emptyMessage="No alerts yet."
          />
        </ChartCard>

        <ChartCard
          title="Alert mix"
          subtitle="The four binary splits across all alerts"
        >
          <SplitBar
            label="Level"
            primary={{ label: "errors", count: alerts.by_level.ERROR ?? 0 }}
            secondary={{ label: "warnings", count: alerts.by_level.WARN ?? 0 }}
          />
          <SplitBar
            label="Analysis"
            primary={{ label: "AI-analyzed", count: alerts.by_source.ai ?? 0 }}
            secondary={{ label: "fallback", count: alerts.by_source.fallback ?? 0 }}
          />
          {/* Sits under "Analysis" because it refines that split rather than
              standing beside it: a cached alert IS an AI answer, reused. `cached`
              takes the accent because it is the number this row exists to show —
              the filled share reads directly as the cache hit rate. */}
          <SplitBar
            label="Answer"
            primary={{ label: "cached", count: alerts.cached }}
            secondary={{ label: "fresh", count: alerts.fresh }}
          />
          <SplitBar
            label="Triage"
            primary={{ label: "open", count: alerts.open }}
            secondary={{ label: "resolved", count: alerts.resolved }}
          />
        </ChartCard>
      </div>
    </div>
  );
}
