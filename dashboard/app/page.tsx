"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@computacenter-ro/style-guide/components";
import { fetchAlerts, resolveAlert, type Page } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
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

// Resolves without a network call — used before filters are rehydrated so the
// hook's mount load doesn't fetch with the pre-rehydration default filter.
const EMPTY_PAGE: Page<ProcessedAlert> = { items: [], next_cursor: null };

export default function AlertFeedPage() {
  const [pending, setPending] = useState<ProcessedAlert[]>([]);
  const [selected, setSelected] = useState<ProcessedAlert | null>(null);
  const [filters, setFilters] = useState<AlertFilters>(DEFAULT_ALERT_FILTERS);
  // Gates fetching until localStorage has been read, so the list loads once with
  // the rehydrated selection instead of flashing the default filter first.
  const [filtersReady, setFiltersReady] = useState(false);

  // Rehydrate the saved selection after mount (not during render): reading
  // localStorage synchronously would diverge the client's first paint from the
  // server's (no localStorage) and cause a hydration mismatch. Effects run after
  // hydration, so this is safe. Runs once, then unblocks fetching below.
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

  // The page owns the filters; the hook is filter-agnostic and just pages over
  // whatever this closure fetches. "all" maps to undefined so the param is
  // omitted. The feed only ever shows active (unresolved) alerts, newest first.
  const fetchPage = useCallback(
    (cursor: string | null): Promise<Page<ProcessedAlert>> => {
      if (!filtersReady) return Promise.resolve(EMPTY_PAGE);
      return fetchAlerts({
        department: filters.department === "all" ? undefined : filters.department,
        source: filters.source === "all" ? undefined : filters.source,
        level: filters.level === "all" ? undefined : filters.level,
        app_name: filters.app_name === "all" ? undefined : filters.app_name,
        severity: filters.severity === "all" ? undefined : filters.severity,
        resolved: false,
        sort: "emitted_at",
        cursor: cursor ?? undefined,
      });
    },
    [filters, filtersReady]
  );

  const { items, loading, hasMore, loadMore, reload, prepend, remove } =
    usePagination<ProcessedAlert>(fetchPage, (a) => a.alert_id);

  // (Re)load the first page once filters are ready and on every filter change.
  useEffect(() => {
    if (!filtersReady) return;
    reload();
  }, [filters, filtersReady, reload]);

  const handleFiltersChange = useCallback((next: AlertFilters) => {
    setFilters(next);
    // Drop live alerts captured under the previous filter; the re-fetched list
    // reflects the new filter, and future WS alerts are re-guarded below.
    setPending([]);
  }, []);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "alert.new") return;
      // A live alert enters the feed only if it matches the active filter —
      // otherwise a non-matching alert would leak into a filtered view.
      if (!alertMatchesFilters(event.data, filters)) return;
      setPending((prev) =>
        prev.some((a) => a.alert_id === event.data.alert_id) ? prev : [event.data, ...prev]
      );
    },
    [filters]
  );

  useWebSocket(handleEvent);

  const handleReveal = useCallback(() => {
    // pending is newest-first; prepend inserts at the top, so replay
    // oldest-first to leave the newest alert on top. The hook dedups.
    [...pending].reverse().forEach(prepend);
    setPending([]);
  }, [pending, prepend]);

  const handleResolve = useCallback(
    (alert: ProcessedAlert) => {
      resolveAlert(alert.alert_id)
        .then((updated) => {
          // Resolved alerts move to History — drop it from the live feed.
          remove(updated.alert_id);
        })
        .catch((err) => console.error("Failed to resolve alert:", err));
    },
    [remove]
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
        {/* Server returns emitted_at DESC; prepend keeps live alerts on top. */}
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
