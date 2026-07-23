"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchAlerts, resolveAlert } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import { NewAlertsBanner } from "@/components/alerts/NewAlertsBanner";
import type { ProcessedAlert, WsEvent } from "@/lib/types";

export default function AlertFeedPage() {
  const [alerts, setAlerts] = useState<ProcessedAlert[]>([]);
  const [pending, setPending] = useState<ProcessedAlert[]>([]);
  const [selected, setSelected] = useState<ProcessedAlert | null>(null);

  useEffect(() => {
    // Resolved alerts live in History (see /history) — the feed only ever
    // shows active (unresolved) alerts.
    fetchAlerts({ resolved: false })
      .then(setAlerts)
      .catch((err) => console.error("Failed to load alerts:", err));
  }, []);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "alert.new") return;
      if (alerts.some((a) => a.alert_id === event.data.alert_id)) return;
      setPending((prev) =>
        prev.some((a) => a.alert_id === event.data.alert_id) ? prev : [event.data, ...prev]
      );
    },
    [alerts]
  );

  useWebSocket(handleEvent);

  const handleReveal = useCallback(() => {
    setAlerts((prev) => [...pending, ...prev]);
    setPending([]);
  }, [pending]);

  const handleResolve = useCallback((alert: ProcessedAlert) => {
    resolveAlert(alert.alert_id)
      .then((updated) => {
        // Resolved alerts move to History — drop it from the live feed.
        setAlerts((prev) => prev.filter((a) => a.alert_id !== updated.alert_id));
      })
      .catch((err) => console.error("Failed to resolve alert:", err));
  }, []);

  const sorted = [...alerts].sort(
    (a, b) => new Date(b.emitted_at).getTime() - new Date(a.emitted_at).getTime()
  );

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Alert Feed
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Real-time WARN / ERROR alerts, explained in plain English
      </p>
      <div>
        <NewAlertsBanner count={pending.length} onReveal={handleReveal} />
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
