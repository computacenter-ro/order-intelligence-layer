import type { Journey, ProcessedAlert } from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`);
  if (!res.ok) {
    throw new Error(`${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export interface AlertsFilter {
  since?: string;
  department?: string;
  source?: string;
}

export function fetchAlerts(filter: AlertsFilter = {}): Promise<ProcessedAlert[]> {
  const params = new URLSearchParams();
  if (filter.since) params.set("since", filter.since);
  if (filter.department) params.set("department", filter.department);
  if (filter.source) params.set("source", filter.source);
  const query = params.toString();
  return getJson<ProcessedAlert[]>(`/alerts${query ? `?${query}` : ""}`);
}

export function fetchJourneys(status?: string): Promise<Journey[]> {
  const query = status ? `?status=${encodeURIComponent(status)}` : "";
  return getJson<Journey[]>(`/journeys${query}`);
}

export function fetchJourney(journeyId: string): Promise<Journey> {
  return getJson<Journey>(`/journeys/${encodeURIComponent(journeyId)}`);
}
