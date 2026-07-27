"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { Page } from "@/lib/api";

/**
 * A cursor-paginated ("load more") list, decoupled from what it lists.
 *
 * The hook owns the list + cursor + loading/error state; the caller supplies
 * two pure functions: `fetchPage(cursor)` (the caller closes over its own
 * filters, so the hook never knows about them) and `idOf(item)` (the dedup /
 * removal key). Reused by the alert feed, history, and journeys pages.
 */
export interface Paginated<T> {
  items: T[];
  loading: boolean;
  hasMore: boolean;
  error: unknown;
  /** Fetch the next batch via the current next_cursor, de-duplicated by id. */
  loadMore: () => void;
  /** Reset to the first page (cursor=null) — call this on a filter change. */
  reload: () => void;
  /** Insert a live item at the top (deduped) — WS `alert.new` / journeys. */
  prepend: (item: T) => void;
  /** Drop an item by id (e.g. a resolved alert leaving the feed). */
  remove: (id: string) => void;
  /** Merge `updates` into the item with this id in place; no-op if the item
   * isn't currently loaded (e.g. filtered out, or on a page not yet fetched).
   * WS `incident.updated` — counts/last_ts changing on an already-visible row. */
  patch: (id: string, updates: Partial<T>) => void;
}

export function usePagination<T>(
  fetchPage: (cursor: string | null) => Promise<Page<T>>,
  idOf: (item: T) => string,
): Paginated<T> {
  const [items, setItems] = useState<T[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<unknown>(null);

  // fetchPage / idOf can change identity each render (new closure over new
  // filters). Keep them in refs so reload/loadMore stay stable (useCallback
  // deps []) yet always call the latest versions.
  const fetchPageRef = useRef(fetchPage);
  const idOfRef = useRef(idOf);
  useEffect(() => {
    fetchPageRef.current = fetchPage;
    idOfRef.current = idOf;
  });

  // The cursor for the *next* loadMore; null = at the start or no further pages.
  const cursorRef = useRef<string | null>(null);
  // Mirrors `loading` for synchronous guarding (state lags a render).
  const loadingRef = useRef(false);
  // Monotonic request id: only the newest in-flight request may apply its
  // result. A reload during an in-flight loadMore (or a double loadMore) bumps
  // this, so the stale response is dropped instead of corrupting the list.
  const requestIdRef = useRef(0);

  const reload = useCallback(() => {
    const reqId = ++requestIdRef.current;
    cursorRef.current = null;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    fetchPageRef.current(null)
      .then((page) => {
        if (reqId !== requestIdRef.current) return; // superseded
        setItems(page.items);
        cursorRef.current = page.next_cursor;
        setHasMore(page.next_cursor !== null);
        loadingRef.current = false;
        setLoading(false);
      })
      .catch((err) => {
        if (reqId !== requestIdRef.current) return;
        setError(err);
        loadingRef.current = false;
        setLoading(false);
      });
  }, []);

  const loadMore = useCallback(() => {
    if (loadingRef.current) return; // a fetch is already in flight
    const cursor = cursorRef.current;
    if (cursor === null) return; // !hasMore — nothing more to fetch
    const reqId = ++requestIdRef.current;
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    fetchPageRef.current(cursor)
      .then((page) => {
        if (reqId !== requestIdRef.current) return; // superseded (e.g. by reload)
        setItems((prev) => {
          const seen = new Set(prev.map(idOfRef.current));
          const fresh = page.items.filter((it) => !seen.has(idOfRef.current(it)));
          return [...prev, ...fresh];
        });
        cursorRef.current = page.next_cursor;
        setHasMore(page.next_cursor !== null);
        loadingRef.current = false;
        setLoading(false);
      })
      .catch((err) => {
        if (reqId !== requestIdRef.current) return;
        setError(err);
        loadingRef.current = false;
        setLoading(false);
      });
  }, []);

  const prepend = useCallback((item: T) => {
    setItems((prev) => {
      const id = idOfRef.current(item);
      if (prev.some((it) => idOfRef.current(it) === id)) return prev;
      return [item, ...prev];
    });
  }, []);

  const remove = useCallback((id: string) => {
    setItems((prev) => prev.filter((it) => idOfRef.current(it) !== id));
  }, []);

  const patch = useCallback((id: string, updates: Partial<T>) => {
    setItems((prev) =>
      prev.map((it) => (idOfRef.current(it) === id ? { ...it, ...updates } : it))
    );
  }, []);

  // Initial load at mount. `reload` is stable, so this fires exactly once; the
  // page drives subsequent reloads itself when its filters change.
  useEffect(() => {
    reload();
  }, [reload]);

  return { items, loading, hasMore, error, loadMore, reload, prepend, remove, patch };
}
