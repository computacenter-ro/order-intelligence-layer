"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@computacenter-ro/style-guide/components";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { fetchIncidents, resolveIncident } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
import { IncidentCard } from "@/components/incidents/IncidentCard";
import type { Incident, IncidentStatus } from "@/lib/types";

type StatusFilter = IncidentStatus | "all";

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All Statuses" },
];

const selectStyle: React.CSSProperties = {
  height: "32px",
  minWidth: "150px",
  padding: `0 ${semanticSpacing.md}`,
  fontSize: "14px",
  fontFamily: "inherit",
  color: "var(--cc-grey-one)",
  backgroundColor: "var(--cc-cloud-white)",
  border: "1px solid var(--cc-grey-four)",
  borderRadius: radii.md,
  cursor: "pointer",
};

export default function IncidentsPage() {
  const [status, setStatus] = useState<StatusFilter>("open");

  // The server sorts last_ts DESC and applies the status filter; "all" omits it.
  const fetchPage = useCallback(
    (cursor: string | null) =>
      fetchIncidents({ status: status === "all" ? undefined : status, cursor: cursor ?? undefined }),
    [status]
  );

  const { items, loading, hasMore, loadMore, reload, remove } = usePagination<Incident>(
    fetchPage,
    (i) => i.incident_id
  );

  // Reload on status change, but skip the mount run — the hook already loads
  // once at mount (same pattern as app/journeys/page.tsx).
  const didMount = useRef(false);
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true;
      return;
    }
    reload();
  }, [status, reload]);

  const handleResolve = useCallback(
    (incident: Incident) => {
      resolveIncident(incident.incident_id)
        .then(() => {
          // Under a specific-status filter, a resolved incident no longer
          // matches ("open" -> gone; "resolved" can't happen here since a
          // resolve action is never shown on an already-resolved card) — drop
          // it from view immediately, same as a resolved alert leaving the Feed.
          // Under "all", the row should stay but its badge must flip, and the
          // hook has no "patch one item" operation, so re-fetch the page.
          if (status !== "all") {
            remove(incident.incident_id);
          } else {
            reload();
          }
        })
        .catch((err) => console.error("Failed to resolve incident:", err));
    },
    [remove, reload, status]
  );

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        Incidents
      </h1>
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "4px", marginBottom: "24px" }}>
        Related alerts across one or many orders, collapsed into a single incident
      </p>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          marginBottom: "24px",
          maxWidth: "220px",
        }}
      >
        <label
          htmlFor="incident-status-filter"
          style={{
            fontSize: "14px",
            fontWeight: 500,
            color: "var(--cc-grey-one)",
            marginBottom: semanticSpacing.xs,
          }}
        >
          Status
        </label>
        <select
          id="incident-status-filter"
          style={selectStyle}
          value={status}
          onChange={(e) => setStatus(e.target.value as StatusFilter)}
        >
          {STATUS_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>
      {items.length === 0 && !loading && (
        <p style={{ color: "var(--cc-grey-three)" }}>No incidents match this filter</p>
      )}
      {items.map((incident) => (
        <IncidentCard key={incident.incident_id} incident={incident} onResolve={handleResolve} />
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
