import type { BadgeStatus, JourneyStatus } from "@/lib/types";

export const JOURNEY_STATUS_BADGE: Record<JourneyStatus, BadgeStatus> = {
  SUCCESS: "success",
  FAILED: "error",
  TIMED_OUT: "pending",
  IN_PROGRESS: "info",
};

export const JOURNEY_STATUS_LABEL: Record<JourneyStatus, string> = {
  SUCCESS: "Success",
  FAILED: "Failed",
  TIMED_OUT: "Timed out",
  IN_PROGRESS: "In progress",
};
