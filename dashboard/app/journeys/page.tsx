"use client";

import { Suspense, useCallback, useEffect, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card, Button } from "@computacenter-ro/style-guide/components";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { Badge } from "@/components/ui/Badge";
import { formatTime } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
import { fetchJourneys } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
import { useWebSocket } from "@/lib/useWebSocket";
import type { Journey, WsEvent } from "@/lib/types";

const COLUMN_HEADINGS = ["Status", "Order ID", "Cart Header ID", "Event ID", "Outcome", "Last Seen"];

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
  const highlightRef = useRef<HTMLTableRowElement | null>(null);

  // No filters here, so fetchPage is stable; the server returns last_ts DESC.
  const fetchPage = useCallback(
    (cursor: string | null) => fetchJourneys({ cursor: cursor ?? undefined }),
    []
  );

  const { items, loading, hasMore, loadMore, prepend, remove } = usePagination<Journey>(
    fetchPage,
    (j) => j.journey_id
  );

  useEffect(() => {
    if (highlightId) highlightRef.current?.scrollIntoView({ block: "center" });
  }, [highlightId, items]);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "journey.updated" && event.type !== "journey.completed") return;
      // A live update moves the journey to the top (order is last_ts DESC, and it
      // was just touched): drop the old position, then prepend the fresh row.
      // remove-then-prepend keeps the hook's dedup happy.
      remove(event.data.journey_id);
      prepend(event.data);
    },
    [remove, prepend]
  );

  useWebSocket(handleEvent);

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
              {COLUMN_HEADINGS.map((heading) => (
                <th key={heading}>{heading}</th>
              ))}
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
                <td className="oil-mono">{formatTime(journey.last_ts)}</td>
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
