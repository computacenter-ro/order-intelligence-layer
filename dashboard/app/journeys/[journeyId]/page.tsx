"use client";

import { useParams, useRouter } from "next/navigation";
import { Button } from "@computacenter-ro/style-guide/components";
import { journeys } from "@/lib/fixtures";
import { OutcomeBanner } from "@/components/journeys/OutcomeBanner";
import { AiSummaryPanel } from "@/components/journeys/AiSummaryPanel";
import { PipelineTrail } from "@/components/journeys/PipelineTrail";
import { JourneyTimeline } from "@/components/journeys/JourneyTimeline";

export default function JourneyDetailPage() {
  const params = useParams<{ journeyId: string }>();
  const router = useRouter();
  const journey = journeys.find((j) => j.journey_id === params.journeyId);

  if (!journey) {
    return (
      <div>
        <div style={{ marginBottom: "16px" }}>
          <Button variant="ghost" onClick={() => router.push("/journeys")}>
            ← Back to Journeys
          </Button>
        </div>
        <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
          Journey not found
        </h1>
        <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "8px" }}>
          No journey matches this id — it may not exist, or the id is wrong.
        </p>
      </div>
    );
  }

  return (
    <div>
      <div style={{ marginBottom: "16px" }}>
        <Button variant="ghost" onClick={() => router.push("/journeys")}>
          ← Back to Journeys
        </Button>
      </div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: "0 0 16px" }}>
        Journey {journey.order_id ?? journey.event_id}
      </h1>
      <OutcomeBanner journey={journey} />
      <AiSummaryPanel summary={journey.summary} />
      <h2 style={{ fontSize: "16px", fontWeight: 500, color: "var(--cc-grey-two)", margin: "0 0 8px" }}>
        Pipeline path
      </h2>
      <div style={{ marginBottom: "24px" }}>
        <PipelineTrail journey={journey} />
      </div>
      <h2 style={{ fontSize: "16px", fontWeight: 500, color: "var(--cc-grey-two)", margin: "0 0 12px" }}>
        Event timeline
      </h2>
      <JourneyTimeline journey={journey} />
    </div>
  );
}
