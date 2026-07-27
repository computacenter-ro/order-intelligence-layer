import type {
  ChatRequest,
  ChatResponse,
  Journey,
  OverviewStats,
  ProcessedAlert,
} from "@/lib/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * One page of a cursor-paginated listing (backend/schemas.py ``Page``).
 * ``next_cursor`` is the opaque token to pass back as ``?cursor=`` for the next
 * page; ``null`` means this was the last page.
 */
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

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
  level?: string;
  app_name?: string;
  severity?: string;
  resolved?: boolean;
  // Semantic-cache provenance; orthogonal to `source` (cached alerts are all
  // source="ai"). Omitted = both.
  cached?: boolean;
  // Cursor pagination (backend/pagination.py). `cursor` from a prior page's
  // next_cursor; `sort` picks the keyset column (feed=emitted_at,
  // history=resolved_at).
  limit?: number;
  cursor?: string;
  sort?: "emitted_at" | "resolved_at";
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

export function fetchAlerts(filter: AlertsFilter = {}): Promise<Page<ProcessedAlert>> {
  const params = new URLSearchParams();
  if (filter.since) params.set("since", filter.since);
  if (filter.department) params.set("department", filter.department);
  if (filter.source) params.set("source", filter.source);
  if (filter.level) params.set("level", filter.level);
  if (filter.app_name) params.set("app_name", filter.app_name);
  if (filter.severity) params.set("severity", filter.severity);
  if (filter.resolved !== undefined) params.set("resolved", String(filter.resolved));
  if (filter.cached !== undefined) params.set("cached", String(filter.cached));
  if (filter.limit !== undefined) params.set("limit", String(filter.limit));
  if (filter.cursor) params.set("cursor", filter.cursor);
  if (filter.sort) params.set("sort", filter.sort);
  const query = params.toString();
  return getJson<Page<ProcessedAlert>>(`/alerts${query ? `?${query}` : ""}`);
}

export interface JourneysFilter {
  status?: string;
  limit?: number;
  cursor?: string;
}

export function fetchJourneys(filter: JourneysFilter = {}): Promise<Page<Journey>> {
  const params = new URLSearchParams();
  if (filter.status) params.set("status", filter.status);
  if (filter.limit !== undefined) params.set("limit", String(filter.limit));
  if (filter.cursor) params.set("cursor", filter.cursor);
  const query = params.toString();
  return getJson<Page<Journey>>(`/journeys${query ? `?${query}` : ""}`);
}

export function fetchJourney(journeyId: string): Promise<Journey> {
  return getJson<Journey>(`/journeys/${encodeURIComponent(journeyId)}`);
}

/** Aggregate counters for the Insights page (backend GET /stats/insights). */
export function fetchStats(): Promise<OverviewStats> {
  return getJson<OverviewStats>("/stats/insights");
}

// --- chat --------------------------------------------------------------------

/**
 * POST a question to the backend's authenticated chat proxy.
 *
 * `getJson` is GET-only, so this mirrors its contract for a body-carrying call:
 * same `credentials: "include"` (the httpOnly session cookie must ride along
 * cross-origin :3000 -> :8000) and the same `UnauthorizedError` on 401, so the
 * caller handles an expired session exactly as every other API call does.
 *
 * Non-streaming by design for now: one request, one answer. The backend already
 * degrades internally (an LLM outage returns `mode: "retrieval-only"` rather
 * than an error), so a rejected promise here means a transport/auth failure —
 * not "the assistant had nothing to say".
 */
export async function sendChat(body: ChatRequest): Promise<ChatResponse> {
  const res = await fetch(`${API_URL}/chat`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`/chat failed: ${res.status} ${res.statusText}`);
  return res.json() as Promise<ChatResponse>;
}
