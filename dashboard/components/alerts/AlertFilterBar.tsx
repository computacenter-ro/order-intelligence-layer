"use client";

import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { capitalize } from "@/lib/format";
import type { Department, ProcessedAlert } from "@/lib/types";

// --- filter model ------------------------------------------------------------
//
// "all" is the UI-only sentinel meaning "no filter"; the page maps it to
// `undefined` before calling fetchAlerts (which omits the query param entirely).
// The concrete values mirror the backend contract exactly — Department values,
// ProcessedAlert.source, the WARN/ERROR levels, and app_name — so a selection
// round-trips to /alerts unchanged.

export type DepartmentFilter = Department | "all";
export type SourceFilter = "all" | "ai" | "fallback";
export type LevelFilter = "all" | "WARN" | "ERROR";
export type AppNameFilter = "all" | (typeof APP_NAMES)[number];
export type SeverityFilter = "all" | "critical" | "high" | "medium" | "low";

export interface AlertFilters {
  department: DepartmentFilter;
  source: SourceFilter;
  level: LevelFilter;
  app_name: AppNameFilter;
  severity: SeverityFilter;
}

export const DEFAULT_ALERT_FILTERS: AlertFilters = {
  department: "all",
  source: "all",
  level: "all",
  app_name: "all",
  severity: "all",
};

const DEPARTMENTS: Department[] = ["networking", "devops", "backend", "database", "general"];

// Fixed roster of the pipeline's emitters (CLAUDE.md [1] Services). app_name is
// a free string server-side, but the UI offers this closed list so the control
// is a dropdown rather than free text.
const APP_NAMES = [
  "cc-inbound-service",
  "cc-order-engine",
  "cc-spt-service",
  "cc-rsm-service",
  "cc-solr-service",
  "cc-jam-service",
  "cc-settings-service",
  "cc-checker-service",
  "cc-avalara-service",
  "cc-validator-service",
  "cc-outbound-osw",
  "cc-track-trace",
] as const;

const DEPARTMENT_FILTERS: DepartmentFilter[] = ["all", ...DEPARTMENTS];
const SOURCE_FILTERS: SourceFilter[] = ["all", "ai", "fallback"];
const LEVEL_FILTERS: LevelFilter[] = ["all", "WARN", "ERROR"];
const APP_NAME_FILTERS: AppNameFilter[] = ["all", ...APP_NAMES];

// Router LLM severities (shared/models.py Severity), most→least urgent.
const SEVERITIES = ["critical", "high", "medium", "low"] as const;
const SEVERITY_FILTERS: SeverityFilter[] = ["all", ...SEVERITIES];

// Labels spelled out where capitalize() would mangle them ("ai" -> "Ai").
const SOURCE_LABELS: Record<SourceFilter, string> = {
  all: "All Sources",
  ai: "AI",
  fallback: "Fallback",
};

const LEVEL_LABELS: Record<LevelFilter, string> = {
  all: "All Levels",
  WARN: "WARN",
  ERROR: "ERROR",
};

