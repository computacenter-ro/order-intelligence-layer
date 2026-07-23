"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchAlerts } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import { NewAlertsBanner } from "@/components/alerts/NewAlertsBanner";
import {
  AlertFilterBar,
  DEFAULT_ALERT_FILTERS,
  alertMatchesFilters,
  sanitizeAlertFilters,
  type AlertFilters,
} from "@/components/alerts/AlertFilterBar";
import type { ProcessedAlert, WsEvent } from "@/lib/types";

const FILTERS_STORAGE_KEY = "oil.alertFilters";

export default function AlertFeedPage() {
  const [alerts, setAlerts] = useState<ProcessedAlert[]>([]);
  const [pending, setPending] = useState<ProcessedAlert[]>([]);
  const [selected, setSelected] = useState<ProcessedAlert | null>(null);
  const [filters, setFilters] = useState<AlertFilters>(DEFAULT_ALERT_FILTERS);
  // Gates the fetch until localStorage has been read, so the list loads once
  // with the rehydrated selection instead of flashing the default filter first.
  const [filtersReady, setFiltersReady] = useState(false);

  // Rehydrate the saved selection after mount, not during the initial render:
  // reading localStorage synchronously (e.g. a useState lazy initializer) would
  // make the client's first paint diverge from the server's (no localStorage),
  // causing a hydration mismatch. Effects run after hydration, so this is safe.
  // Runs exactly once, then unblocks the fetch effect below.
  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(FILTERS_STORAGE_KEY);
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (raw) setFilters(sanitizeAlertFilters(JSON.parse(raw)));
    } catch {
      // corrupt/absent storage — fall back to the defaults already in state
    }
    setFiltersReady(true);
  }, []);

  // Persist the selection whenever it changes (but not the pre-rehydration default).
  useEffect(() => {
    if (!filtersReady) return;
    try {
      localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters));
    } catch {
      // storage unavailable (private mode / quota) — non-fatal
    }
  }, [filters, filtersReady]);

  // (Re)load the list from the backend whenever the filter changes. "all" maps
  // to undefined so fetchAlerts omits the query param entirely.
  useEffect(() => {
    if (!filtersReady) return;
    let stale = false;
    fetchAlerts({
      department: filters.department === "all" ? undefined : filters.department,
      source: filters.source === "all" ? undefined : filters.source,
      level: filters.level === "all" ? undefined : filters.level,
      app_name: filters.app_name === "all" ? undefined : filters.app_name,
      severity: filters.severity === "all" ? undefined : filters.severity,
    })
      .then((next) => {
        if (!stale) setAlerts(next);
      })
      .catch((err) => console.error("Failed to load alerts:", err));
    return () => {
      stale = true;
    };
  }, [filters, filtersReady]);

  const handleFiltersChange = useCallback((next: AlertFilters) => {
    setFilters(next);
    // Drop live alerts captured under the previous filter; the re-fetched list
    // already reflects the new filter, and future WS alerts are re-guarded below.
    setPending([]);
  }, []);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "alert.new") return;
      // Critical: a live alert enters the feed only if it matches the active
      // filter — otherwise a non-matching alert would leak into a filtered view.
      if (!alertMatchesFilters(event.data, filters)) return;
      if (alerts.some((a) => a.alert_id === event.data.alert_id)) return;
      setPending((prev) =>
        prev.some((a) => a.alert_id === event.data.alert_id) ? prev : [event.data, ...prev]
      );
    },
    [alerts, filters]
  );

  useWebSocket(handleEvent);

  const handleReveal = useCallback(() => {
    setAlerts((prev) => [...pending, ...prev]);
    setPending([]);
  }, [pending]);

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
      <AlertFilterBar value={filters} onChange={handleFiltersChange} />
      <div>
        <NewAlertsBanner count={pending.length} onReveal={handleReveal} />
        {sorted.map((alert) => (
          <AlertCard
            key={alert.alert_id}
            alert={alert}
            onOpen={setSelected}
            isSelected={selected?.alert_id === alert.alert_id}
          />
        ))}
      </div>
      <AlertDetailDrawer alert={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
