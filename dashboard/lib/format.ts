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

/**
 * Display-only: turn a backend bucket key into a readable chart label —
 * "INBOUND_TRANSFORM_FAILED" -> "Inbound transform failed", "unassigned" ->
 * "Unassigned". Never use the result for matching or lookup; the raw key stays
 * the identity (charts keep it for the tooltip).
 */
export function humanizeKey(key: string): string {
  return capitalize(key.replace(/_/g, " ").toLowerCase());
}

/**
 * Render a duration in seconds compactly: "820ms" / "12.3s" / "1m 20s".
 *
 * `null` (no finished journey had both timestamps) renders as an em dash rather
 * than "0s" — those are different facts and must not look the same.
 */
export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  // Round the remainder, but never to a bare "60s" — carry it into the minutes.
  const rest = Math.round(seconds - minutes * 60);
  if (rest === 60) return `${minutes + 1}m`;
  return rest === 0 ? `${minutes}m` : `${minutes}m ${rest}s`;
}

export function stoppedAt(journey: Journey): string {
  const events = journey.events ?? [];
  const lastEvent = events[events.length - 1];
  return lastEvent ? lastEvent.raw.app_name : "—";
}
