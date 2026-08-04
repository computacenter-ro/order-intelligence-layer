"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@computacenter-ro/style-guide/components";
import {
  alertLoadErrorMessage,
  fetchAlert,
  fetchAlerts,
  fetchFacets,
  resolveAlert,
  type AlertFacets,
  type Page,
} from "@/lib/api";
import { ALERT_PARAM, alertIdFromParam } from "@/lib/alertDeepLink";
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
  // Deep-link failure (404 / expired session), shown in place of drawer content.
  // Null in the normal case, including while the fetch is in flight.
  const [deepLinkError, setDeepLinkError] = useState<string | null>(null);
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

  // --- `/?alert=<alert_id>` deep link (where a Teams card lands) --------------
  //
  // Fetched BY ID, never looked up in `items`: the alert may be filtered out by the
  // active filters, older than the first page, or resolved (resolved alerts are not
  // in this feed at all — they live in /history). Landing on / is still right, since
  // the drawer's content comes from the fetch; the list behind it simply omits the
  // row. Deliberately independent of `filtersReady` for the same reason — the alert
  // is addressed by id, not matched by query, so it opens whatever the filters say.
  //
  // `useSearchParams` in a prerendered route client-renders the component tree up
  // to the nearest Suspense boundary, and Next's docs suggest adding one. Checked
  // rather than assumed: the built HTML for `/` contains none of this page and none
  // of the side-nav even WITHOUT this hook (compare `/history`) — the whole
  // dashboard is client-rendered already, since filters come from localStorage and
  // every list from the API. So there is nothing left to prerender and no Suspense
  // wrapper to justify; the hook is used directly for the value it adds, which is
  // reacting to the param rather than reading `window.location` once on mount.
  const router = useRouter();
  const searchParams = useSearchParams();
  const deepLinkId = alertIdFromParam(searchParams.get(ALERT_PARAM));

  useEffect(() => {
    if (!deepLinkId) return;
    let cancelled = false;
    fetchAlert(deepLinkId)
      .then((alert) => {
        if (cancelled) return;
        setSelected(alert);
      })
      .catch((err) => {
        if (cancelled) return;
        // A card can outlive its alert, so a 404 is expected rather than
        // exceptional: show why the drawer is empty instead of an empty drawer.
        setDeepLinkError(alertLoadErrorMessage(err));
      });
    // Guards against a second `?alert=` arriving before the first resolves, so a
    // slow earlier response cannot overwrite the newer selection.
    return () => {
      cancelled = true;
    };
  }, [deepLinkId]);

  // Closing the drawer drops the param, so a refresh or a Back press does not
  // reopen it. `replace`, not `push`: the deep-linked URL should not become a
  // history entry the user has to step back through. Replacing with a bare "/" is
  // safe because `alert` is the only param this route reads — the filters live in
  // localStorage, not the URL.
  const closeDetail = useCallback(() => {
    setSelected(null);
    setDeepLinkError(null);
    if (deepLinkId) router.replace("/", { scroll: false });
  }, [deepLinkId, router]);

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
          // Resolved alerts move to History — drop it from the live feed. A no-op
          // when the alert was never in the loaded list, which is the normal case
          // for one opened by deep link (filtered out, or past the first page).
          remove(updated.alert_id);
          // Close if the drawer was showing this alert. Resolving from the drawer
          // must behave exactly like resolving from the card, and the row it
          // described is gone — so leaving it open would show a stale record.
          setSelected((prev) => (prev?.alert_id === updated.alert_id ? null : prev));
          if (deepLinkId === updated.alert_id) router.replace("/", { scroll: false });
        })
        .catch((err) => console.error("Failed to resolve alert:", err));
    },
    [remove, deepLinkId, router]
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
        onClose={closeDetail}
        search={filters.search.trim()}
        onResolve={handleResolve}
        error={deepLinkError}
      />
    </div>
  );
}
