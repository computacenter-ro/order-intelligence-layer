"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowsClockwiseIcon } from "@phosphor-icons/react";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";
import { fetchLlmStats } from "@/lib/api";
import { formatDuration, formatTimestampFull, formatUpdatedAt } from "@/lib/format";
import {
  FEEDBACK_DISMISS_MS,
  SNAPSHOT_POLL_MS,
  isStale,
  updateFeedback,
} from "@/lib/aiPerformance";
import { StatCard } from "@/components/insights/StatCard";
import { ChartCard } from "@/components/insights/ChartCard";
import { SplitBar } from "@/components/insights/SplitBar";
import { OUTCOME_FAILED } from "@/lib/chartColors";
import type { CacheSavings, LlmNodeStats, LlmStats } from "@/lib/types";

type Window = "1h" | "24h" | "7d";

const WINDOWS: { value: Window; label: string }[] = [
  { value: "1h", label: "1h" },
  { value: "24h", label: "24h" },
  { value: "7d", label: "7d" },
];

// The four logical models llm.py tags, in pipeline order: an alert is explained
// then routed; summary and chat are the other two jobs. Titles are ours; the keys
// are the tags LangSmith is queried by.
const NODES: { key: keyof LlmStats["nodes"]; title: string; subtitle: string }[] = [
  {
    key: "explainer",
    title: "Explainer",
    subtitle: "LLM call 1 — plain-English explanation of a WARN/ERROR log",
  },
  {
    key: "router",
    title: "Router",
    subtitle: "LLM call 2 — department, severity and confidence",
  },
  {
    key: "summary",
    title: "Summary",
    subtitle: "Journey summaries, on completion",
  },
  {
    key: "chat",
    title: "Chat",
    subtitle: "Grounded answers for the assistant",
  },
];

/**
 * Distinguishes "measured zero" from "nothing to measure".
 *
 * The whole reason this page exists is to be trusted, so a missing LangSmith key
 * must never render as 0 calls / $0.00 — that reads as "the pipeline is idle and
 * free", which is the opposite of "we aren't looking". Nulls become an em dash,
 * matching how InsightsPage renders its unmeasurable ratio.
 */
function money(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  // Per-call LLM costs are fractions of a cent, so 2dp would show every model as
  // free. 4dp is the smallest that keeps a single explainer call visible.
  return `$${value.toFixed(4)}`;
}

function count(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : value.toLocaleString();
}

