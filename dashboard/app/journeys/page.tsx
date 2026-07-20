"use client";

import { Card } from "@computacenter-ro/style-guide/components";
import { Badge } from "@/components/ui/Badge";
import { formatTime } from "@/lib/format";
import type { BadgeStatus, Journey, JourneyStatus } from "@/lib/types";
import { journeys } from "@/lib/fixtures";

const STATUS_BADGE: Record<JourneyStatus, BadgeStatus> = {
  success: "success",
  failed: "error",
  timed_out: "pending",
  in_progress: "info",
};

const STATUS_LABEL: Record<JourneyStatus, string> = {
  success: "Success",
  failed: "Failed",
  timed_out: "Timed out",
  in_progress: "In progress",
};

const COLUMN_HEADINGS = ["Status", "Order ID", "Event ID", "Outcome", "Stopped At", "Last Seen"];

function stoppedAt(journey: Journey): string {
  const lastEvent = journey.events[journey.events.length - 1];
  return lastEvent ? lastEvent.raw.app_name : "—";
}

export default function JourneysPage() {
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
            {sorted.map((journey) => (
              <tr key={journey.journey_id}>
                <td>
                  <Badge status={STATUS_BADGE[journey.status]}>{STATUS_LABEL[journey.status]}</Badge>
                </td>
                <td className="oil-mono">{journey.order_id ?? "—"}</td>
                <td className="oil-mono">{journey.event_id ?? "—"}</td>
                <td>{journey.outcome ?? "—"}</td>
                <td className="oil-mono">{stoppedAt(journey)}</td>
                <td className="oil-mono">{formatTime(journey.last_ts)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
    </div>
  );
}
