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
