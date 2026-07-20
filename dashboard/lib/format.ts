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
  return new Date(iso).toISOString().slice(11, 19);
}

export function stoppedAt(journey: Journey): string {
  const events = journey.events ?? [];
  const lastEvent = events[events.length - 1];
  return lastEvent ? lastEvent.raw.app_name : "—";
}
