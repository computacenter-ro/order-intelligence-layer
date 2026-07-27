"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@computacenter-ro/style-guide/components";
import { fetchAlerts, fetchFacets, type AlertFacets, type Page } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import {
  AlertFilterBar,
  DEFAULT_ALERT_FILTERS,
  sanitizeAlertFilters,
  toAlertsQuery,
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
        // toAlertsQuery is shared with the facet fetch below, so the counts always
        // describe this exact query. It resolves the rolling `since` per call, so
        // the window re-anchors on every fetch — including each "Load more" page.
        // Note `since` bounds `emitted_at` (when the alert fired), not
        // `resolved_at` (what History sorts on): "Last 24h" means alerts raised in
        // the last day, the same reading as on the feed.
        ...toAlertsQuery(filters, true),
        sort: "resolved_at",
        cursor: cursor ?? undefined,
      });
    },
    [filters, filtersReady]
  );

  // Contextual per-value counts for the three multi-select filters. Fetched with
  // the SAME filters as the list (via toAlertsQuery), so the numbers annotate this
  // exact query; the backend applies exclude-self per facet, which is why the
  // current multi-select ticks are sent rather than withheld.
  const [facets, setFacets] = useState<AlertFacets | null>(null);

  useEffect(() => {
    if (!filtersReady) return;
    // Filters can change faster than the request returns; without this guard a
    // slow earlier response could land last and leave stale counts on screen.
    let current = true;
    fetchFacets(toAlertsQuery(filters, true))
      .then((next) => {
        if (current) setFacets(next);
      })
      .catch((err) => {
        // Counts are an enhancement — the filters and the list work without them,
        // so a failure keeps the last known values rather than breaking the page.
        console.error("Failed to load alert facets:", err);
      });
    return () => {
      current = false;
    };
  }, [filters, filtersReady]);

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
      <AlertFilterBar value={filters} onChange={setFilters} facets={facets} />
      <div>
        {items.length === 0 && !loading && (
          <p style={{ color: "var(--cc-grey-three)" }}>No resolved alerts yet.</p>
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
