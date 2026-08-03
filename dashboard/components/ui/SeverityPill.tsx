import { capitalize, levelLabel } from "@/lib/format";
import { severityPillTone } from "@/lib/severityPill";
import type { LogLevel, Severity } from "@/lib/types";

interface SeverityPillProps {
  level: LogLevel;
  severity: Severity | null;
}

/**
 * The combined level + severity pill, e.g. "Error · Critical".
 *
 * Hue is fixed by level (ERROR = red family, WARN = orange family); the shade
 * within that hue is set by severity (deeper = more severe). See
 * ``lib/severityPill.ts`` for the tone table. Reads "Error · High" when a
 * severity is present, or just the level (still hue-colored) otherwise.
 */
export function SeverityPill({ level, severity }: SeverityPillProps) {
  const tone = severityPillTone(level, severity);
  const label = severity
    ? `${levelLabel(level)} · ${capitalize(severity)}`
    : levelLabel(level);

  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: "9999px",
        border: `1px solid ${tone.border}`,
        backgroundColor: tone.bg,
        color: tone.text,
        fontSize: "12px",
        fontWeight: 600,
        lineHeight: "16px",
        padding: "2px 9px",
        whiteSpace: "nowrap",
      }}
    >
      {label}
    </span>
  );
}
