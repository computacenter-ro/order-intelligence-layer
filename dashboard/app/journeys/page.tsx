"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card, Button } from "@computacenter-ro/style-guide/components";
import { badgeColors, radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { Badge } from "@/components/ui/Badge";
import { EmptyState } from "@/components/ui/EmptyState";
import { SearchInput } from "@/components/alerts/SearchInput";
import { formatTime, formatTimestampFull, humanizeKey } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
import {
  DEFAULT_JOURNEY_FILTERS,
  JOURNEY_OUTCOMES,
  hasActiveJourneyFilters,
  journeyMatchesFilters,
  type JourneyFilters,
} from "@/lib/journeyFilters";
import { highlightRuns } from "@/lib/richText";
import { fetchJourneys } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
import { useWebSocket } from "@/lib/useWebSocket";
import type { Journey, JourneyStatus, WsEvent } from "@/lib/types";

const COLUMN_HEADINGS = ["Status", "Order ID", "Cart Header ID", "Event ID", "Outcome", "Last Seen"];

// "all" is the UI-only sentinel for "no filter"; mapped to undefined before the
// request. The four values mirror the backend Journey.status contract.
const STATUS_OPTIONS: JourneyStatus[] = ["IN_PROGRESS", "SUCCESS", "FAILED", "TIMED_OUT"];

// Options for the header dropdown, "All" first.
const STATUS_MENU: { value: JourneyStatus | "all"; label: string }[] = [
  { value: "all", label: "All Statuses" },
  ...STATUS_OPTIONS.map((s) => ({ value: s, label: JOURNEY_STATUS_LABEL[s] })),
];

// The outcome values live in lib/journeyFilters.ts (which mirrors
// backend/journeys.py); humanizeKey turns SAP_SUBMISSION_FAILED into a readable
// label without a second hand-maintained list to drift.
const OUTCOME_MENU = [
  { value: "all", label: "All Outcomes" },
  ...JOURNEY_OUTCOMES.map((o) => ({ value: o, label: humanizeKey(o) })),
];

export default function JourneysPage() {
  return (
    <Suspense fallback={null}>
      <JourneysPageContent />
    </Suspense>
  );
}

function JourneysPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const highlightId = searchParams.get("highlight");
  const highlightRef = useRef<HTMLTableRowElement>(null);
  // One object for all three filters, so the fetch, the live-event gate and the
  // empty state cannot disagree about what is being filtered. Both status and
  // outcome are filtered from their column header (see below); only where they
  // are rendered differs, not what they mean.
  const [filters, setFilters] = useState<JourneyFilters>(DEFAULT_JOURNEY_FILTERS);
  const { status } = filters;

  // Header-integrated status/outcome filters: the column heading opens a small
  // dropdown instead of a separate control above the table. Only one menu can
  // be open at a time, so a single piece of state (rather than one bool per
  // column) also makes "close the other one" free.
  const [openMenu, setOpenMenu] = useState<"status" | "outcome" | null>(null);
  const statusThRef = useRef<HTMLTableCellElement>(null);
  const outcomeThRef = useRef<HTMLTableCellElement>(null);

  useEffect(() => {
    if (!openMenu) return;
    const activeRef = openMenu === "status" ? statusThRef : outcomeThRef;
    const onDown = (e: MouseEvent) => {
      if (activeRef.current && !activeRef.current.contains(e.target as Node)) {
        setOpenMenu(null);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenMenu(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [openMenu]);

  // The server sorts last_ts DESC and applies the filters; "all" / "" omit theirs.
  const fetchPage = useCallback(
    (cursor: string | null) =>
      fetchJourneys({
        status: filters.status === "all" ? undefined : filters.status,
        outcome: filters.outcome === "all" ? undefined : filters.outcome,
        search: filters.search.trim() || undefined,
        cursor: cursor ?? undefined,
      }),
    [filters]
  );

  const { items, loading, hasMore, loadMore, reload, prepend, remove } = usePagination<Journey>(
    fetchPage,
    (j) => j.journey_id
  );

  // Reload on any filter change, but skip the mount run — the hook already loads
  // once at mount, so reloading here too would double the initial fetch.
  const didMount = useRef(false);
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true;
      return;
    }
    reload();
  }, [filters, reload]);

  useEffect(() => {
    if (highlightId) highlightRef.current?.scrollIntoView({ block: "center" });
  }, [highlightId, items]);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "journey.updated" && event.type !== "journey.completed") return;
      // Always drop the old position first, THEN re-insert only if it still
      // matches. That order is load-bearing: a journey that has just filtered
      // itself out (IN_PROGRESS → SUCCESS while filtering IN_PROGRESS, or an
      // outcome that no longer matches) has to disappear rather than linger.
      // Order is last_ts DESC and it was just touched, so the top is right; the
      // hook dedups.
      remove(event.data.journey_id);
      if (journeyMatchesFilters(event.data, filters)) {
        prepend(event.data);
      }
    },
    [remove, prepend, filters]
  );

  useWebSocket(handleEvent);

  const statusFilterActive = status !== "all";
  const outcomeFilterActive = filters.outcome !== "all";
  const anyFilterActive = hasActiveJourneyFilters(filters);

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Order Journeys
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Each order&apos;s full path through the pipeline — where it went, where it stopped
      </p>

      {/* Controls above the table. Status and Outcome are both filtered from
          their own column header (see below) — only the search box lives here. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: semanticSpacing.sm,
          flexWrap: "wrap",
          marginBottom: semanticSpacing.base,
        }}
      >
        <SearchInput
          value={filters.search}
          onChange={(search) => setFilters((prev) => ({ ...prev, search }))}
          ariaLabel="Search journeys by order, cart header or event id"
          placeholder="Search by id…"
        />
      </div>

      <Card style={{ padding: 0 }}>
        <table className="oil-table">
          <thead>
            <tr>
              {COLUMN_HEADINGS.map((heading) => {
                if (heading === "Status") {
                  return (
                    <th key={heading} ref={statusThRef} style={{ position: "relative" }}>
                      <button
                        type="button"
                        onClick={() => setOpenMenu((cur) => (cur === "status" ? null : "status"))}
                        aria-haspopup="listbox"
                        aria-expanded={openMenu === "status"}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "6px",
                          background: "none",
                          border: "none",
                          padding: 0,
                          font: "inherit",
                          fontWeight: 600,
                          cursor: "pointer",
                          color: statusFilterActive ? "var(--cc-heritage-blue)" : "inherit",
                        }}
                      >
                        Status
                        <span aria-hidden="true" style={{ fontSize: "16px" }}>▾</span>
                        {statusFilterActive && (
                          <span
                            aria-hidden="true"
                            style={{
                              width: "6px",
                              height: "6px",
                              borderRadius: "50%",
                              background: "var(--cc-heritage-blue)",
                            }}
                          />
                        )}
                      </button>
                      {openMenu === "status" && (
                        <div
                          role="listbox"
                          style={{
                            position: "absolute",
                            top: "calc(100% + 4px)",
                            left: 0,
                            zIndex: 20,
                            minWidth: "170px",
                            padding: "4px",
                            background: "var(--cc-cloud-white)",
                            border: "1px solid var(--cc-grey-four)",
                            borderRadius: radii.md,
                            boxShadow: "0 4px 12px rgba(0, 0, 0, 0.12)",
                          }}
                        >
                          {STATUS_MENU.map((opt) => {
                            const active = status === opt.value;
                            return (
                              <button
                                key={opt.value}
                                type="button"
                                role="option"
                                aria-selected={active}
                                onClick={() => {
                                  setFilters((prev) => ({ ...prev, status: opt.value }));
                                  setOpenMenu(null);
                                }}
                                style={{
                                  display: "block",
                                  width: "100%",
                                  textAlign: "left",
                                  padding: "6px 10px",
                                  border: "none",
                                  borderRadius: "6px",
                                  background: active ? "var(--cc-table-header-bg)" : "transparent",
                                  color: "var(--cc-grey-one)",
                                  fontSize: "14px",
                                  fontWeight: active ? 600 : 400,
                                  cursor: "pointer",
                                }}
                              >
                                {opt.label}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </th>
                  );
                }
                if (heading === "Outcome") {
                  return (
                    <th key={heading} ref={outcomeThRef} style={{ position: "relative" }}>
                      <button
                        type="button"
                        onClick={() => setOpenMenu((cur) => (cur === "outcome" ? null : "outcome"))}
                        aria-haspopup="listbox"
                        aria-expanded={openMenu === "outcome"}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: "6px",
                          background: "none",
                          border: "none",
                          padding: 0,
                          font: "inherit",
                          fontWeight: 600,
                          cursor: "pointer",
                          color: outcomeFilterActive ? "var(--cc-heritage-blue)" : "inherit",
                        }}
                      >
                        Outcome
                        <span aria-hidden="true" style={{ fontSize: "16px" }}>▾</span>
                        {outcomeFilterActive && (
                          <span
                            aria-hidden="true"
                            style={{
                              width: "6px",
                              height: "6px",
                              borderRadius: "50%",
                              background: "var(--cc-heritage-blue)",
                            }}
                          />
                        )}
                      </button>
                      {openMenu === "outcome" && (
                        <div
                          role="listbox"
                          style={{
                            position: "absolute",
                            top: "calc(100% + 4px)",
                            left: 0,
                            zIndex: 20,
                            minWidth: "190px",
                            maxHeight: "320px",
                            overflowY: "auto",
                            padding: "4px",
                            background: "var(--cc-cloud-white)",
                            border: "1px solid var(--cc-grey-four)",
                            borderRadius: radii.md,
                            boxShadow: "0 4px 12px rgba(0, 0, 0, 0.12)",
                          }}
                        >
                          {OUTCOME_MENU.map((opt) => {
                            const active = filters.outcome === opt.value;
                            return (
                              <button
                                key={opt.value}
                                type="button"
                                role="option"
                                aria-selected={active}
                                onClick={() => {
                                  setFilters((prev) => ({
                                    ...prev,
                                    outcome: opt.value as JourneyFilters["outcome"],
                                  }));
                                  setOpenMenu(null);
                                }}
                                style={{
                                  display: "block",
                                  width: "100%",
                                  textAlign: "left",
                                  padding: "6px 10px",
                                  border: "none",
                                  borderRadius: "6px",
                                  background: active ? "var(--cc-table-header-bg)" : "transparent",
                                  color: "var(--cc-grey-one)",
                                  fontSize: "14px",
                                  fontWeight: active ? 600 : 400,
                                  cursor: "pointer",
                                }}
                              >
                                {opt.label}
                              </button>
                            );
                          })}
                        </div>
                      )}
                    </th>
                  );
                }
                return <th key={heading}>{heading}</th>;
              })}
            </tr>
          </thead>
          <tbody>
            {items.map((journey) => {
              const isHighlighted = journey.journey_id === highlightId;
              return (
                <tr
                  key={journey.journey_id}
                  ref={isHighlighted ? highlightRef : undefined}
                  role="link"
                  tabIndex={0}
                  onClick={() => router.push(`/journeys/${journey.journey_id}`)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") router.push(`/journeys/${journey.journey_id}`);
                  }}
                  style={
                    isHighlighted
                      ? { boxShadow: `inset 0 0 0 2px ${badgeColors.primary.border}`, background: badgeColors.primary.bg }
                      : undefined
                  }
                >
                  <td>
                    <Badge status={JOURNEY_STATUS_BADGE[journey.status]}>
                      {JOURNEY_STATUS_LABEL[journey.status]}
                    </Badge>
                  </td>
                  {/* highlightRuns, not renderInlineMarkdown: an id is not
                      markdown, and running it through the markdown parser would
                      let a stray backtick or ** in an id eat characters. */}
                  <td className="oil-mono">
                    {journey.order_id ? highlightRuns(journey.order_id, filters.search) : "—"}
                  </td>
                  <td className="oil-mono">
                    {journey.cart_header_id
                      ? highlightRuns(journey.cart_header_id, filters.search)
                      : "—"}
                  </td>
                  <td className="oil-mono">
                    {journey.event_id ? highlightRuns(journey.event_id, filters.search) : "—"}
                  </td>
                  <td>{journey.outcome ?? "—"}</td>
                  <td className="oil-mono" title={formatTimestampFull(journey.last_ts)}>
                    {formatTime(journey.last_ts)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>
      {items.length === 0 && !loading &&
        // Branches on "any filter active", not on status alone: a search with no
        // hits used to claim "no journeys yet — run the injector", which sends the
        // reader off to fix a system that is working fine.
        (anyFilterActive ? (
          <EmptyState title="No journeys match these filters" />
        ) : (
          <EmptyState
            title="No journeys yet"
            hint="Run the injector to start some order flows."
          />
        ))}
      {hasMore && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: "16px" }}>
          <Button variant="secondary" onClick={loadMore} disabled={loading} loading={loading}>
            Load more
          </Button>
        </div>
      )}
    </div>
  );
}
