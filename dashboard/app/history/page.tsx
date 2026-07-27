"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@computacenter-ro/style-guide/components";
import { fetchAlerts, type Page } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
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

// Resolves without a network call — used before filters are rehydrated so the
// hook's mount load doesn't fetch with the pre-rehydration default filter.
const EMPTY_PAGE: Page<ProcessedAlert> = { items: [], next_cursor: null };

export default function HistoryPage() {
  const [selected, setSelected] = useState<ProcessedAlert | null>(null);
  const [filters, setFilters] = useState<AlertFilters>(DEFAULT_ALERT_FILTERS);
  // Gates fetching until localStorage has been read (see feed page for the
  // hydration-mismatch rationale).
  const [filtersReady, setFiltersReady] = useState(false);

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

  useEffect(() => {
    if (!filtersReady) return;
    try {
      localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters));
    } catch {
      // storage unavailable (private mode / quota) — non-fatal
    }
  }, [filters, filtersReady]);

  // History lists resolved alerts, most-recently-resolved first (the server
  // sorts on resolved_at). Not live — no WS.
  const fetchPage = useCallback(
    (cursor: string | null): Promise<Page<ProcessedAlert>> => {
      if (!filtersReady) return Promise.resolve(EMPTY_PAGE);
      return fetchAlerts({
        department: filters.department === "all" ? undefined : filters.department,
        source: filters.source === "all" ? undefined : filters.source,
        level: filters.level === "all" ? undefined : filters.level,
        app_name: filters.app_name === "all" ? undefined : filters.app_name,
        severity: filters.severity === "all" ? undefined : filters.severity,
        // "all" omits the param; otherwise "cached" -> true, "fresh" -> false.
        cached: filters.cached === "all" ? undefined : filters.cached === "cached",
        resolved: true,
        sort: "resolved_at",
        cursor: cursor ?? undefined,
      });
    },
    [filters, filtersReady]
  );

  const { items, loading, hasMore, loadMore, reload } = usePagination<ProcessedAlert>(
    fetchPage,
    (a) => a.alert_id
  );

  useEffect(() => {
    if (!filtersReady) return;
    reload();
  }, [filters, filtersReady, reload]);

  // Resolved alerts never re-enter the "mark resolved" flow from here — the
  // kebab menu already renders a plain "Resolved" indicator once is_resolved
  // is true, so this is never actually invoked.
  const handleResolve = useCallback(() => {}, []);

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
        {items.length === 0 && !loading && (
          <p style={{ color: "var(--cc-grey-three)" }}>No resolved alerts yet</p>
        )}
        {items.map((alert) => (
          <AlertCard
            key={alert.alert_id}
            alert={alert}
            onOpen={setSelected}
            onResolve={handleResolve}
            isSelected={selected?.alert_id === alert.alert_id}
          />
        ))}
      </div>
      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: "16px" }}>
          <Button variant="secondary" onClick={loadMore} disabled={loading} loading={loading}>
            Load more
          </Button>
        </div>
      )}
      <AlertDetailDrawer alert={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
