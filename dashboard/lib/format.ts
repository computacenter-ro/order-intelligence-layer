import type { Journey, LogLevel } from "@/lib/types";

const LEVEL_LABEL: Record<LogLevel, string> = {
  DEBUG: "Debug",
  INFO: "Info",
  WARN: "Warning",
  ERROR: "Error",
};

export function levelLabel(level: LogLevel): string {
  return LEVEL_LABEL[level];
}

export function formatTime(iso: string): string {
  // Backend timestamps are UTC; render in the VIEWER's local timezone (the
  // browser's) rather than UTC, so a user in Europe/Bucharest sees local wall
  // time. Using toLocaleTimeString (not toISOString, which forces UTC) keeps
  // this correct for any viewer's timezone, not just one hardcoded offset.
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

// Display-only: capitalizes the first letter for rendering. Never apply this
// to a value used for matching/routing/lookup (e.g. Department, BadgeStatus,
// department-to-Teams-channel keys) - those must stay exactly as the backend
// contract defines them.
export function capitalize(value: string): string {
  return value.length === 0 ? value : value.charAt(0).toUpperCase() + value.slice(1);
}

export function stoppedAt(journey: Journey): string {
  const events = journey.events ?? [];
  const lastEvent = events[events.length - 1];
  return lastEvent ? lastEvent.raw.app_name : "—";
}
