export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

export interface LogLine {
  log_id: string;
  timestamp: string;
  app_name: string;
  level: LogLevel;
  logger: string;
  host: string;
  process_id: string;
  thread: string;
  eventId?: string;
  orderId?: string;
  cartHeaderId?: string;
  accountNumber?: string;
  message: string;
}

export type Department = "networking" | "devops" | "backend" | "database" | "general";

export type Severity = "critical" | "high" | "medium" | "low";

// Mirrors backend/schemas.py AlertOut exactly — flat, snake_case ids, no
// host/process_id/thread/timestamp (the alerts table never stores them).
export interface ProcessedAlert {
  alert_id: string;
  emitted_at: string;
  log_id: string;
  level: LogLevel;
  app_name: string;
  logger: string;
  message: string;
  event_id: string | null;
  order_id: string | null;
  cart_header_id: string | null;
  account_number: string | null;
  explanation: string | null;
  department: Department | null;
  severity: Severity | null;
  source: "ai" | "fallback";
  // Semantic-cache hit: the AI answer was reused rather than recomputed. A
  // modifier on source="ai", not an alternative — cached alerts are still
  // AI-analyzed, so both flags are shown together.
  cached: boolean;
  journey_id: string | null;
  is_resolved: boolean;
  resolved_at: string | null;
}

export type JourneyStatus = "IN_PROGRESS" | "SUCCESS" | "FAILED" | "TIMED_OUT";

export interface JourneyEvent {
  log_id: string;
  ts: string;
  raw: LogLine;
}

export interface Journey {
  journey_id: string;
  status: JourneyStatus;
  outcome: string | null;
  first_ts: string;
  last_ts: string;
  event_id: string | null;
  order_id: string | null;
  cart_header_id: string | null;
  summary: string | null;
  // Only present on GET /journeys/{id} and the journey.completed WS event
  // (backend's JourneyDetailOut) — journey.updated carries a header-only
  // JourneyOut with no events, so a fresh fetch is needed to grow the list.
  events?: JourneyEvent[];
}

export type WsEvent =
  | { type: "alert.new"; data: ProcessedAlert }
  | { type: "journey.updated"; data: Journey }
  | { type: "journey.completed"; data: Journey }
  | { type: "incident.new"; data: Incident }
  | { type: "incident.updated"; data: Incident };

export type BadgeStatus =
  | "error"
  | "warning"
  | "pending"
  | "success"
  | "info"
  | "inactive"
  | "other"
  | "primary";

export type IncidentStatus = "open" | "resolved";

// Mirrors backend/schemas.py IncidentOut exactly.
export interface Incident {
  incident_id: string;
  signature: string | null;
  failure_subtype: string | null;
  failing_service: string | null;
  error_token: string | null;
  title: string;
  department: Department | null;
  status: IncidentStatus;
  first_ts: string;
  last_ts: string;
  primary_alert_id: string | null;
  alert_count: number;
  journey_count: number;
}

// Mirrors backend/schemas.py IncidentDetailOut — adds the full linked alert list.
export interface IncidentDetail extends Incident {
  alerts: ProcessedAlert[];
}

// --- insights aggregation (GET /stats/insights) ------------------------------
//
// Mirrors backend/schemas.py OverviewStats. The breakdowns are open-ended
// Records rather than keyed unions on purpose: the backend adds an explicit
// bucket for nullable columns ("unassigned" department, "unrated" severity,
// "none" outcome) so each map sums back to its total, and a new
// department/severity/outcome must not break the type.

export interface JourneyStats {
  total: number;
  by_status: Record<string, number>;
  by_outcome: Record<string, number>;
  // Over finished journeys only (SUCCESS+FAILED+TIMED_OUT); 0 when none have finished.
  success_rate: number;
  // null when no finished journey has both timestamps.
  avg_duration_seconds: number | null;
}

export interface AlertStats {
  total: number;
  open: number;
  resolved: number;
  // Semantic-cache provenance. cached + fresh == total. `cached` is a modifier on
  // source="ai" (never on a fallback), so it is a SUBSET of by_source.ai — not a
  // peer of it.
  cached: number;
  fresh: number;
  by_department: Record<string, number>;
  by_severity: Record<string, number>;
  by_level: Record<string, number>;
  by_source: Record<string, number>;
}

// Mirrors backend/schemas.py IncidentStats. alerts_clustered + alerts_unclustered
// == alerts.total: only FAILED/TIMED_OUT journeys are clustered, so alerts from
// successful orders have no incident and must stay out of the ratio.
export interface IncidentStats {
  total: number;
  by_status: Record<string, number>;
  alerts_clustered: number;
  alerts_unclustered: number;
  // null when no incident exists — "nothing to compress", not "no compression".
  alerts_per_incident: number | null;
}

export interface OverviewStats {
  journeys: JourneyStats;
  alerts: AlertStats;
  incidents: IncidentStats;
}

// --- chat (POST /chat) -------------------------------------------------------
//
// Mirrors backend/schemas.py ChatRequest/ChatResponse. The backend is an
// authenticated proxy in front of the AI service, which does the retrieval and
// (when its LLM is up) the grounded composition.

/** Scope a question to one record — the "Ask about this" buttons. */
export interface ChatContext {
  kind: "alert" | "journey" | "incident";
  id: string;
}

