"use client";

import { useEffect, useRef, useState } from "react";
import {
  ArrowLeftIcon,
  PaperPlaneRightIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
  XIcon,
} from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { Badge } from "@/components/ui/Badge";
import { AlertDetailBody } from "@/components/alerts/AlertDetailBody";
import {
  alertLoadErrorMessage,
  fetchAlert,
  sendChat,
  sendChatFeedback,
  UnauthorizedError,
} from "@/lib/api";
import { localizeUtcStamps } from "@/lib/format";
import { renderInlineMarkdown } from "@/lib/richText";
import type { ChatContext, ChatMode, ChatSource, ProcessedAlert } from "@/lib/types";

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
  /**
   * Heading for the chip list. In an unscoped chat the retrieved records ARE the
   * provenance of the answer, so "Sources" is accurate. In a scoped chat the
   * answer comes from the injected record instead, and these are merely other
   * records that resembled the query — calling those "Sources" would claim a
   * provenance they don't have.
   */
  sourcesLabel?: string;
  /** True for the "something went wrong" turn — styled as a warning, not an answer. */
  failed?: boolean;
  /** Identifies this answer for rating; absent on failed turns (nothing to rate). */
  answerId?: string;
  /** The question that produced it — sent with the vote for later analysis. */
  query?: string;
  /** null = not yet voted. Optimistic: set before the request returns. */
  liked?: boolean | null;
}

/** Thumbs up/down on one answer. */
function VoteButtons({
  liked,
  onVote,
}: {
  liked: boolean | null | undefined;
  onVote: (liked: boolean) => void;
}) {
  const base: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: "28px",
    height: "28px",
    background: "transparent",
    border: "1px solid var(--cc-grey-five)",
    borderRadius: "8px",
    cursor: "pointer",
    padding: 0,
    lineHeight: 0,
  };
  // Circuit Green / United Red are the palette's success and negative signals, and
  // this is exactly a small semantic indicator — the one place accent colours are
  // allowed. Unselected stays grey so neither option is visually pre-suggested.
  const up: React.CSSProperties =
    liked === true
      ? { ...base, borderColor: "var(--cc-circuit-green)", color: "var(--cc-circuit-green)" }
      : { ...base, color: "var(--cc-grey-three)" };
  const down: React.CSSProperties =
    liked === false
      ? { ...base, borderColor: "var(--cc-united-red)", color: "var(--cc-united-red)" }
      : { ...base, color: "var(--cc-grey-three)" };

  return (
    <div style={{ display: "flex", gap: "4px", marginLeft: "auto" }}>
      <button
        type="button"
        onClick={() => onVote(true)}
        style={up}
        aria-label="This answer was helpful"
        aria-pressed={liked === true}
        title="Helpful — nudges these records up in future searches"
      >
        <ThumbsUpIcon size={16} />
      </button>
      <button
        type="button"
        onClick={() => onVote(false)}
        style={down}
        aria-label="This answer was not helpful"
        aria-pressed={liked === false}
        title="Not helpful"
      >
        <ThumbsDownIcon size={16} />
      </button>
    </div>
  );
}

/**
 * Drop the scoped record from its own citation list.
 *
 * A scoped answer is built from the context the backend injects (that journey's
 * summary + log lines), not from retrieval — yet retrieval still runs on the
 * query text and returns whatever resembles it. Measured on a live scoped
 * question: 5 sources came back, 1 was the journey being asked about and 4 were
 * unrelated journeys. Listing the record you are already looking at is noise;
 * listing the others under "Sources" is worse, because it implies the answer drew
 * on them. So the record itself is removed and the remainder is relabelled by the
 * caller.
 */
