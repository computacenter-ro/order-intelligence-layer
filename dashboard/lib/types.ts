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

export interface ProcessedAlert {
  alert_id: string;
  emitted_at: string;
  log: LogLine;
  explanation: string | null;
  department: Department | null;
  confidence: number | null;
  source: "ai" | "fallback";
}

export type JourneyStatus = "in_progress" | "success" | "failed" | "timed_out";

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
  events: JourneyEvent[];
}

// Not consumed until Phase 3 — kept here now per the workplan's "define the
// contract once" instruction.
export type WsEvent =
  | { type: "alert.new"; payload: ProcessedAlert }
  | { type: "journey.updated"; payload: Journey }
  | { type: "journey.completed"; payload: Journey };

export type BadgeStatus =
  | "error"
  | "warning"
  | "pending"
  | "success"
  | "info"
  | "inactive"
  | "other"
  | "primary";