export interface ChatRequest {
  query: string;
  k?: number;
  filters?: Record<string, string> | null;
  context?: ChatContext | null;
  /**
   * IANA zone (e.g. "Europe/Bucharest") from the browser — the only party that
   * knows where the reader is. The backend renders the SCOPED context's
   * timestamps in it, so the model quotes a local time directly. Indexed records
   * stay UTC (shared by all viewers) and are rewritten on display instead.
   */
  tz?: string;
}

/** One cited incident record. `link` is null when DASHBOARD_URL is unset. */
export interface ChatSource {
  id: string;
  kind: string;
  score: number;
  snippet: string;
  link: string | null;
}

/**
 * How much of the history the answer drew on — computed by the AI service, not
 * written by the LLM. `truncated` means retrieval hit its limit, so other
 * matching incidents likely exist beyond the ones cited.
 */
export interface ChatCoverage {
  shown: number;
  limit: number;
  truncated: boolean;
}

/**
 * `mode` mirrors the alert feed's AI/fallback distinction: "ai" = the LLM
 * composed the answer from the sources; "retrieval-only" = the LLM was
 * unavailable and the answer is a deterministic template over the same sources.
 * Sources are identical either way.
 */
export type ChatMode = "ai" | "retrieval-only";

export interface ChatResponse {
  answer: string;
  sources: ChatSource[];
  mode: ChatMode;
  coverage: ChatCoverage;
  /** Identifies THIS answer so it can be rated (POST /chat/feedback). */
  answer_id: string;
}

/**
 * A thumbs up/down on one answer.
 *
 * Rates the ANSWER, not any single source: the backend attributes credit to the
 * cited records by rank (see backend/feedback.py), so `record_ids` must be sent
 * IN THE ORDER SHOWN. Re-voting the same `answer_id` replaces the prior vote.
 */
export interface ChatFeedbackRequest {
  answer_id: string;
  liked: boolean;
  query?: string;
  record_ids?: string[];
  answer_mode?: ChatMode;
  scoped_kind?: string | null;
  scoped_id?: string | null;
}

export interface ChatFeedbackResponse {
  recorded: boolean;
  liked: boolean;
}

// --- LLM observability (GET /llm-stats) --------------------------------------
//
// Mirrors what backend/api.py's /llm-stats route serves. That route FORWARDS the
// AI service's own /llm-stats (ai_service/langsmith_stats.py) and degrades rather
// than failing, so the nullability below is load-bearing — see CacheSavings.

/** Per-logical-model run stats over the requested window, from LangSmith. */
export interface LlmNodeStats {
  run_count: number;
  latency_p50_s: number;
  latency_p99_s: number;
  error_rate: number;
  total_cost_usd: number;
  total_tokens: number;
}

/**
 * Semantic-cache counters plus the LLM spend they avoided.
 *
 * ALL FOUR are nullable, which is wider than it looks. Two different degraded
 * bodies reach this type:
 *
 * - LangSmith unconfigured (`ai_service/langsmith_stats.py::cache_savings`) —
 *   real hit/miss counters from Redis, but `estimated_saved_usd: null`, because
 *   there is no per-run cost to multiply by.
 * - AI service unreachable (`backend/llm_stats_client.py::degraded`) — *every*
 *   field null, because the backend never saw the cache and a 0 would be a
 *   fabricated measurement.
 *
 * So `hits` etc. cannot be typed as plain `number`: rendering has to handle the
 * second case, and `hits.toLocaleString()` on a null is a runtime crash on the
 * exact path this endpoint was built to survive. null means "unknown", never 0.
 */
export interface CacheSavings {
  hits: number | null;
  misses: number | null;
  hit_rate: number | null;
  estimated_saved_usd: number | null;
}

/**
 * The `/llm-stats` payload. `window` is echoed from the request, so it always
 * matches what was asked for even when nothing could be read.
 *
 * A node is `null` when its stats could not be read (no creds, API error,
 * timeout, or the AI service being down) — distinct from a node with
 * `run_count: 0`, which means "queried fine, no runs in this window".
 *
 * `fetched_at` and `langsmith_configured` are what let the UI say WHICH of those
 * a null node is. The AI service refreshes these numbers on a timer rather than
 * on request, so "no cycle has finished yet" is a real, routine state — it is
 * what the page shows for the first few seconds after a restart, and rendering it
 * as "not configured" (the only option before these fields existed) told the
 * reader to go edit an env var that was already correct.
 */
export interface LlmStats {
  window: string;
  /** ISO-8601 UTC when the AI service's last refresh cycle finished; `null` if
   *  none has (cold start), or if the AI service itself was unreachable.
   *  Stamped at the END of a cycle, so it never promises data still being gathered. */
  fetched_at: string | null;
  /** The AI service's configured refresh period, in seconds. Always positive — the
   *  backend substitutes a default rather than forwarding a zero, because the UI
   *  both adds this to `fetched_at` (next update due) and multiplies it (staleness
   *  threshold). */
  refresh_interval_s: number;
  /** Whether the AI service has LangSmith credentials. `false` also covers "the
   *  backend could not reach the AI service to ask", so treat it as "no data to
   *  be had", not strictly as "the key is missing". */
  langsmith_configured: boolean;
  nodes: Record<"explainer" | "router" | "summary" | "chat", LlmNodeStats | null>;
  cache_savings: CacheSavings;
}
