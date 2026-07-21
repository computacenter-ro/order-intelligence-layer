"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Button } from "@computacenter-ro/style-guide/components";
import { fetchAlerts, fetchJourney } from "@/lib/api";
import { useWebSocket } from "@/lib/useWebSocket";
import { OutcomeBanner } from "@/components/journeys/OutcomeBanner";
import { AiSummaryPanel } from "@/components/journeys/AiSummaryPanel";
import { PipelineTrail } from "@/components/journeys/PipelineTrail";
import { JourneyTimeline } from "@/components/journeys/JourneyTimeline";
import type { Journey, ProcessedAlert, WsEvent } from "@/lib/types";

export default function JourneyDetailPage() {
  const params = useParams<{ journeyId: string }>();
  const router = useRouter();
  const [journey, setJourney] = useState<Journey | null>(null);
  const [journeyAlerts, setJourneyAlerts] = useState<ProcessedAlert[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Reset loading when journeyId changes (e.g. navigating between journeys) —
    // synchronizing with the params-driven fetch below, not derivable from render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    fetchJourney(params.journeyId)
      .then(setJourney)
      .catch(() => setJourney(null))
      .finally(() => setLoading(false));
    fetchAlerts()
      .then((all) => setJourneyAlerts(all.filter((a) => a.journey_id === params.journeyId)))
      .catch((err) => console.error("Failed to load journey alerts:", err));
  }, [params.journeyId]);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.data.journey_id !== params.journeyId) return;
      if (event.type === "journey.completed") {
        setJourney(event.data);
      } else if (event.type === "journey.updated") {
        fetchJourney(params.journeyId).then(setJourney).catch(() => {});
      } else if (event.type === "alert.new") {
        setJourneyAlerts((prev) =>
          prev.some((a) => a.alert_id === event.data.alert_id) ? prev : [...prev, event.data]
        );
      }
    },
    [params.journeyId]
  );

  useWebSocket(handleEvent);

  if (loading) {
    return <p style={{ fontSize: "16px", color: "var(--cc-grey-three)" }}>Loading journey…</p>;
  }

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
      <JourneyTimeline journey={journey} alerts={journeyAlerts} />
    </div>
  );
}
