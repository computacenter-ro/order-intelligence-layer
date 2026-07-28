"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@computacenter-ro/style-guide/components";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { fetchIncidents, resolveIncident } from "@/lib/api";
import { usePagination } from "@/lib/usePagination";
import { useWebSocket } from "@/lib/useWebSocket";
import { IncidentCard } from "@/components/incidents/IncidentCard";
import { NewIncidentsBanner } from "@/components/incidents/NewIncidentsBanner";
import { MultiFilterDropdown } from "@/components/alerts/MultiFilterDropdown";
import type { FilterOption } from "@/components/alerts/FilterDropdown";
import { capitalize } from "@/lib/format";
import type { Department, Incident, IncidentStatus, WsEvent } from "@/lib/types";

type StatusFilter = IncidentStatus | "all";

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "open", label: "Open" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All Statuses" },
];

// Same 5 values as the Alert Feed's department filter (AlertFilterBar.tsx) —
// kept as its own local list rather than importing from there, since that
// constant isn't exported and duplicating five literals is cheaper than
// exporting it purely to share.
const DEPARTMENTS: Department[] = ["networking", "devops", "backend", "database", "general"];
const DEPARTMENT_OPTIONS: FilterOption[] = DEPARTMENTS.map((d) => ({
  value: d,
  label: capitalize(d),
}));

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
  const [department, setDepartment] = useState<Department[]>([]);
  const [pending, setPending] = useState<Incident[]>([]);

  // The server sorts last_ts DESC and applies both filters; "all" status and
  // an empty department list both mean "no filter" for their dimension.
  const fetchPage = useCallback(
    (cursor: string | null) =>
      fetchIncidents({
        status: status === "all" ? undefined : status,
        department,
        cursor: cursor ?? undefined,
      }),
    [status, department]
  );

  const { items, loading, hasMore, loadMore, reload, prepend, remove, patch } = usePagination<Incident>(
    fetchPage,
    (i) => i.incident_id
  );

  // Reload on filter change, but skip the mount run — the hook already loads
  // once at mount (same pattern as app/journeys/page.tsx).
  const didMount = useRef(false);
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true;
      return;
    }
    reload();
  }, [status, department, reload]);

  const handleStatusChange = useCallback((next: StatusFilter) => {
    setStatus(next);
    // Drop live incidents captured under the previous filter, same as the
    // Alert Feed does on a filter change.
    setPending([]);
  }, []);

  const handleDepartmentChange = useCallback((next: Department[]) => {
    setDepartment(next);
    setPending([]);
  }, []);

  const handleEvent = useCallback(
    (event: WsEvent) => {
      if (event.type === "incident.updated") {
        // An already-open incident absorbed another journey — its counts /
        // last_ts changed in place, so patch the row directly rather than
        // treating it as a new arrival. No-op if it isn't currently loaded
        // (e.g. it's on a page not yet fetched via "Load more").
        patch(event.data.incident_id, event.data);
        return;
      }
      if (event.type !== "incident.new") return;
      // A freshly created incident is always "open" — it only belongs in the
      // live feed under the "open"/"all" filters, never under "resolved".
      if (status === "resolved") return;
      // Same IN(...) convention as the backend: a non-empty department
      // selection excludes an incident with no department (or a department
      // not in the ticked set) — an empty selection means "no filter".
      if (department.length > 0 && (!event.data.department || !department.includes(event.data.department))) {
        return;
      }
      setPending((prev) =>
        prev.some((i) => i.incident_id === event.data.incident_id) ? prev : [event.data, ...prev]
      );
    },
    [status, department, patch]
  );

  useWebSocket(handleEvent);

  const handleReveal = useCallback(() => {
    // pending is newest-first; prepend inserts at the top, so replay
    // oldest-first to leave the newest incident on top. The hook dedups.
    [...pending].reverse().forEach(prepend);
    setPending([]);
  }, [pending, prepend]);

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
          alignItems: "flex-end",
          gap: semanticSpacing.base,
          marginBottom: "24px",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", maxWidth: "220px" }}>
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
            onChange={(e) => handleStatusChange(e.target.value as StatusFilter)}
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <MultiFilterDropdown
          label="Department"
          selected={department}
          options={DEPARTMENT_OPTIONS}
          onChange={(next) => handleDepartmentChange(next as Department[])}
        />
      </div>
      <NewIncidentsBanner count={pending.length} onReveal={handleReveal} />
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
