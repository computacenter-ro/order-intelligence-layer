"use client";

import { useCallback, useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Button } from "@computacenter-ro/style-guide/components";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { ChatCircleDotsIcon, WarningIcon } from "@phosphor-icons/react";
import { fetchIncident, resolveIncident } from "@/lib/api";
import { useChat } from "@/lib/chat";
import { groupAlertsByOrder } from "@/lib/incidents";
import { INCIDENT_STATUS_BADGE, INCIDENT_STATUS_LABEL } from "@/lib/incidentStatus";
import { useWebSocket } from "@/lib/useWebSocket";
import { Badge } from "@/components/ui/Badge";
import { OrderGroupRow } from "@/components/incidents/OrderGroupRow";
import { formatTime, capitalize } from "@/lib/format";
import type { IncidentDetail, WsEvent } from "@/lib/types";

export default function IncidentDetailPage() {
  const params = useParams<{ incidentId: string }>();
  const router = useRouter();
  const { openChat } = useChat();
  const [incident, setIncident] = useState<IncidentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [resolving, setResolving] = useState(false);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    fetchIncident(params.incidentId)
      .then(setIncident)
      .catch(() => setIncident(null))
      .finally(() => setLoading(false));
  }, [params.incidentId]);

  const handleResolve = useCallback(() => {
    if (!incident) return;
    setResolving(true);
    resolveIncident(incident.incident_id)
      .then((updated) => setIncident((prev) => (prev ? { ...prev, status: updated.status } : prev)))
      .catch((err) => console.error("Failed to resolve incident:", err))
      .finally(() => setResolving(false));
  }, [incident]);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type !== "incident.updated") return;
      if (event.data.incident_id !== params.incidentId) return;
      // Counts/last_ts changed AND a new order's alerts joined — re-fetch full
      // detail rather than patching just the header fields (mirrors the
      // journey detail page's journey.updated handling: a re-fetch is what
      // grows the list, the pushed payload itself is header-only).
      fetchIncident(params.incidentId).then(setIncident).catch(() => {});
    },
    [params.incidentId]
  );

  useWebSocket(handleEvent);

  if (loading) {
    return <p style={{ fontSize: "16px", color: "var(--cc-grey-three)" }}>Loading incident…</p>;
  }

  if (!incident) {
    return (
      <div>
        <div style={{ marginBottom: "16px" }}>
          <Button variant="ghost" onClick={() => router.push("/incidents")}>
            ← Back to Incidents
          </Button>
        </div>
        <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
          Incident not found
        </h1>
        <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "8px" }}>
          No incident matches this id — it may not exist, or the id is wrong.
        </p>
      </div>
    );
  }

  const tone = badgeColors[INCIDENT_STATUS_BADGE[incident.status]];
  const isResolved = incident.status === "resolved";
  const groups = groupAlertsByOrder(incident.alerts);
  const facts: { label: string; value: string | null }[] = [
    { label: "Failure subtype", value: incident.failure_subtype },
    { label: "Failing service", value: incident.failing_service },
    { label: "Error", value: incident.error_token },
  ];

  return (
    <div>
      <div style={{ marginBottom: "16px" }}>
        <Button variant="ghost" onClick={() => router.push("/incidents")}>
          ← Back to Incidents
        </Button>
      </div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: "0 0 16px" }}>
        {incident.title}
      </h1>
      <div
        style={{
          background: tone.bg,
          border: `1px solid ${tone.border}`,
          borderRadius: "8px",
          padding: "16px 24px",
          marginBottom: "24px",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          <WarningIcon size={20} color={tone.text} />
          <span style={{ fontSize: "20px", fontWeight: 600, color: tone.text }}>
            {INCIDENT_STATUS_LABEL[incident.status]}
          </span>
          {incident.department && <Badge status="info">{capitalize(incident.department)}</Badge>}
          <span style={{ marginLeft: "auto", display: "flex", gap: "8px" }}>
            {/* Secondary, not primary: "Resolve" is this page's single primary
                action (guidelines allow at most one), and the assistant is a
                supporting action. Same treatment as AlertDetailDrawer's
                "Ask About This". */}
            <Button
              variant="secondary"
              size="compact"
              leftIcon={<ChatCircleDotsIcon size={20} />}
              onClick={() =>
                openChat(
                  { kind: "incident", id: incident.incident_id },
                  `incident ${incident.title}`
                )
              }
            >
              Ask About This Incident
            </Button>
            <Button
              variant="primary"
              size="compact"
              onClick={handleResolve}
              disabled={isResolved || resolving}
              loading={resolving}
            >
              {isResolved ? "Resolved" : "Resolve"}
            </Button>
          </span>
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "16px",
            fontSize: "13px",
            color: "var(--cc-grey-two)",
            marginTop: "12px",
          }}
        >
          {facts.map(({ label, value }) => (
            <span key={label}>
              {label}: <span style={{ fontFamily: "ui-monospace, Menlo, monospace" }}>{value ?? "—"}</span>
            </span>
          ))}
          <span>
            {incident.journey_count} orders · {incident.alert_count} alerts
          </span>
          <span>First seen: {formatTime(incident.first_ts)}</span>
          <span>Last seen: {formatTime(incident.last_ts)}</span>
        </div>
      </div>
      <h2 style={{ fontSize: "16px", fontWeight: 500, color: "var(--cc-grey-two)", margin: "0 0 12px" }}>
        Affected orders
      </h2>
      {groups.map((group) => (
        <OrderGroupRow
          key={group.journeyId ?? group.outcome.alert_id}
          group={group}
          defaultExpanded
          incidentId={incident.incident_id}
        />
      ))}
    </div>
  );
}
