import type { Journey, ProcessedAlert } from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** Thrown when the backend rejects a request for lack of a valid session. */
export class UnauthorizedError extends Error {
  constructor() {
    super("unauthorized");
    this.name = "UnauthorizedError";
  }
}

// `credentials: "include"` makes the browser send/receive the httpOnly session
// cookie cross-origin (:3000 -> :8000). Required for every authed call.
async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { credentials: "include" });
  if (res.status === 401) {
    throw new UnauthorizedError();
  }
  if (!res.ok) {
    throw new Error(`${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export interface CurrentUser {
  username: string;
}

/** Resolve the logged-in user, or null when there is no valid session (401). */
export async function fetchMe(): Promise<CurrentUser | null> {
  const res = await fetch(`${API_URL}/auth/me`, { credentials: "include" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`/auth/me failed: ${res.status}`);
  return res.json() as Promise<CurrentUser>;
}

/** Log in with username/password; sets the httpOnly cookie on success. */
export async function login(username: string, password: string): Promise<CurrentUser> {
  const res = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`login failed: ${res.status}`);
  return res.json() as Promise<CurrentUser>;
}

/** Clear the session cookie. */
export async function logout(): Promise<void> {
  await fetch(`${API_URL}/auth/logout`, { method: "POST", credentials: "include" });
}

export interface AlertsFilter {
  since?: string;
  department?: string;
  source?: string;
}

/** Mark an alert resolved; returns the updated alert. */
export async function resolveAlert(alertId: string): Promise<ProcessedAlert> {
  const res = await fetch(`${API_URL}/alerts/${encodeURIComponent(alertId)}/resolve`, {
    method: "PATCH",
    credentials: "include",
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`resolveAlert failed: ${res.status} ${res.statusText}`);
  return res.json() as Promise<ProcessedAlert>;
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
