"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card, Button } from "@computacenter-ro/style-guide/components";
import { badgeColors, radii } from "@computacenter-ro/style-guide/tokens";
import { Badge } from "@/components/ui/Badge";
import { formatTime, formatTimestampFull } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
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
  const [status, setStatus] = useState<JourneyStatus | "all">("all");

  // Header-integrated status filter: the "Status" column heading opens a small
  // dropdown instead of a separate control above the table.
  const [statusMenuOpen, setStatusMenuOpen] = useState(false);
  const statusThRef = useRef<HTMLTableCellElement>(null);

  useEffect(() => {
    if (!statusMenuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (statusThRef.current && !statusThRef.current.contains(e.target as Node)) {
        setStatusMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setStatusMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [statusMenuOpen]);

  // The server sorts last_ts DESC and applies the status filter; "all" omits it.
  const fetchPage = useCallback(
    (cursor: string | null) =>
      fetchJourneys({ status: status === "all" ? undefined : status, cursor: cursor ?? undefined }),
    [status]
  );

  const { items, loading, hasMore, loadMore, reload, prepend, remove } = usePagination<Journey>(
    fetchPage,
    (j) => j.journey_id
  );

  // Reload on status change, but skip the mount run — the hook already loads
  // once at mount, so reloading here too would double the initial fetch.
  const didMount = useRef(false);
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true;
      return;
    }
    reload();
  }, [status, reload]);

  useEffect(() => {
    if (highlightId) highlightRef.current?.scrollIntoView({ block: "center" });
  }, [highlightId, items]);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "journey.updated" && event.type !== "journey.completed") return;
      // Always drop the old position first. Re-insert at the top only if the
      // journey still matches the active filter — so one that just changed to a
      // non-matching status (e.g. IN_PROGRESS → SUCCESS while filtering
      // IN_PROGRESS) correctly disappears from the filtered view. Order is
      // last_ts DESC and it was just touched, so the top is right; the hook dedups.
      remove(event.data.journey_id);
      if (status === "all" || event.data.status === status) {
        prepend(event.data);
      }
    },
    [remove, prepend, status]
  );

  useWebSocket(handleEvent);

  const filterActive = status !== "all";

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Order Journeys
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Each order&apos;s full path through the pipeline — where it went, where it stopped
      </p>
      <Card style={{ padding: 0 }}>
        <table className="oil-table">
          <thead>
            <tr>
              {COLUMN_HEADINGS.map((heading) =>
                heading === "Status" ? (
                  <th key={heading} ref={statusThRef} style={{ position: "relative" }}>
                    <button
                      type="button"
                      onClick={() => setStatusMenuOpen((open) => !open)}
                      aria-haspopup="listbox"
                      aria-expanded={statusMenuOpen}
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
                        color: filterActive ? "var(--cc-heritage-blue)" : "inherit",
                      }}
                    >
                      Status
                      <span aria-hidden="true" style={{ fontSize: "16px" }}>▾</span>
                      {filterActive && (
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
                    {statusMenuOpen && (
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
                                setStatus(opt.value);
                                setStatusMenuOpen(false);
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
                ) : (
                  <th key={heading}>{heading}</th>
                )
              )}
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
                  <td className="oil-mono">{journey.order_id ?? "—"}</td>
                  <td className="oil-mono">{journey.cart_header_id ?? "—"}</td>
                  <td className="oil-mono">{journey.event_id ?? "—"}</td>
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
