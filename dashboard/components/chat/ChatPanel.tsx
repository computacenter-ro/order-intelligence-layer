"use client";

import { useEffect, useRef, useState } from "react";
import { PaperPlaneRightIcon, XIcon } from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { Badge } from "@/components/ui/Badge";
import { sendChat, UnauthorizedError } from "@/lib/api";
import { renderInlineMarkdown } from "@/lib/richText";
import type { ChatContext, ChatMode, ChatSource } from "@/lib/types";

/**
 * The assistant drawer: ask questions about incident history and get an answer
 * grounded in retrieved alerts and journeys.
 *
 * Structure mirrors ``components/alerts/AlertDetailDrawer.tsx`` — a fixed
 * right-side ``<aside>`` over a Foundation-Blue scrim, Escape to close — so the
 * two overlays feel like one thing. It differs in being a flex column: the
 * message list scrolls while the composer stays pinned to the bottom.
 *
 * Non-streaming: one request, one answer. The backend degrades internally (an
 * LLM outage returns ``mode: "retrieval-only"`` with the same sources), so a
 * thrown error here means auth or transport failed — not "no answer".
 */

interface ChatPanelProps {
  open: boolean;
  onClose: () => void;
  /**
   * Scopes the conversation to one record (the "Ask about this" buttons). The
   * backend prepends that record's text to the question, so the first answer is
   * about what the agent is looking at rather than whatever the query retrieves.
   */
  context?: ChatContext | null;
  /** Shown above the composer so it is obvious the answer is scoped. */
  contextLabel?: string;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
  mode?: ChatMode;
  sources?: ChatSource[];
  /** True for the "something went wrong" turn — styled as a warning, not an answer. */
  failed?: boolean;
}

const PANEL_WIDTH = 480;

// Distinct, on-palette badge statuses. "other" (purple) is what the alert feed
// already uses for AI-analyzed and "inactive" (grey) for fallback — reusing them
// keeps one visual language for provenance across the app.
const MODE_BADGE: Record<ChatMode, { status: "other" | "inactive"; label: string; title: string }> = {
  ai: {
    status: "other",
    label: "AI",
    title: "Composed by the LLM from the cited incident records",
  },
  "retrieval-only": {
    status: "inactive",
    label: "Retrieval-only",
    title: "The LLM was unavailable — this lists the matching records without a written answer",
  },
};

const SUGGESTIONS = [
  "Why did SAP submission fail?",
  "Which orders were blocked by the margin check?",
  "What went wrong with order validation?",
];

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

/** One cited record: a pill linking to the journey view when a link exists. */
function SourceChip({ source }: { source: ChatSource }) {
  const label = `${source.kind} · ${source.id.slice(0, 8)}`;
  const title = `${source.snippet}\n\nrelevance ${source.score.toFixed(2)}`;

  const chipStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: "4px",
    borderRadius: "9999px",
    border: "1px solid var(--cc-grey-five)",
    background: "var(--cc-grey-six)",
    color: source.link ? "var(--cc-heritage-blue)" : "var(--cc-grey-two)",
    fontSize: "12px",
    fontWeight: 600,
    lineHeight: "16px",
    padding: "2px 9px",
    textDecoration: "none",
    maxWidth: "100%",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  };

  // Heritage Blue is reserved for navigable text, so an unlinked citation stays
  // grey rather than looking clickable (guidelines: tables/lists colour rules).
  if (!source.link) {
    return (
      <span style={chipStyle} title={title}>
        {label}
      </span>
    );
  }
  return (
    <a href={source.link} style={chipStyle} title={title} target="_blank" rel="noopener noreferrer">
      {label}
    </a>
  );
}

