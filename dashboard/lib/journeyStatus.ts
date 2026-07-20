import type { BadgeStatus, JourneyStatus } from "@/lib/types";

export const JOURNEY_STATUS_BADGE: Record<JourneyStatus, BadgeStatus> = {
  success: "success",
  failed: "error",
  timed_out: "pending",
  in_progress: "info",
};

export const JOURNEY_STATUS_LABEL: Record<JourneyStatus, string> = {
  success: "Success",
  failed: "Failed",
  timed_out: "Timed out",
  in_progress: "In progress",
};
