"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@computacenter-ro/style-guide/components";
import { fetchAlerts, fetchFacets, resolveAlert, type AlertFacets, type Page } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
import { useWebSocket } from "@/lib/useWebSocket";
import { AlertCard } from "@/components/alerts/AlertCard";
import { AlertDetailDrawer } from "@/components/alerts/AlertDetailDrawer";
import { NewAlertsBanner } from "@/components/alerts/NewAlertsBanner";
import {
  AlertFilterBar,
  DEFAULT_ALERT_FILTERS,
  alertMatchesFilters,
  hasActiveFilters,
  sanitizeAlertFilters,
  toAlertsQuery,
  type AlertFilters,
} from "@/components/alerts/AlertFilterBar";
import { EmptyState } from "@/components/ui/EmptyState";
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
      // search is forced back to "" rather than restored: the dropdown selections
      // are a standing preference ("I work on backend criticals"), but a search
      // term is a one-off lookup. Reopening the tab to a pre-filtered list with an
      // empty-looking cause is the confusing case this avoids.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (raw) setFilters({ ...sanitizeAlertFilters(JSON.parse(raw)), search: "" });
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
        // toAlertsQuery is shared with the facet fetch below, so the counts always
        // describe this exact query. It resolves the rolling `since` per call, so
        // the window re-anchors on every fetch — including each "Load more" page.
        ...toAlertsQuery(filters, false),
        sort: "emitted_at",
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
  const facetsRequestRef = useRef(0);

  const loadFacets = useCallback(() => {
    // Self-guarding rather than relying on each caller: filters can change — or a
    // reveal can fire — faster than the request returns, and a slow earlier
    // response landing last would leave stale counts on screen. Stamping every
    // request and applying only the newest protects the effect and the reveal
    // alike, so a future third caller can't forget to.
    const requestId = ++facetsRequestRef.current;
    fetchFacets(toAlertsQuery(filters, false))
      .then((next) => {
        if (requestId === facetsRequestRef.current) setFacets(next);
      })
      .catch((err) => {
        // Counts are an enhancement — the filters and the list work without them,
        // so a failure keeps the last known values rather than breaking the page.
        console.error("Failed to load alert facets:", err);
      });
  }, [filters]);

  // loadFacets' identity changes with `filters`, so this refires on every filter
  // change as well as once filters become ready.
  useEffect(() => {
    if (!filtersReady) return;
    loadFacets();
  }, [filtersReady, loadFacets]);

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
    // The revealed alerts change what the filter counts should say, and nothing
    // else would refresh them — the facets effect keys on `filters`, which a
    // reveal doesn't touch. Without this the pills keep the numbers from before
    // the burst arrived.
    loadFacets();
  }, [pending, prepend, loadFacets]);

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
      <AlertFilterBar value={filters} onChange={handleFiltersChange} facets={facets} />
      <div>
        <NewAlertsBanner count={pending.length} onClick={handleReveal} />
        {items.length === 0 && !loading ? (
          hasActiveFilters(filters) ? (
            <EmptyState
              title="No alerts match your filters"
              hint="Try clearing or widening the filters above."
            />
          ) : (
            <EmptyState
              title="No active alerts"
              hint="New WARN / ERROR alerts show up here in real time — fire the injector to generate some flows."
            />
          )
        ) : (
          /* Server returns emitted_at DESC; prepend keeps live alerts on top. */
          items.map((alert) => (
            <AlertCard
              key={alert.alert_id}
              alert={alert}
              onOpen={setSelected}
              onResolve={handleResolve}
              isSelected={selected?.alert_id === alert.alert_id}
              search={filters.search.trim()}
            />
          ))
        )}
      </div>
      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: "16px" }}>
          <Button variant="secondary" onClick={loadMore} disabled={loading} loading={loading}>
            Load more
          </Button>
        </div>
      )}
      <AlertDetailDrawer
        alert={selected}
        onClose={() => setSelected(null)}
        search={filters.search.trim()}
      />
    </div>
  );
}