export function withoutScopedRecord(
  sources: ChatSource[],
  scope: ChatContext | null
): ChatSource[] {
  if (!scope) return sources;
  return sources.filter((s) => s.id !== scope.id);
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

/**
 * Turn an order number typed into the question into an exact metadata filter.
 *
 * Embeddings are poor at exact identifiers — `ORD-8427` and `ORD-8435` are nearly
 * the same string to the model, so "why did ORD-8427 fail" can retrieve the wrong
 * order's records. The index stores `order_id` in metadata, and the backend
 * forwards `filters` verbatim, so lifting the id out of the prose turns a fuzzy
 * match into an exact one.
 *
 * Same shape as the stitcher's mining pattern (`\bORD-\d+\b`). Only the FIRST
 * match is used: two order numbers in one question means the agent is comparing
 * them, and filtering to one would silently answer half the question.
 */
const ORDER_ID_RE = /\bORD-\d+\b/g;

export function filtersFromQuery(query: string): Record<string, string> | null {
  const matches = query.match(ORDER_ID_RE);
  if (!matches || matches.length !== 1) return null;
  return { order_id: matches[0] };
}

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

/**
 * Documentation sources are collapsed into ONE chip, never listed individually.
 *
 * A doc citation is `jam-ws#blind-spots-and-traps--role-matching-is-exact` — a
 * chunk id inside a repository the support agent cannot open. Listing four of
 * them offers no way to verify anything and pushes the incident citations, which
 * DO link to the dashboard, out of sight. What matters to the reader is only that
 * the answer came from the official documentation rather than from log evidence,
 * so that is the single fact shown.
 *
 * They still travel in `sources` — the API is unchanged, and the ids stay
 * available for the evaluation set and for debugging in the network tab.
 */
const DOC_KIND = "doc";

function DocumentationChip({ count }: { count: number }) {
  // The shared Badge, not a hand-rolled pill: badge bg/border/text are three
  // accessibility-tested values per status and must never be re-derived locally.
  // `info` rather than the grey of a SourceChip, because this is a different KIND
  // of citation — reference material, not log evidence — and the greys are for
  // functional metadata.
  return (
    <span
      title={
        `${count} section(s) of the service documentation informed this answer. ` +
        `Documentation describes how a service works — it is not evidence that ` +
        `anything happened.`
      }
    >
      <Badge status="info">Official documentation</Badge>
    </span>
  );
}

/**
 * One cited record.
 *
 * Three shapes, in priority order. An ALERT citation opens the alert in this
 * panel (`onOpen`) rather than navigating: the record is fully renderable here,
 * and sending the reader to another page would abandon the conversation that
 * cited it. Anything else with a link is an ordinary anchor to the journey view.
 * A citation with neither stays plain text.
 *
 * Documentation citations never reach this component — they are collapsed into a
 * single badge by the caller (see DocumentationChip above).
 */
function SourceChip({
  source,
  onOpen,
}: {
  source: ChatSource;
  onOpen?: (source: ChatSource) => void;
}) {
  const label = `${source.kind} · ${source.id.slice(0, 8)}`;
  const title = `${source.snippet}\n\nrelevance ${source.score.toFixed(2)}`;
  const opensInPanel = source.kind === "alert" && onOpen !== undefined;

  const chipStyle: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: "4px",
    borderRadius: "9999px",
    border: "1px solid var(--cc-grey-five)",
    background: "var(--cc-grey-six)",
    color: opensInPanel || source.link ? "var(--cc-heritage-blue)" : "var(--cc-grey-two)",
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

  if (opensInPanel) {
    return (
      <button
        type="button"
        onClick={() => onOpen?.(source)}
        // font/family are inherited by anchors and spans but NOT by buttons, so
        // without these the one chip that is a button renders in the UA default.
        style={{ ...chipStyle, cursor: "pointer", fontFamily: "inherit" }}
        title={title}
      >
        {label}
      </button>
    );
  }

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

function TurnBubble({
  turn,
  onVote,
  onOpenSource,
}: {
  turn: Turn;
  onVote?: (turn: Turn, liked: boolean) => void;
  onOpenSource?: (source: ChatSource) => void;
}) {
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
            {/* Only real answers are rateable — a failed turn has nothing to rate. */}
            {turn.answerId && onVote && (
              <VoteButtons liked={turn.liked} onVote={(liked) => onVote(turn, liked)} />
            )}
          </div>
        )}

        {turn.sources && turn.sources.length > 0 && (
          <>
            <SectionLabel>{turn.sourcesLabel ?? "Sources"}</SectionLabel>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "8px" }}>
              {turn.sources
                .filter((s) => s.kind !== DOC_KIND)
                .map((s) => (
                  <SourceChip key={s.id} source={s} onOpen={onOpenSource} />
                ))}
              {/* All documentation chunks collapse to one chip — see DocumentationChip. */}
              {turn.sources.some((s) => s.kind === DOC_KIND) && (
                <DocumentationChip
                  count={turn.sources.filter((s) => s.kind === DOC_KIND).length}
                />
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/**
 * A cited alert, shown over the conversation.
 *
 * `alert` stays null while the fetch is in flight and after a failure, so the
 * three states are distinguishable without a second flag. `id` is kept so a late
 * response can be matched against the view that is actually open.
 */
interface DetailView {
  id: string;
  alert: ProcessedAlert | null;
  error: string | null;
}

export function ChatPanel({ open, onClose, context = null, contextLabel }: ChatPanelProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<DetailView | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Escape steps back ONE level: out of a cited record first, out of the panel
  // only from the conversation. Closing the whole panel from a detail view would
  // make the reader re-open it and re-find their place, and the conversation is
  // still right behind the record they are reading.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (detail) setDetail(null);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, detail]);

  // Focus the composer when the panel opens so it is keyboard-usable immediately
  // (WCAG 2.1 focus management: opening an overlay should move focus into it).
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Keep the newest turn in view as the conversation grows. Skipped while a cited
  // record is open: the scroll region is showing the record then, and scrolling it
  // to the bottom would drop the reader at the raw log line.
  useEffect(() => {
    if (detail) return;
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [turns, busy, detail]);

  if (!open) return null;

  /**
   * Record a thumbs up/down.
   *
   * Optimistic and deliberately un-revertible on failure: the click is a nicety,
   * not the work, so a dropped vote must never bounce the button back and imply
   * the agent mis-clicked. Clicking the same side again is idempotent server-side
   * (the row is keyed by answer_id), so a double-click cannot inflate the tally.
   */
  const vote = (turn: Turn, liked: boolean) => {
    if (!turn.answerId) return;
    const answerId = turn.answerId;
    setTurns((prev) =>
      prev.map((t) => (t.answerId === answerId ? { ...t, liked } : t))
    );
    void sendChatFeedback({
      answer_id: answerId,
      liked,
      query: turn.query ?? "",
      // In the ORDER SHOWN — the backend attributes credit by citation rank, so a
      // reordered list would mis-credit the sources.
      //
      // Documentation chunks are excluded: the boost they would earn is never
      // read (the docs index ranks without feedback, so a downvote on a badly
      // worded answer cannot demote a correct reference page). Including them
      // would only dilute the vote across records that can never spend it.
      record_ids: (turn.sources ?? []).filter((s) => s.kind !== DOC_KIND).map((s) => s.id),
      answer_mode: turn.mode,
      scoped_kind: context?.kind ?? null,
      scoped_id: context?.id ?? null,
    });
  };

  /**
   * Open a cited alert over the conversation.
   *
   * Every `setDetail` in the async paths re-checks `prev.id`: clicking chip A then
   * chip B before A resolves must leave B on screen, and without the guard A's
   * late response would overwrite it. Nothing here touches `turns`, so the
   * conversation is untouched and Back restores it exactly.
   */
  const openSource = (source: ChatSource) => {
    if (source.kind !== "alert") return;
    setDetail({ id: source.id, alert: null, error: null });
    fetchAlert(source.id)
      .then((alert) =>
        setDetail((prev) => (prev && prev.id === source.id ? { ...prev, alert } : prev))
      )
      .catch((err) => {
        // Wording shared with the Alert Feed's `/?alert=` deep link, which loads an
        // alert by id the same way and can only fail the same two ways. It draws
        // the distinction the composer draws: an expired session is actionable,
        // anything else is not.
        const message = alertLoadErrorMessage(err);
        setDetail((prev) => (prev && prev.id === source.id ? { ...prev, error: message } : prev));
      });
  };

  const ask = async (question: string) => {
    const query = question.trim();
    if (!query || busy) return;

    setTurns((prev) => [...prev, { role: "user", text: query }]);
    setDraft("");
    setBusy(true);
    try {
      // A scoped conversation is already anchored to one record, so an order-id
      // filter would only narrow it further (and could contradict the scope).
      const filters = context ? null : filtersFromQuery(query);
      // The browser is the only party that knows the reader's zone, so it tells
      // the backend: scoped context timestamps then come back already local.
      const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || undefined;
      const res = await sendChat({ query, k: 5, context, filters, tz });
      setTurns((prev) => [
        ...prev,
        {
          role: "assistant",
          answerId: res.answer_id,
          query,
          liked: null,
          // Safety net for the other path: records from the INDEX carry UTC (they
          // are shared by every viewer), so any such stamp the model quoted is
          // rewritten to local here. Scoped answers are already local.
          text: localizeUtcStamps(res.answer),
          mode: res.mode,
          sources: withoutScopedRecord(res.sources, context),
          // Scoped: the answer came from the injected record, so these are
          // lookalikes, not provenance. Unscoped: they genuinely are the sources.
          sourcesLabel: context ? "Similar incidents" : "Sources",
        },
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
        aria-label="AI Assistant"
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
          {detail ? (
            // Back, not a second close: the conversation is still mounted behind
            // this record, so the reader has somewhere to return TO. A close X
            // here would throw away the thread that cited the record.
            <button
              type="button"
              onClick={() => setDetail(null)}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "6px",
                background: "none",
                border: "none",
                padding: "4px 0",
                color: "var(--cc-heritage-blue)",
                fontSize: "14px",
                fontWeight: 600,
                fontFamily: "inherit",
                cursor: "pointer",
              }}
            >
              <ArrowLeftIcon size={16} />
              Back to conversation
            </button>
          ) : (
            <div>
              <div style={{ fontSize: "20px", fontWeight: 600, color: "var(--cc-foundation-blue)" }}>
                AI Assistant
              </div>
              {/* Only ever announces a SCOPE. Unscoped, the row is omitted entirely
                  rather than filled with a description of how the assistant works:
                  the subtitle's job is to tell you the answers are narrowed to one
                  record, so text here when nothing is narrowed reads as a caveat and
                  competes with the empty state, which already explains the grounding. */}
              {contextLabel ? (
                <div style={{ fontSize: "14px", color: "var(--cc-grey-three)", marginTop: "4px" }}>
                  Scoped to {contextLabel}
                </div>
              ) : null}
            </div>
          )}
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

        {/* Message list — the only scrolling region. A cited record takes it over
            rather than stacking a second overlay: the panel already owns Escape
            and a scrim, and a nested dialog would duplicate both. */}
        <div
          ref={listRef}
          style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "16px 24px" }}
        >
          {detail ? (
            detail.alert ? (
              // No onAsk: this record IS being read inside the assistant, so
              // offering to ask the assistant about it would loop back here.
              <AlertDetailBody alert={detail.alert} onNavigate={onClose} />
            ) : (
              <div
                role="status"
                aria-live="polite"
                style={{ color: "var(--cc-grey-three)", fontSize: "14px" }}
              >
                {detail.error ?? "Loading alert…"}
              </div>
            )
          ) : (
            <>
          {turns.length === 0 && !busy && (
            <div style={{ color: "var(--cc-grey-three)", fontSize: "16px", lineHeight: "22px" }}>
              <p style={{ margin: "0 0 16px" }}>
                Ask about past alerts and order journeys, or how a service works. Every
                answer cites the sources it used.
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
            <TurnBubble key={i} turn={turn} onVote={vote} onOpenSource={openSource} />
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
            </>
          )}
        </div>

        {/* Composer — pinned, and hidden while a cited record is open: there is
            nothing to ask into from a record view, and leaving it would invite a
            question that silently lands back in the conversation behind. */}
        {!detail && (
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
            // Deliberately open-ended: /chat grounds in BOTH incident history and
            // the per-service documentation index, so naming either one narrows what
            // people think to ask. The empty state spells out both.
            placeholder="Ask something…"
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
        )}
      </aside>
    </>
  );
}
