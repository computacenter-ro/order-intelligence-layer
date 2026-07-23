"use client";

import { useEffect, useState } from "react";
import { Card } from "@computacenter-ro/style-guide/components";
import { Badge } from "@/components/ui/Badge";
import { formatTime, levelLabel, capitalize } from "@/lib/format";
import { fetchAlerts } from "@/lib/api";
import type { BadgeStatus, ProcessedAlert } from "@/lib/types";

const COLUMN_HEADINGS = ["Level", "Service", "Explanation", "Department", "Resolved At"];

export default function HistoryPage() {
  const [alerts, setAlerts] = useState<ProcessedAlert[]>([]);

  useEffect(() => {
    fetchAlerts({ resolved: true })
      .then(setAlerts)
      .catch((err) => console.error("Failed to load resolved alerts:", err));
  }, []);

  const sorted = [...alerts].sort(
    (a, b) => new Date(b.resolved_at ?? 0).getTime() - new Date(a.resolved_at ?? 0).getTime()
  );

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Resolved Alerts History
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Alerts marked resolved from the Alert Feed
      </p>
      <Card style={{ padding: 0 }}>
        <table className="oil-table">
          <thead>
            <tr>
              {COLUMN_HEADINGS.map((heading) => (
                <th key={heading}>{heading}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr>
                <td colSpan={COLUMN_HEADINGS.length} style={{ textAlign: "center", color: "var(--cc-grey-three)", cursor: "default" }}>
                  No resolved alerts yet
                </td>
              </tr>
            )}
            {sorted.map((alert) => (
              <tr key={alert.alert_id} style={{ cursor: "default" }}>
                <td>
                  <Badge status={(alert.level === "ERROR" ? "error" : "warning") as BadgeStatus}>
                    {levelLabel(alert.level)}
                  </Badge>
                </td>
                <td className="oil-mono">{alert.app_name}</td>
                <td>{alert.explanation ?? "—"}</td>
                <td>{alert.department ? capitalize(alert.department) : "—"}</td>
                <td className="oil-mono">{alert.resolved_at ? formatTime(alert.resolved_at) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
