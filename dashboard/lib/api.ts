import type {
  ChatFeedbackRequest,
  ChatFeedbackResponse,
  ChatRequest,
  ChatResponse,
  Department,
  Incident,
  IncidentDetail,
  IncidentStatus,
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

/** Which sign-in methods the backend offers (GET /auth/config, unauthenticated). */
export interface AuthConfig {
  entra_enabled: boolean;
  password_login: boolean;
}

export async function fetchAuthConfig(): Promise<AuthConfig> {
  const res = await fetch(`${API_URL}/auth/config`, { credentials: "include" });
  if (!res.ok) throw new Error(`/auth/config failed: ${res.status}`);
  return res.json() as Promise<AuthConfig>;
}

/**
 * Where the Microsoft sign-in starts. Used as a full-page navigation target, NOT
 * with fetch: OAuth needs a real top-level navigation, and an XHR could neither
 * pass CORS nor render Microsoft's sign-in page.
 */
export function entraLoginUrl(): string {
  return `${API_URL}/auth/entra/login`;
}

export interface AlertsFilter {
  since?: string;
  // Multi-valued: sent as repeated params (?department=backend&department=devops)
  // and OR'd server-side into one `IN (...)`. An empty array or `undefined` both
  // mean "no filter" — matching the backend, where [] and None behave alike.
  department?: string[];
  app_name?: string[];
  severity?: string[];
  source?: string;
  level?: string;
  // Free-text substring match over message OR explanation, case-insensitive
  // server-side. Blank/whitespace-only is treated as absent by the backend, but
  // callers should omit it rather than send "" so the URL stays clean.
  search?: string;
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

/**
 * The filter half of the query string, shared by `/alerts` and `/alerts/facets`.
 *
 * One builder on purpose: the two endpoints take the same filter params (the
 * backend has a test pinning that), and the facet counts only describe the list
 * if both are asked the same question. Paging params are added by `fetchAlerts`
 * alone — facets aggregate rather than page.
 */
function alertFilterParams(filter: AlertsFilter): URLSearchParams {
  const params = new URLSearchParams();
  if (filter.since) params.set("since", filter.since);
  // append, not set: one entry per selected value, which is how FastAPI parses a
  // list query param. `set` would keep only the last and silently narrow the
  // filter to a single value. An empty array appends nothing = no filter.
  (filter.department ?? []).forEach((v) => params.append("department", v));
  (filter.app_name ?? []).forEach((v) => params.append("app_name", v));
  (filter.severity ?? []).forEach((v) => params.append("severity", v));
  if (filter.source) params.set("source", filter.source);
  if (filter.level) params.set("level", filter.level);
  if (filter.search) params.set("search", filter.search);
  if (filter.resolved !== undefined) params.set("resolved", String(filter.resolved));
  if (filter.cached !== undefined) params.set("cached", String(filter.cached));
  return params;
}

export function fetchAlerts(filter: AlertsFilter = {}): Promise<Page<ProcessedAlert>> {
  const params = alertFilterParams(filter);
  if (filter.limit !== undefined) params.set("limit", String(filter.limit));
  if (filter.cursor) params.set("cursor", filter.cursor);
  if (filter.sort) params.set("sort", filter.sort);
  const query = params.toString();
  return getJson<Page<ProcessedAlert>>(`/alerts${query ? `?${query}` : ""}`);
}

/**
 * Per-value alert counts for the three multi-select filters (backend
 * ``GET /alerts/facets``). A value with no matches is absent from the map, so
 * read a missing key as 0.
 */
export interface AlertFacets {
  severity: Record<string, number>;
  department: Record<string, number>;
  app_name: Record<string, number>;
}

/**
 * Contextual counts for the current filter selection.
 *
 * Pass the SAME filters as the list fetch, multi-select values included: the
 * backend applies the exclude-self rule per facet (each facet omits its own
 * filter), so it needs to see everything that is ticked. Withholding the
 * multi-selects here would silently produce unscoped counts.
 */
export function fetchFacets(filter: AlertsFilter = {}): Promise<AlertFacets> {
  const query = alertFilterParams(filter).toString();
  return getJson<AlertFacets>(`/alerts/facets${query ? `?${query}` : ""}`);
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

export interface IncidentsFilter {
  status?: IncidentStatus;
  // Multi-valued, same convention as AlertsFilter.department: sent as
  // repeated params (?department=backend&department=devops), OR'd server
  // side. [] and undefined both mean "no filter".
  department?: Department[];
  limit?: number;
  cursor?: string;
}

export function fetchIncidents(filter: IncidentsFilter = {}): Promise<Page<Incident>> {
  const params = new URLSearchParams();
  if (filter.status) params.set("status", filter.status);
  (filter.department ?? []).forEach((v) => params.append("department", v));
  if (filter.limit !== undefined) params.set("limit", String(filter.limit));
  if (filter.cursor) params.set("cursor", filter.cursor);
  const query = params.toString();
  return getJson<Page<Incident>>(`/incidents${query ? `?${query}` : ""}`);
}

export function fetchIncident(incidentId: string): Promise<IncidentDetail> {
  return getJson<IncidentDetail>(`/incidents/${encodeURIComponent(incidentId)}`);
}

/** Mark an incident resolved; returns the updated incident. */
export async function resolveIncident(incidentId: string): Promise<Incident> {
  const res = await fetch(`${API_URL}/incidents/${encodeURIComponent(incidentId)}/resolve`, {
    method: "PATCH",
    credentials: "include",
  });
  if (res.status === 401) throw new UnauthorizedError();
  if (!res.ok) throw new Error(`resolveIncident failed: ${res.status} ${res.statusText}`);
  return res.json() as Promise<Incident>;
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

/**
 * Rate an assistant answer (thumbs up/down).
 *
 * Fire-and-forget from the UI's point of view: the panel updates optimistically
 * and a failure here must not undo what the agent clicked or throw an error at
 * them — the vote is a nicety, not the work. Returns null on any failure so the
 * caller can decide whether to surface it.
 */
export async function sendChatFeedback(
  body: ChatFeedbackRequest
): Promise<ChatFeedbackResponse | null> {
  try {
    const res = await fetch(`${API_URL}/chat/feedback`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) return null;
    return (await res.json()) as ChatFeedbackResponse;
  } catch {
    return null;
  }
}
