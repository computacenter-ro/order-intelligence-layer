"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchAlerts } from "@/lib/api";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import {
  AlertFilterBar,
  DEFAULT_ALERT_FILTERS,
  sanitizeAlertFilters,
  type AlertFilters,
} from "@/components/alerts/AlertFilterBar";
import type { ProcessedAlert } from "@/lib/types";

// Separate from the feed's "oil.alertFilters" so History and Feed remember their
// filter selections independently.
const FILTERS_STORAGE_KEY = "oil.historyFilters";

export default function HistoryPage() {
  const [alerts, setAlerts] = useState<ProcessedAlert[]>([]);
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

  // (Re)load the resolved list from the backend whenever the filter changes.
  // "all" maps to undefined so fetchAlerts omits the query param entirely.
  // History is not live (no WS), so a plain re-fetch on filter change is enough —
  // no live-alert guarding / pending logic is needed.
  useEffect(() => {
    if (!filtersReady) return;
    let stale = false;
    fetchAlerts({
      department: filters.department === "all" ? undefined : filters.department,
      source: filters.source === "all" ? undefined : filters.source,
      level: filters.level === "all" ? undefined : filters.level,
      app_name: filters.app_name === "all" ? undefined : filters.app_name,
      severity: filters.severity === "all" ? undefined : filters.severity,
      resolved: true,
    })
      .then((next) => {
        if (!stale) setAlerts(next);
      })
      .catch((err) => console.error("Failed to load resolved alerts:", err));
    return () => {
      stale = true;
    };
  }, [filters, filtersReady]);

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
      <AlertFilterBar value={filters} onChange={setFilters} />
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
