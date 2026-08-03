import type { BadgeStatus, IncidentStatus } from "@/lib/types";

export const INCIDENT_STATUS_BADGE: Record<IncidentStatus, BadgeStatus> = {
  open: "error",
  resolved: "success",
};

export const INCIDENT_STATUS_LABEL: Record<IncidentStatus, string> = {
  open: "Open",
  resolved: "Resolved",
};
