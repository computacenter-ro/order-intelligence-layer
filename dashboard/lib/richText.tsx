import { Fragment, type CSSProperties, type ReactNode } from "react";

/**
 * Render LLM-authored text with the minimal inline markdown the AI service
 * actually emits: `**bold**` → <strong>, and `` `code` `` → inline <code>.
 *
 * The explainer / journey summary write plain prose that sometimes includes
 * `**emphasis**` and `` `identifiers` `` (service names, order ids, log fields).
 * Rendered as a raw string those markers show literally; this turns them into
 * styled spans. It deliberately supports ONLY those two — no links, lists, or
 * block markup — avoiding a full markdown dependency and, crucially, any HTML
 * interpretation: output is plain React text nodes plus <strong>/<code>
 * wrappers, so there is no `dangerouslySetInnerHTML` and no XSS surface.
 *
 * A single left-to-right scan handles both markers in any order. An unmatched
 * opener (no closing `**` / `` ` ``) is emitted as literal text, so partial or
 * malformed model output degrades gracefully instead of swallowing the rest.
 *
 * With a `highlight` term, every case-insensitive occurrence is wrapped in a
 * <mark> — inside plain runs AND inside bold/code spans, so a hit is never
 * invisible just because the model happened to emphasise it.
 */

const CODE_STYLE: CSSProperties = {
  fontFamily: "ui-monospace, Menlo, monospace",
  fontSize: "0.9em",
  background: "var(--cc-grey-six)",
  color: "var(--cc-grey-one)",
  padding: "1px 5px",
  borderRadius: "4px",
};

// Pale amber. `color: inherit` matters: the surrounding text may be grey (a
// fallback explanation) or inside <code>, and forcing a color here would fight
// those. Only the background marks the hit, so the text keeps its own meaning.
const MARK_STYLE: CSSProperties = {
  background: "#fff3bf",
  color: "inherit",
  padding: "0 1px",
  borderRadius: "2px",
};

/**
 * Split `text` on case-insensitive occurrences of `query`, wrapping each hit in
 * a <mark>. A blank query or no match returns `text` unchanged (the same string,
 * not a one-element array), so the no-search path costs nothing.
 *
 * Matching is done by scanning lowercased copies rather than with a RegExp,
 * which sidesteps escaping the query's regex metacharacters entirely — the term
 * comes from a free-text box, so `(`, `.`, `*` and friends are all expected
 * input and must be treated as literals. (The backend dodges the equivalent
 * hazard by escaping LIKE wildcards.)
 *
 * Exported for text that is NOT markdown — e.g. the drawer's raw-log block,
 * where running it through `renderInlineMarkdown` would eat backticks that are
 * part of the log line.
 */
export function highlightRuns(text: string, query?: string): ReactNode {
  const term = query?.trim() ?? "";
  if (term === "") return text;

  const haystack = text.toLowerCase();
  const needle = term.toLowerCase();
  // Lowercasing must not change the length, or the offsets below would slice the
  // ORIGINAL string in the wrong places and render corrupted text (a few Unicode
  // characters, e.g. 'İ', expand when lowercased). Unmarked text is a safe
  // degradation; mangled text is not.
  if (haystack.length !== text.length) return text;

  let at = haystack.indexOf(needle);
  if (at === -1) return text;

  const nodes: ReactNode[] = [];
  let pos = 0;
  let key = 0;
  while (at !== -1) {
    if (at > pos) nodes.push(text.slice(pos, at));
    // Sliced from `text`, not from the lowercased copy, so the original casing
    // survives the highlight.
    nodes.push(
      <mark key={key++} style={MARK_STYLE}>
        {text.slice(at, at + needle.length)}
      </mark>
    );
    pos = at + needle.length;
    at = haystack.indexOf(needle, pos);
  }
  if (pos < text.length) nodes.push(text.slice(pos));
  return nodes;
}

const MARKERS: { open: string; render: (inner: ReactNode, key: number) => ReactNode }[] = [
  { open: "**", render: (inner, key) => <strong key={key}>{inner}</strong> },
  { open: "`", render: (inner, key) => (
      <code key={key} style={CODE_STYLE}>{inner}</code>
    ) },
];

export function renderInlineMarkdown(text: string, highlight?: string): ReactNode {
  // Fast path: no markers to parse, but the term still has to be marked.
  if (!text.includes("**") && !text.includes("`")) return highlightRuns(text, highlight);

  const nodes: ReactNode[] = [];
  let buffer = ""; // accumulates plain text between spans
  let i = 0;
  let key = 0;

  const flush = () => {
    if (buffer) {
      const run = highlightRuns(buffer, highlight);
      // A marked run is an array; wrapping it in a keyed Fragment keeps the outer
      // list's children unambiguous rather than nesting a bare array in it.
      nodes.push(typeof run === "string" ? run : <Fragment key={key++}>{run}</Fragment>);
      buffer = "";
    }
  };

  while (i < text.length) {
    // Which marker (if any) starts here? Check "**" before "`" so it wins.
    const marker = MARKERS.find((m) => text.startsWith(m.open, i));
    if (marker) {
      const close = text.indexOf(marker.open, i + marker.open.length);
      if (close !== -1) {
        const inner = text.slice(i + marker.open.length, close);
        flush();
        // Highlight inside the span too — a term the model wrapped in ** must
        // still show as a hit.
        nodes.push(marker.render(highlightRuns(inner, highlight), key++));
        i = close + marker.open.length;
        continue;
      }
      // No closing marker — treat this opener as literal text and move past it.
      buffer += marker.open;
      i += marker.open.length;
      continue;
    }
    buffer += text[i];
    i += 1;
  }
  flush();
  return nodes;
}
