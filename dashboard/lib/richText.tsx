import type { CSSProperties, ReactNode } from "react";

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
 */

const CODE_STYLE: CSSProperties = {
  fontFamily: "ui-monospace, Menlo, monospace",
  fontSize: "0.9em",
  background: "var(--cc-grey-six)",
  color: "var(--cc-grey-one)",
  padding: "1px 5px",
  borderRadius: "4px",
};

const MARKERS: { open: string; render: (inner: string, key: number) => ReactNode }[] = [
  { open: "**", render: (inner, key) => <strong key={key}>{inner}</strong> },
  { open: "`", render: (inner, key) => (
      <code key={key} style={CODE_STYLE}>{inner}</code>
    ) },
];

export function renderInlineMarkdown(text: string): ReactNode {
  if (!text.includes("**") && !text.includes("`")) return text; // fast path

  const nodes: ReactNode[] = [];
  let buffer = ""; // accumulates plain text between spans
  let i = 0;
  let key = 0;

  const flush = () => {
    if (buffer) {
      nodes.push(buffer);
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
        nodes.push(marker.render(inner, key++));
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
