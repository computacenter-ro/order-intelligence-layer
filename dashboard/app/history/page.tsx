"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchAlerts } from "@/lib/api";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import type { ProcessedAlert } from "@/lib/types";

export default function HistoryPage() {
  const [alerts, setAlerts] = useState<ProcessedAlert[]>([]);
  const [selected, setSelected] = useState<ProcessedAlert | null>(null);

  useEffect(() => {
    fetchAlerts({ resolved: true })
      .then(setAlerts)
      .catch((err) => console.error("Failed to load resolved alerts:", err));
  }, []);

  // Resolved alerts never re-enter the "mark resolved" flow from here — the
  // kebab menu already renders a plain "Resolved" indicator once is_resolved
  // is true, so this is never actually invoked.
  const handleResolve = useCallback(() => {}, []);

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
      <div>
        {sorted.length === 0 && (
          <p style={{ color: "var(--cc-grey-three)" }}>No resolved alerts yet</p>
        )}
        {sorted.map((alert) => (
          <AlertCard
            key={alert.alert_id}
            alert={alert}
            onOpen={setSelected}
            onResolve={handleResolve}
            isSelected={selected?.alert_id === alert.alert_id}
          />
        ))}
      </div>
      <AlertDetailDrawer alert={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
