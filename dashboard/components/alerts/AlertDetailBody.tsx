import Link from "next/link";
import { ChatCircleDotsIcon } from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { Badge } from "@/components/ui/Badge";
import { SeverityPill } from "@/components/ui/SeverityPill";
import { formatTime, formatTimestampFull, capitalize } from "@/lib/format";
import { highlightRuns, renderInlineMarkdown } from "@/lib/richText";
import type { ProcessedAlert } from "@/lib/types";

/**
 * Everything an alert shows, minus the container it shows up in.
 *
 * Extracted because two surfaces render an alert now: the right-side
 * ``AlertDetailDrawer`` opened from the feed, and the assistant panel's citation
 * detail view. They must not drift — an agent comparing an alert cited by the
 * assistant against the same alert in the feed has to be looking at the same
 * fields — so the markup lives here once and each surface supplies only its own
 * chrome (the drawer's close X, the panel's Back button).
 *
 * Deliberately owns no positioning, no scrim and no Escape handling: those belong
 * to whichever overlay is hosting it, and duplicating them is what makes two
 * stacked dialogs fight over the same key.
 */

interface AlertDetailBodyProps {
  alert: ProcessedAlert;
  /** Active search term, highlighted in the explanation and the raw log. */
  search?: string;
  /**
   * Rendered as an "Ask About This" button when provided. Omitted inside the
   * assistant panel: offering to ask the assistant about a record you are already
   * looking at *in* the assistant would loop back on itself.
   */
  onAsk?: () => void;
  /**
   * Called when the "View Full Order Journey" link is followed, so the host can
   * dismiss itself before the route changes and not leave an overlay stranded
   * over the new page.
   */
  onNavigate?: () => void;
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
      <span style={{ fontFamily: "ui-monospace, Menlo, monospace", color: "var(--cc-grey-one)" }}>
        {value}
      </span>
    </div>
  );
}

export function AlertDetailBody({ alert, search, onAsk, onNavigate }: AlertDetailBodyProps) {
  const isFallback = alert.source === "fallback";

  return (
    <>
      <div style={{ fontSize: "20px", fontWeight: 600, color: "var(--cc-foundation-blue)" }}>
        {alert.app_name}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginBottom: "8px" }}>
        <SeverityPill level={alert.level} severity={alert.severity} />
        <span
          style={{ fontSize: "14px", color: "var(--cc-grey-three)" }}
          title={formatTimestampFull(alert.emitted_at)}
        >
          {formatTime(alert.emitted_at)}
        </span>
      </div>

      {onAsk && (
        // Primary, and at the top: it is the only action here now that "Related"
        // holds just a link, so the guidelines' one-primary-per-view rule is
        // satisfied — and it matches the incident/journey detail pages, where
        // asking the assistant is also the primary action.
        //
        // `clear: both` because the host may float a close control to the right of
        // the heading above; without it a button placed this high can be pushed
        // out of the float's column. It stays on its OWN row rather than joining
        // the severity/timestamp row for the same reason.
        <div
          style={{
            clear: "both",
            marginTop: "4px",
            display: "flex",
            justifyContent: "flex-end",
          }}
        >
          <Button
            variant="primary"
            size="compact"
            leftIcon={<ChatCircleDotsIcon size={20} />}
            onClick={onAsk}
          >
            Ask About This
          </Button>
        </div>
      )}

      <SectionLabel>Explanation</SectionLabel>
      <p
        style={{
          fontSize: "16px",
          lineHeight: "22px",
          color: isFallback ? "var(--cc-grey-three)" : "var(--cc-grey-one)",
          fontStyle: isFallback ? "italic" : "normal",
        }}
      >
        {alert.explanation
          ? renderInlineMarkdown(alert.explanation, search)
          : "Unprocessed — LLM unavailable. This alert was passed straight through as a fallback and needs manual triage."}
      </p>
      <div style={{ display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" }}>
        {alert.source === "ai" ? (
          <>
            <Badge status="other">AI-analyzed</Badge>
            {/* Same pairing as AlertCard: cached modifies AI-analyzed. */}
            {alert.cached && (
              <span title="Reused from a previous identical alert — no new LLM call">
                <Badge status="primary">Cached</Badge>
              </span>
            )}
            {alert.department && <Badge status="info">{capitalize(alert.department)}</Badge>}
          </>
        ) : (
          <Badge status="inactive">Fallback → #general-logs</Badge>
        )}
      </div>

      <SectionLabel>Correlation ids</SectionLabel>
      <KeyValueRow label="orderId" value={alert.order_id ?? "—"} />
      <KeyValueRow label="eventId" value={alert.event_id ?? "—"} />
      <KeyValueRow label="cartHeaderId" value={alert.cart_header_id ?? "—"} />
      <KeyValueRow label="accountNumber" value={alert.account_number ?? "—"} />
      <KeyValueRow label="source" value={alert.source} />
      <KeyValueRow label="cached" value={alert.cached ? "yes" : "no"} />

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
        {/* highlightRuns, not renderInlineMarkdown: this is a raw log dump, so a
            backtick or ** in a log line is literal text, not a marker. Marking
            the whole block is what surfaces a hit that exists only in
            `message` — the term never appears in the explanation for those. */}
        {highlightRuns(
          JSON.stringify(
            {
              log_id: alert.log_id,
              level: alert.level,
              app_name: alert.app_name,
              logger: alert.logger,
              message: alert.message,
              event_id: alert.event_id,
              order_id: alert.order_id,
              cart_header_id: alert.cart_header_id,
              account_number: alert.account_number,
            },
            null,
            2
          ),
          search
        )}
      </pre>

      <SectionLabel>Related</SectionLabel>
      <div style={{ display: "flex", flexDirection: "column", gap: "12px", alignItems: "flex-start" }}>
        <Link
          href={alert.journey_id ? `/journeys?highlight=${alert.journey_id}` : "/journeys"}
          onClick={onNavigate}
          style={{ color: "var(--cc-heritage-blue)", fontSize: "14px", cursor: "pointer" }}
        >
          → View Full Order Journey
        </Link>
      </div>
    </>
  );
}