const SEVERITY_LABELS: Record<SeverityFilter, string> = {
  all: "All Severities",
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/**
 * Coerce an untrusted value (e.g. parsed localStorage) into valid filters,
 * falling back to the defaults for anything outside the known domain. Keeps a
 * stale or hand-edited `oil.alertFilters` entry from poisoning the UI.
 */
export function sanitizeAlertFilters(raw: unknown): AlertFilters {
  const obj = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const department = DEPARTMENT_FILTERS.includes(obj.department as DepartmentFilter)
    ? (obj.department as DepartmentFilter)
    : "all";
  const source = SOURCE_FILTERS.includes(obj.source as SourceFilter)
    ? (obj.source as SourceFilter)
    : "all";
  const level = LEVEL_FILTERS.includes(obj.level as LevelFilter)
    ? (obj.level as LevelFilter)
    : "all";
  const app_name = APP_NAME_FILTERS.includes(obj.app_name as AppNameFilter)
    ? (obj.app_name as AppNameFilter)
    : "all";
  const severity = SEVERITY_FILTERS.includes(obj.severity as SeverityFilter)
    ? (obj.severity as SeverityFilter)
    : "all";
  return { department, source, level, app_name, severity };
}

/**
 * The single source of truth for "does this alert belong in the feed under the
 * active filter?". Used both to guard live WS alerts (`alert.new`) and as the
 * mirror of the backend query — a live alert is admitted iff a re-fetch with
 * the same filters would have returned it (department AND source AND level AND
 * app_name, "all" = any). Fallback alerts have a null department, so any
 * non-"all" department filter excludes them — exactly as `Alert.department ==
 * department` does server-side.
 */
export function alertMatchesFilters(alert: ProcessedAlert, filters: AlertFilters): boolean {
  if (filters.department !== "all" && alert.department !== filters.department) return false;
  if (filters.source !== "all" && alert.source !== filters.source) return false;
  if (filters.level !== "all" && alert.level !== filters.level) return false;
  if (filters.app_name !== "all" && alert.app_name !== filters.app_name) return false;
  if (filters.severity !== "all" && alert.severity !== filters.severity) return false;
  return true;
}

// --- component ---------------------------------------------------------------

interface AlertFilterBarProps {
  value: AlertFilters;
  onChange: (next: AlertFilters) => void;
}

const labelStyle: React.CSSProperties = {
  fontSize: "14px",
  fontWeight: 500,
  lineHeight: "18px",
  color: "var(--cc-grey-one)",
  marginBottom: semanticSpacing.xs,
};

const selectStyle: React.CSSProperties = {
  height: "32px",
  minWidth: "180px",
  padding: `0 ${semanticSpacing.md}`,
  fontSize: "14px",
  fontFamily: "inherit",
  color: "var(--cc-grey-one)",
  backgroundColor: "var(--cc-cloud-white)",
  border: "1px solid var(--cc-grey-four)",
  borderRadius: radii.md,
  cursor: "pointer",
};

export function AlertFilterBar({ value, onChange }: AlertFilterBarProps) {
  return (
    <div
      style={{
        display: "flex",
        gap: semanticSpacing.lg,
        flexWrap: "wrap",
        alignItems: "flex-end",
        marginBottom: semanticSpacing.lg,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column" }}>
        <label htmlFor="alert-filter-severity" style={labelStyle}>
          Severity
        </label>
        <select
          id="alert-filter-severity"
          className="oil-filter-select"
          style={selectStyle}
          value={value.severity}
          onChange={(e) => onChange({ ...value, severity: e.target.value as SeverityFilter })}
        >
          {SEVERITY_FILTERS.map((sev) => (
            <option key={sev} value={sev}>
              {SEVERITY_LABELS[sev]}
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        <label htmlFor="alert-filter-level" style={labelStyle}>
          Level
        </label>
        <select
          id="alert-filter-level"
          className="oil-filter-select"
          style={selectStyle}
          value={value.level}
          onChange={(e) => onChange({ ...value, level: e.target.value as LevelFilter })}
        >
          {LEVEL_FILTERS.map((lvl) => (
            <option key={lvl} value={lvl}>
              {LEVEL_LABELS[lvl]}
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        <label htmlFor="alert-filter-app-name" style={labelStyle}>
          Service
        </label>
        <select
          id="alert-filter-app-name"
          className="oil-filter-select"
          style={selectStyle}
          value={value.app_name}
          onChange={(e) => onChange({ ...value, app_name: e.target.value as AppNameFilter })}
        >
          <option value="all">All Services</option>
          {APP_NAMES.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        <label htmlFor="alert-filter-department" style={labelStyle}>
          Department
        </label>
        <select
          id="alert-filter-department"
          className="oil-filter-select"
          style={selectStyle}
          value={value.department}
          onChange={(e) => onChange({ ...value, department: e.target.value as DepartmentFilter })}
        >
          <option value="all">All Departments</option>
          {DEPARTMENTS.map((dept) => (
            <option key={dept} value={dept}>
              {capitalize(dept)}
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: "flex", flexDirection: "column" }}>
        <label htmlFor="alert-filter-source" style={labelStyle}>
          Source
        </label>
        <select
          id="alert-filter-source"
          className="oil-filter-select"
          style={selectStyle}
          value={value.source}
          onChange={(e) => onChange({ ...value, source: e.target.value as SourceFilter })}
        >
          {SOURCE_FILTERS.map((src) => (
            <option key={src} value={src}>
              {SOURCE_LABELS[src]}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
