import type { LogLevel, Severity } from "@/lib/types";

/**
 * Colors for the combined level+severity pill.
 *
 * The HUE is fixed by the log LEVEL — ERROR is always the red family, WARN
 * always the orange family — so a viewer never has to decode "which color means
 * error?". The SHADE within that hue is driven by SEVERITY: higher severity =
 * deeper / more saturated, lower = paler. Each tone is a {bg, border, text}
 * triple in one hue (same three-color pattern as the CC badge table), so the
 * pill stays legible while its intensity communicates urgency at a glance.
 *
 * Reds are built around United Red (#F12938), oranges around Fibre Orange
 * (#FF7900) — both brand palette colors — darkened/lightened by severity.
 */
export interface PillTone {
  bg: string;
  border: string;
  text: string;
}

// ERROR → red family, critical (deepest) → low (palest).
const ERROR_TONES: Record<Severity, PillTone> = {
  critical: { bg: "#FBD0D3", border: "#A30914", text: "#7A0510" },
  high: { bg: "#FDDCDE", border: "#F12938", text: "#A30914" },
  medium: { bg: "#FEE7E9", border: "#F5707B", text: "#C21422" },
  low: { bg: "#FFF1F2", border: "#F9A8AF", text: "#D64450" },
};

// WARN → orange family, critical (deepest) → low (palest).
const WARN_TONES: Record<Severity, PillTone> = {
  critical: { bg: "#FFE0C2", border: "#B45500", text: "#8A4100" },
  high: { bg: "#FFE8D1", border: "#FF7900", text: "#B45500" },
  medium: { bg: "#FFF1E0", border: "#FFA352", text: "#C56200" },
  low: { bg: "#FFF7EE", border: "#FFC894", text: "#D97A1F" },
};

// Neutral grey for fallback / unknown (no severity to color by).
const NEUTRAL: PillTone = {
  bg: "var(--cc-grey-six)",
  border: "var(--cc-grey-four)",
  text: "var(--cc-grey-two)",
};

/**
 * Pick the pill tone for a level + severity. Without a severity (fallback),
 * return a mid tone of the level's hue so the pill still reads as error/warn.
 */
export function severityPillTone(
  level: LogLevel,
  severity: Severity | null
): PillTone {
  const isError = level === "ERROR";
  if (severity == null) {
    // No severity: use the level's "high" tone as a sensible default hue,
    // or neutral grey for non-ERROR/WARN levels.
    if (isError) return ERROR_TONES.high;
    if (level === "WARN") return WARN_TONES.high;
    return NEUTRAL;
  }
  if (isError) return ERROR_TONES[severity];
  if (level === "WARN") return WARN_TONES[severity];
  return NEUTRAL;
}
