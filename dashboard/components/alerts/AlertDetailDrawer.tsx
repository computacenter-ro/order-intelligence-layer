import { useEffect } from "react";
import Link from "next/link";
import { XIcon } from "@phosphor-icons/react";
import { Badge } from "@/components/ui/Badge";
import { ConfidenceBar } from "@/components/ui/ConfidenceBar";
import { levelLabel, formatTime } from "@/lib/format";
import type { ProcessedAlert } from "@/lib/types";

interface AlertDetailDrawerProps {
  alert: ProcessedAlert | null;
  onClose: () => void;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: "14px",
        fontWeight: 500,
        color: "var(--cc-grey-three)",
        textTransform: "uppercase",
        letterSpacing: "1.6px",
        margin: "16px 0 8px",
      }}
    >
      {children}
    </div>
  );
}

function KeyValueRow({ label, value }: { label: string; value: string }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        padding: "7px 0",
        borderBottom: "1px solid var(--cc-grey-six)",
        fontSize: "14px",
      }}
    >
      <span style={{ color: "var(--cc-grey-three)" }}>{label}</span>
      <span style={{ fontFamily: "ui-monospace, Menlo, monospace", color: "var(--cc-grey-one)" }}>{value}</span>
    </div>
  );
}

export function AlertDetailDrawer({ alert, onClose }: AlertDetailDrawerProps) {

  useEffect(() => {
    if (!alert) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [alert, onClose]);

  if (!alert) return null;

  const isFallback = alert.source === "fallback";

  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(1, 23, 73, 0.5)",
          zIndex: 20,
        }}
      />
      <aside
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "440px",
          background: "var(--cc-cloud-white)",
          boxShadow: "var(--shadow-cc-xl, 0 32px 64px rgba(1,23,73,0.20))",
          zIndex: 21,
          overflowY: "auto",
          padding: "24px",
          boxSizing: "border-box",
        }}
      >
        <button
          onClick={onClose}
          aria-label="Close"
          style={{
            float: "right",
            background: "none",
            border: "none",
            color: "var(--cc-heritage-blue)",
            cursor: "pointer",
            padding: "4px",
          }}
        >
          <XIcon size={20} />
        </button>
        <div style={{ fontSize: "20px", fontWeight: 600, color: "var(--cc-foundation-blue)" }}>
          {alert.log.app_name}
        </div>
        <div style={{ fontSize: "14px", color: "var(--cc-grey-three)", marginBottom: "8px" }}>
          {levelLabel(alert.log.level)} · {formatTime(alert.emitted_at)}
        </div>

        <SectionLabel>Explanation</SectionLabel>
        <p
          style={{
            fontSize: "16px",
            lineHeight: "22px",
            color: isFallback ? "var(--cc-grey-three)" : "var(--cc-grey-one)",
            fontStyle: isFallback ? "italic" : "normal",
          }}
        >
          {alert.explanation ??
            "Unprocessed — LLM unavailable. This alert was passed straight through as a fallback and needs manual triage."}
        </p>
        <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
          {alert.source === "ai" ? (
            <>
              <Badge status="other">AI-analyzed</Badge>
              {alert.department && <Badge status="info">{alert.department}</Badge>}
              {alert.confidence != null && <ConfidenceBar confidence={alert.confidence} />}
            </>
          ) : (
            <Badge status="inactive">Fallback → #general-logs</Badge>
          )}
        </div>

        <SectionLabel>Correlation ids</SectionLabel>
        <KeyValueRow label="orderId" value={alert.log.orderId ?? "—"} />
        <KeyValueRow label="eventId" value={alert.log.eventId ?? "—"} />
        <KeyValueRow label="cartHeaderId" value={alert.log.cartHeaderId ?? "—"} />
        <KeyValueRow label="accountNumber" value={alert.log.accountNumber ?? "—"} />
        <KeyValueRow label="source" value={alert.source} />

        <SectionLabel>Raw log line</SectionLabel>
        <pre
          style={{
            background: "var(--cc-foundation-blue)",
            color: "var(--cc-cloud-white)",
            borderRadius: "8px",
            padding: "12px",
            fontFamily: "ui-monospace, Menlo, monospace",
            fontSize: "11px",
            whiteSpace: "pre-wrap",
            overflowX: "auto",
          }}
        >
          {JSON.stringify(alert.log, null, 2)}
        </pre>

        <SectionLabel>Related</SectionLabel>
        <Link
          href="/journeys"
          onClick={onClose}
          style={{ color: "var(--cc-heritage-blue)", fontSize: "14px", cursor: "pointer" }}
        >
          → View full order journey
        </Link>
      </aside>
    </>
  );
}
