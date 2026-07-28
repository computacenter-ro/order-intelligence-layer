"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Card } from "@computacenter-ro/style-guide/components";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import { CaretDownIcon, CaretRightIcon, WarningIcon } from "@phosphor-icons/react";
import { fetchIncident } from "@/lib/api";
import { groupAlertsByOrder } from "@/lib/incidents";
import { INCIDENT_STATUS_BADGE, INCIDENT_STATUS_LABEL } from "@/lib/incidentStatus";
import { Badge } from "@/components/ui/Badge";
import { IncidentActionsMenu } from "@/components/incidents/IncidentActionsMenu";
import { OrderGroupRow } from "@/components/incidents/OrderGroupRow";
import { capitalize } from "@/lib/format";
import type { Incident, IncidentDetail } from "@/lib/types";

interface IncidentCardProps {
  incident: Incident;
  onResolve: (incident: Incident) => void;
}

export function IncidentCard({ incident, onResolve }: IncidentCardProps) {
  const router = useRouter();
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [loading, setLoading] = useState(false);

  const isResolved = incident.status === "resolved";
  const iconColor = badgeColors[INCIDENT_STATUS_BADGE[incident.status]].border;

  const toggleExpanded = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      setExpanded((v) => !v);
      if (!detail && !loading) {
        setLoading(true);
        fetchIncident(incident.incident_id)
          .then(setDetail)
          .catch((err) => console.error("Failed to load incident detail:", err))
          .finally(() => setLoading(false));
      }
    },
    [detail, loading, incident.incident_id]
  );

  // The header count above is kept live by the parent's WS patch (incident.updated),
  // but that patch only touches the `incident` prop, not this already-fetched
  // per-order breakdown. Re-fetch it whenever the counts move so an incident
  // absorbing another journey while expanded doesn't strand a stale order list.
  useEffect(() => {
    if (expanded && detail) {
      fetchIncident(incident.incident_id)
        .then(setDetail)
        .catch((err) => console.error("Failed to refresh incident detail:", err));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [incident.journey_count, incident.alert_count]);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => router.push(`/incidents/${incident.incident_id}`)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") router.push(`/incidents/${incident.incident_id}`);
      }}
    >
      <Card style={{ marginBottom: "12px", cursor: "pointer" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
          <button
            type="button"
            aria-label={expanded ? "Collapse incident" : "Expand incident"}
            onClick={toggleExpanded}
            style={{
              display: "inline-flex",
              border: "none",
              background: "transparent",
              color: "var(--cc-heritage-blue)",
              cursor: "pointer",
              padding: 0,
            }}
          >
            {expanded ? <CaretDownIcon size={20} /> : <CaretRightIcon size={20} />}
          </button>
          <WarningIcon size={20} color={iconColor} />
          <span style={{ fontSize: "20px", fontWeight: 600, color: "var(--cc-foundation-blue)" }}>
            {incident.title}
          </span>
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: "8px" }}>
            <Badge status={INCIDENT_STATUS_BADGE[incident.status]}>
              {INCIDENT_STATUS_LABEL[incident.status]}
            </Badge>
            {incident.department && <Badge status="info">{capitalize(incident.department)}</Badge>}
            <span style={{ fontSize: "14px", color: "var(--cc-grey-three)" }}>
              {incident.journey_count} orders · {incident.alert_count} alerts
            </span>
            <span onClick={(e) => e.stopPropagation()}>
              <IncidentActionsMenu isResolved={isResolved} onResolve={() => onResolve(incident)} />
            </span>
          </div>
        </div>
        {expanded && (
          <div onClick={(e) => e.stopPropagation()} style={{ marginTop: "12px" }}>
            {loading && (
              <p style={{ fontSize: "14px", color: "var(--cc-grey-three)" }}>Loading orders…</p>
            )}
            {detail &&
              groupAlertsByOrder(detail.alerts).map((group) => (
                <OrderGroupRow
                  key={group.journeyId ?? group.outcome.alert_id}
                  group={group}
                  incidentId={incident.incident_id}
                />
              ))}
          </div>
        )}
      </Card>
    </div>
  );
}
