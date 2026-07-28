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
  confidence: number | null;
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
  | { type: "journey.completed"; data: Journey };

export type BadgeStatus =
  | "error"
  | "warning"
  | "pending"
  | "success"
  | "info"
  | "inactive"
  | "other"
  | "primary";

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
  by_department: Record<string, number>;
  by_severity: Record<string, number>;
  by_level: Record<string, number>;
  by_source: Record<string, number>;
}

export interface OverviewStats {
  journeys: JourneyStats;
  alerts: AlertStats;
}

// --- chat (POST /chat) -------------------------------------------------------
//
// Mirrors backend/schemas.py ChatRequest/ChatResponse. The backend is an
// authenticated proxy in front of the AI service, which does the retrieval and
// (when its LLM is up) the grounded composition.

/** Scope a question to one record — the "Ask about this" buttons. */
export interface ChatContext {
  kind: "alert" | "journey";
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
}
