"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Card } from "@computacenter-ro/style-guide/components";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { Badge } from "@/components/ui/Badge";
import { formatTime } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
import { fetchJourneys } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import type { Journey, WsEvent } from "@/lib/types";

const COLUMN_HEADINGS = ["Status", "Order ID", "Cart Header ID", "Event ID", "Outcome", "Last Seen"];

function upsertJourney(prev: Journey[], next: Journey): Journey[] {
  const index = prev.findIndex((j) => j.journey_id === next.journey_id);
  if (index === -1) return [next, ...prev];
  const copy = [...prev];
  copy[index] = next;
  return copy;
}

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
  const [journeys, setJourneys] = useState<Journey[]>([]);

  useEffect(() => {
    fetchJourneys()
      .then(setJourneys)
      .catch((err) => console.error("Failed to load journeys:", err));
  }, []);

  useEffect(() => {
    if (highlightId) highlightRef.current?.scrollIntoView({ block: "center" });
  }, [highlightId, journeys]);

  const handleEvent = useCallback((event: WsEvent) => {
    if (event.type !== "journey.updated" && event.type !== "journey.completed") return;
    setJourneys((prev) => upsertJourney(prev, event.data));
  }, []);

  useWebSocket(handleEvent);

  const sorted = [...journeys].sort(
    (a, b) => new Date(b.last_ts).getTime() - new Date(a.last_ts).getTime()
  );

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Order Journeys
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Each order&apos;s full path through the pipeline — where it went, where it stopped
      </p>
      <Card style={{ padding: 0 }}>
        <table className="oil-journeys-table">
          <thead>
            <tr>
              {COLUMN_HEADINGS.map((heading) => (
                <th key={heading}>{heading}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.map((journey) => {
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
    </div>
  );
}