function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`;
}

export default function AiPerformancePage() {
  const [window, setWindow] = useState<Window>("24h");
  const [stats, setStats] = useState<LlmStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Post-click confirmation. Carries an `id` so two identical messages in a row are
  // still distinct objects — otherwise a second click wouldn't restart the
  // auto-dismiss timer and the message would vanish mid-read.
  const [feedback, setFeedback] = useState<{ text: string; id: number } | null>(null);
  // Wall clock at the moment the last successful read landed — the frame of
  // reference for the age label and the staleness check. Initialised to 0 rather
  // than Date.now() to keep the initialiser pure; it is always written together
  // with `stats`, and the render below is gated on `stats` being non-null, so the 0
  // is never used.
  const [readAt, setReadAt] = useState(0);

  // Refs, not state: these are read inside the fetch callback and must be current
  // at that moment. Putting `stats` in loadStats' dependency list instead would
  // rebuild the callback on every load and re-trigger the effect below — a fetch
  // loop.
  const fetchedAtRef = useRef<string | null>(null);
  const fromClickRef = useRef(false);
  const feedbackIdRef = useRef(0);

  // Same shape as InsightsPage's loadStats, with `window` as a dependency so
  // changing the control re-triggers the fetch. No WebSocket: LangSmith
  // aggregates aren't pushed over the app's hub (that carries alert/journey
  // events only), so this follows History's plain-fetch precedent.
  const loadStats = useCallback(() => {
    fetchLlmStats(window)
      .then((next) => {
        // ONE clock reading per load, shared by the age label and the feedback
        // message, so the two can never disagree about what time it is.
        const at = Date.now();
        const previous = fetchedAtRef.current;
        fetchedAtRef.current = next.fetched_at;
        setStats(next);
        setReadAt(at);
        // Clear a stale error so a recovered backend doesn't leave the error
        // screen up behind fresh data.
        setError(null);

        // A real delta retires any "no new data" message immediately — it has just
        // been contradicted by the very thing it described.
        if (next.fetched_at !== previous) setFeedback(null);

        // Only a deliberate click gets a message. A background poll landing on
        // unchanged data is the normal case and must stay silent.
        if (fromClickRef.current) {
          fromClickRef.current = false;
          const text = updateFeedback({
            ok: true,
            previousFetchedAt: previous,
            nextFetchedAt: next.fetched_at,
            refreshIntervalS: next.refresh_interval_s,
            now: at,
          });
          feedbackIdRef.current += 1;
          setFeedback(text === null ? null : { text, id: feedbackIdRef.current });
        }
      })
      .catch((err) => {
        console.error("Failed to load AI performance stats:", err);
        setError("Could not load the AI performance stats.");
        // The error owns the slot. Note the feedback is CLEARED rather than
        // computed: on failure the old stats stay on screen, so `fetched_at` is
        // trivially unchanged and a naive path here would claim "no new data" when
        // what actually happened is that the request failed.
        fromClickRef.current = false;
        setFeedback(null);
      })
      .finally(() => {
        setLoading(false);
        setRefreshing(false);
      });
  }, [window]);

  useEffect(() => {
    loadStats();
  }, [loadStats]);

  // The busy flag is raised HERE rather than inside loadStats, for two reasons:
  // the effect above also calls loadStats, and setting state synchronously in an
  // effect body is a lint error in this repo (react-hooks/set-state-in-effect);
  // and "in flight" is only meaningful for a deliberate click — the initial load
  // and a window switch already have their own affordances.
  const handleUpdate = useCallback(() => {
    setRefreshing(true);
    fromClickRef.current = true;
    loadStats();
  }, [loadStats]);

  // Poll the snapshot. Free — the AI service answers this from memory, which is the
  // whole point of the background refresher — so the interval is chosen for the
  // reader rather than for the provider. This re-render is ALSO what refreshes the
  // age label: there is deliberately no per-second timer, because the data behind
  // the label only moves once a cycle.
  //
  // Note the bare `setInterval`, not `window.setInterval`: `window` is shadowed by
  // the time-window state above, so the qualified form would be a string-property
  // access rather than the DOM timer.
  useEffect(() => {
    const id = setInterval(() => loadStats(), SNAPSHOT_POLL_MS);
    return () => clearInterval(id);
  }, [loadStats]);

  // Auto-dismiss the confirmation. Keyed on the whole object, so a repeat click
  // restarts the countdown instead of inheriting the previous one's remainder.
  useEffect(() => {
    if (feedback === null) return;
    const id = setTimeout(() => setFeedback(null), FEEDBACK_DISMISS_MS);
    return () => clearTimeout(id);
  }, [feedback]);

  if (loading) {
    return (
      <p style={{ fontSize: "16px", color: "var(--cc-grey-three)" }}>
        Loading AI performance…
      </p>
    );
  }

  // Gated on `!stats`, not on `error`: a failed REFETCH (after switching window)
  // must not tear down a screen of valid numbers. Note the backend degrades
  // rather than failing, so this branch is really only "the backend itself is
  // unreachable" — an unreachable AI service still returns a body full of nulls.
  if (!stats) {
    return (
      <div>
        <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
          AI Performance
        </h1>
        <p style={{ fontSize: "16px", color: "var(--cc-grey-three)", marginTop: "8px" }}>
          {error ?? "No stats available."} The server may be down — the alert feed and
          journeys pages will be empty too if so.
        </p>
      </div>
    );
  }

  // `readAt` (captured when the poll landed) rather than Date.now() here: reading
  // the clock during render is impure — React may re-render for unrelated reasons
  // and the value would jump — and the repo's react-hooks/purity rule rejects it.
  // The poll is what advances this, which is exactly why no per-second timer is
  // needed: the label is minute-granular and the data moves once a cycle.
  const updatedLabel = formatUpdatedAt(stats.fetched_at, readAt);
  // The refresher publishes into a snapshot, so if it dies the page keeps serving
  // its last numbers with no error anywhere. This label is the only place that
  // failure becomes visible, hence the warning tone.
  const stale = isStale(stats.fetched_at, stats.refresh_interval_s, readAt);

  return (
    <div>
      <h1 style={{ fontSize: "32px", fontWeight: 700, color: "var(--cc-heritage-blue)", margin: 0 }}>
        AI Performance
      </h1>
      <p
        style={{
          fontSize: "16px",
          color: "var(--cc-grey-three)",
          marginTop: "4px",
          marginBottom: "20px",
        }}
      >
        What each of the four LLM calls cost and how fast it ran, from LangSmith traces
      </p>

      {/* Controls row. A segmented window picker plus an explicit refresh —
          nothing pushes these numbers, so catching up is a deliberate act rather
          than something a live banner prompts. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: semanticSpacing.md,
          flexWrap: "wrap",
          marginBottom: semanticSpacing.lg,
        }}
      >
        <div
          role="group"
          aria-label="Time window"
          style={{
            display: "inline-flex",
            border: "1px solid var(--cc-grey-four)",
            borderRadius: radii.md,
            overflow: "hidden",
          }}
        >
          {WINDOWS.map((option) => {
            const selected = option.value === window;
            return (
              <button
                key={option.value}
                type="button"
                // aria-pressed, not just a colour change: the selected segment
                // must be announced, and colour alone is never the only cue.
                aria-pressed={selected}
                onClick={() => setWindow(option.value)}
                style={{
                  height: "32px",
                  padding: `0 ${semanticSpacing.base}`,
                  border: "none",
                  borderLeft:
                    option.value === WINDOWS[0].value
                      ? "none"
                      : "1px solid var(--cc-grey-four)",
                  background: selected ? "var(--cc-heritage-blue)" : "var(--cc-cloud-white)",
                  color: selected ? "var(--cc-cloud-white)" : "var(--cc-grey-one)",
                  fontSize: "14px",
                  fontWeight: 600,
                  fontFamily: "inherit",
                  cursor: "pointer",
                }}
              >
                {option.label}
              </button>
            );
          })}
        </div>

        <button
          type="button"
          className="oil-filter-toggle"
          onClick={handleUpdate}
          aria-label="Update AI performance stats"
          // The visual busy state comes from this attribute via
          // .oil-filter-toggle[aria-busy], so what a screen reader announces and
          // what the button looks like cannot fall out of step.
          aria-busy={refreshing}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: semanticSpacing.xs,
            height: "32px",
            padding: `0 ${semanticSpacing.md}`,
            // No `background` — see .oil-filter-toggle in globals.css. An inline
            // transparent fill outranks the class and disables hover/:active.
            border: "none",
            borderRadius: radii.md,
            color: "var(--cc-heritage-blue)",
            fontSize: "14px",
            fontWeight: 600,
            fontFamily: "inherit",
            cursor: "pointer",
          }}
        >
          <ArrowsClockwiseIcon size={16} aria-hidden="true" />
          Update
        </button>

        {/* Age of the DATA, not of this browser's last fetch — the two differ now
            that the AI service collects on a timer. Clicking Update re-reads a
            snapshot; if no cycle has run since, the age correctly keeps climbing
            instead of resetting to zero and implying fresh numbers. */}
        <span
          style={{
            fontSize: "13px",
            color: stale ? OUTCOME_FAILED : "var(--cc-grey-three)",
            fontWeight: stale ? 600 : 400,
          }}
          title={stats.fetched_at ? formatTimestampFull(stats.fetched_at) : undefined}
        >
          {updatedLabel}
          {/* Stated in words, not conveyed by colour alone. */}
          {stale && " — the background refresh may have stopped"}
        </span>

        {/* One slot, and the error wins it: a failed fetch is strictly more
            important than a confirmation that nothing changed, and showing both
            would have them contradict each other. */}
        {error ? (
          <span style={{ fontSize: "13px", color: OUTCOME_FAILED }}>
            {error} Showing the last values that loaded.
          </span>
        ) : (
          // aria-live because this message can be the ONLY thing that changes on
          // the page after a click — without it a screen reader user gets exactly
          // the silence this message exists to break. The region is always mounted
          // so the live announcement fires on content change; mounting it together
          // with the text is unreliable across screen readers.
          <span
            aria-live="polite"
            // Muted grey, never success-green: this is a confirmation that nothing
            // happened, not an achievement.
            style={{ fontSize: "13px", color: "var(--cc-grey-three)" }}
          >
            {feedback?.text ?? ""}
          </span>
        )}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: semanticSpacing.lg }}>
        {NODES.map((node) => (
          <NodeCard
            key={node.key}
            title={node.title}
            subtitle={node.subtitle}
            stats={stats.nodes[node.key]}
            configured={stats.langsmith_configured}
            collected={stats.fetched_at !== null}
          />
        ))}

        <CacheSavingsCard savings={stats.cache_savings} />
      </div>
    </div>
  );
}

interface NodeCardProps {
  title: string;
  subtitle: string;
  stats: LlmNodeStats | null;
  /** Whether the AI service has LangSmith creds (false also = service unreachable). */
  configured: boolean;
  /** Whether any refresh cycle has finished, i.e. `fetched_at !== null`. */
  collected: boolean;
}

const MONO = { fontFamily: "ui-monospace, Menlo, monospace" } as const;

function NodeCard({ title, subtitle, stats, configured, collected }: NodeCardProps) {
  if (stats === null) {
    // Never a grid of zeros: a null here means the number could not be READ, and
    // rendering that as 0 calls / $0.0000 would claim a measurement nobody made.
    //
    // But "unreadable" has three distinct causes, and this card used to blame all
    // of them on a missing API key. Two of those messages were wrong — the numbers
    // are collected on a timer now, so for the first seconds after a restart the
    // key is fine and the answer is simply "not yet".
    return (
      <ChartCard title={title} subtitle={subtitle}>
        <p style={{ fontSize: "14px", color: "var(--cc-grey-three)", margin: 0 }}>
          {!configured ? (
            <>
              Not configured yet — no LangSmith data for this model. Set{" "}
              <code style={MONO}>LANGSMITH_API_KEY</code> and{" "}
              <code style={MONO}>LANGSMITH_PROJECT</code> on the AI service to start
              collecting.
            </>
          ) : !collected ? (
            <>
              Collecting… the AI service refreshes these numbers on a timer, and the
              first cycle after a restart takes a few seconds.
            </>
          ) : (
            <>
              No data in the last collection — this model&apos;s query returned
              nothing or failed. It is retried on the next cycle.
            </>
          )}
        </p>
      </ChartCard>
    );
  }

  const idle = stats.run_count === 0;

  return (
    <ChartCard title={title} subtitle={subtitle}>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          gap: semanticSpacing.base,
        }}
      >
        <StatCard
          label="Calls"
          value={count(stats.run_count)}
          // Distinguishes the third state: queried fine, but nothing ran. Without
          // this a zero row looks identical to a broken integration.
          hint={idle ? "none in this window" : undefined}
        />
        <StatCard label="Latency p50" value={formatDuration(stats.latency_p50_s)} hint="median" />
        <StatCard label="Latency p99" value={formatDuration(stats.latency_p99_s)} hint="slowest 1%" />
        <StatCard
          label="Error rate"
          value={percent(stats.error_rate)}
          // Signal colour only when there is something to signal; the label says
          // "Error rate" either way, so colour is never the only cue.
          tone={stats.error_rate > 0 ? OUTCOME_FAILED : undefined}
        />
        <StatCard label="Cost" value={money(stats.total_cost_usd)} hint="total in window" />
        <StatCard label="Tokens" value={count(stats.total_tokens)} hint="prompt + completion" />
      </div>
    </ChartCard>
  );
}

function CacheSavingsCard({ savings }: { savings: CacheSavings }) {
  // Every field is nullable: the backend nulls all four when the AI service is
  // unreachable (it never saw the cache), while LangSmith being unconfigured
  // nulls only the money. Both have to render.
  const measured = savings.hits !== null && savings.misses !== null;

  return (
    <ChartCard
      title="Semantic cache savings"
      subtitle="A cache hit skips BOTH the explainer and the router call, so a hit is two calls not made"
    >
      {measured ? (
        <SplitBar
          label="Answers"
          primary={{ label: "cached", count: savings.hits ?? 0 }}
          secondary={{ label: "fresh", count: savings.misses ?? 0 }}
        />
      ) : (
        <p style={{ fontSize: "14px", color: "var(--cc-grey-three)", margin: "0 0 14px" }}>
          {/* Not a LangSmith problem: the counters live in Redis and are reported
              even with tracing unconfigured, so nulls here mean the backend could
              not reach the AI service at all. */}
          Cache counters unavailable — the AI service could not be reached.
        </p>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))",
          gap: semanticSpacing.base,
        }}
      >
        <StatCard label="Hit rate" value={percent(savings.hit_rate)} hint="of all answers" />
        <StatCard
          label="Estimated saved"
          value={money(savings.estimated_saved_usd)}
          // "—" is the honest answer when the per-call cost is unknown: the hits
          // are real, but multiplying them by a guessed price would invent a
          // figure. Never render this as $0.00.
          hint={
            savings.estimated_saved_usd === null
              ? "needs LangSmith cost data"
              : "hits x avg explainer + router cost"
          }
        />
      </div>
    </ChartCard>
  );
}