function TurnBubble({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "16px" }}>
        <div
          style={{
            maxWidth: "85%",
            background: "var(--cc-heritage-blue)",
            color: "var(--cc-cloud-white)",
            borderRadius: "8px",
            padding: "8px 12px",
            fontSize: "16px",
            lineHeight: "22px",
          }}
        >
          {turn.text}
        </div>
      </div>
    );
  }

  const badge = turn.mode ? MODE_BADGE[turn.mode] : null;

  return (
    <div style={{ marginBottom: "16px" }}>
      <div
        style={{
          background: turn.failed ? "var(--cc-grey-six)" : "var(--cc-cloud-white)",
          border: `1px solid ${turn.failed ? "var(--cc-grey-four)" : "var(--cc-grey-six)"}`,
          borderRadius: "8px",
          boxShadow: turn.failed ? "none" : "0 2px 8px rgba(1,23,73,0.06)",
          padding: "12px",
        }}
      >
        <p
          style={{
            fontSize: "16px",
            lineHeight: "22px",
            color: turn.failed ? "var(--cc-grey-two)" : "var(--cc-grey-one)",
            margin: 0,
            whiteSpace: "pre-wrap",
          }}
        >
          {renderInlineMarkdown(turn.text)}
        </p>

        {badge && (
          <div style={{ display: "flex", gap: "8px", alignItems: "center", marginTop: "12px" }}>
            <span title={badge.title}>
              <Badge status={badge.status}>{badge.label}</Badge>
            </span>
          </div>
        )}

        {turn.sources && turn.sources.length > 0 && (
          <>
            <SectionLabel>Sources</SectionLabel>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
              {turn.sources.map((s) => (
                <SourceChip key={s.id} source={s} />
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function ChatPanel({ open, onClose, context = null, contextLabel }: ChatPanelProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Escape closes, matching AlertDetailDrawer.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Focus the composer when the panel opens so it is keyboard-usable immediately
  // (WCAG 2.1 focus management: opening an overlay should move focus into it).
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Keep the newest turn in view as the conversation grows.
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [turns, busy]);

  if (!open) return null;

  const ask = async (question: string) => {
    const query = question.trim();
    if (!query || busy) return;

    setTurns((prev) => [...prev, { role: "user", text: query }]);
    setDraft("");
    setBusy(true);
    try {
      const res = await sendChat({ query, k: 5, context });
      setTurns((prev) => [
        ...prev,
        { role: "assistant", text: res.answer, mode: res.mode, sources: res.sources },
      ]);
    } catch (err) {
      // Two failure shapes worth telling apart: an expired session is
      // actionable by the user, anything else is not.
      const text =
        err instanceof UnauthorizedError
          ? "Your session has expired. Please sign in again to keep using the assistant."
          : "The assistant is unreachable right now. Alerts and journeys are still searchable from the dashboard — try again in a moment.";
      setTurns((prev) => [...prev, { role: "assistant", text, failed: true }]);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div
        onClick={onClose}
        style={{ position: "fixed", inset: 0, background: "rgba(1, 23, 73, 0.5)", zIndex: 20 }}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Incident assistant"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: `${PANEL_WIDTH}px`,
          maxWidth: "100vw",
          background: "var(--cc-cloud-white)",
          boxShadow: "var(--shadow-cc-xl, 0 32px 64px rgba(1,23,73,0.20))",
          zIndex: 21,
          display: "flex",
          flexDirection: "column",
          boxSizing: "border-box",
        }}
      >
        {/* Header — fixed while the list below scrolls. */}
        <div
          style={{
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "space-between",
            gap: "8px",
            padding: "24px 24px 12px",
            borderBottom: "1px solid var(--cc-grey-six)",
          }}
        >
          <div>
            <div style={{ fontSize: "20px", fontWeight: 600, color: "var(--cc-foundation-blue)" }}>
              Incident Assistant
            </div>
            <div style={{ fontSize: "14px", color: "var(--cc-grey-three)", marginTop: "4px" }}>
              {contextLabel ? `Scoped to ${contextLabel}` : "Answers grounded in indexed incidents"}
            </div>
          </div>
          <button
            onClick={onClose}
            aria-label="Close assistant"
            style={{
              background: "none",
              border: "none",
              color: "var(--cc-heritage-blue)",
              cursor: "pointer",
              padding: "4px",
              lineHeight: 0,
            }}
          >
            <XIcon size={20} />
          </button>
        </div>

        {/* Message list — the only scrolling region. */}
        <div
          ref={listRef}
          style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "16px 24px" }}
        >
          {turns.length === 0 && !busy && (
            <div style={{ color: "var(--cc-grey-three)", fontSize: "16px", lineHeight: "22px" }}>
              <p style={{ margin: "0 0 16px" }}>
                Ask about past alerts and order journeys. Every answer cites the records it used.
              </p>
              <SectionLabel>Try</SectionLabel>
              <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    type="button"
                    onClick={() => void ask(s)}
                    style={{
                      textAlign: "left",
                      background: "var(--cc-grey-six)",
                      border: "1px solid var(--cc-grey-five)",
                      borderRadius: "8px",
                      padding: "8px 12px",
                      fontSize: "14px",
                      color: "var(--cc-heritage-blue)",
                      cursor: "pointer",
                    }}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {turns.map((turn, i) => (
            <TurnBubble key={i} turn={turn} />
          ))}

          {busy && (
            <div
              role="status"
              aria-live="polite"
              style={{ color: "var(--cc-grey-three)", fontSize: "14px", padding: "4px 0" }}
            >
              Searching incident history…
            </div>
          )}
        </div>

        {/* Composer — pinned. */}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void ask(draft);
          }}
          style={{
            display: "flex",
            gap: "8px",
            padding: "12px 24px 24px",
            borderTop: "1px solid var(--cc-grey-six)",
          }}
        >
          <input
            ref={inputRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Ask about an incident…"
            aria-label="Your question"
            disabled={busy}
            style={{
              flex: 1,
              minWidth: 0,
              height: "40px",
              padding: "0 12px",
              fontSize: "16px",
              lineHeight: "20px",
              color: "var(--cc-grey-one)",
              background: "var(--cc-cloud-white)",
              border: "1px solid var(--cc-grey-four)",
              borderRadius: "8px",
              outlineColor: "var(--cc-heritage-blue)",
            }}
          />
          <Button
            variant="primary"
            type="submit"
            disabled={busy || !draft.trim()}
            loading={busy}
            leftIcon={<PaperPlaneRightIcon size={20} />}
          >
            Send
          </Button>
        </form>
      </aside>
    </>
  );
}
