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
