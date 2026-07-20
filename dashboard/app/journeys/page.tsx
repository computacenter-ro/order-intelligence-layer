"use client";

import { useRouter } from "next/navigation";
import { Card } from "@computacenter-ro/style-guide/components";
import { Badge } from "@/components/ui/Badge";
import { formatTime, stoppedAt } from "@/lib/format";
import { JOURNEY_STATUS_BADGE, JOURNEY_STATUS_LABEL } from "@/lib/journeyStatus";
import { journeys } from "@/lib/fixtures";

const COLUMN_HEADINGS = ["Status", "Order ID", "Event ID", "Outcome", "Stopped At", "Last Seen"];

export default function JourneysPage() {
  const router = useRouter();
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
              <tr
                key={journey.journey_id}
                role="link"
                tabIndex={0}
                onClick={() => router.push(`/journeys/${journey.journey_id}`)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") router.push(`/journeys/${journey.journey_id}`);
                }}
              >
                <td>
                  <Badge status={JOURNEY_STATUS_BADGE[journey.status]}>
                    {JOURNEY_STATUS_LABEL[journey.status]}
                  </Badge>
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
