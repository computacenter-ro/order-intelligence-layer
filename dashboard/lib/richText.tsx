import type { ReactNode } from "react";

/**
 * Render LLM-authored text with minimal inline markdown: `**bold**` → <strong>.
 *
 * The AI service (explainer / journey summary) emits plain prose that sometimes
 * includes `**emphasis**`. Rendered as a raw string those asterisks show
 * literally. This does the ONE transform those outputs actually use — nothing
 * more — deliberately avoiding a full markdown dependency and its HTML/XSS
 * surface: the input is split on `**` pairs and emitted as plain React text
 * nodes plus <strong> wrappers, so no markup is ever interpreted.
 *
 * An unpaired trailing `**` (odd count) is left as literal text, so partial or
 * malformed model output degrades gracefully rather than bolding the rest.
 */
export function renderInlineMarkdown(text: string): ReactNode {
  if (!text.includes("**")) return text; // fast path — most lines have no markup

  const parts = text.split("**");
  // split on "**": even indices are outside bold, odd indices are inside.
  // With an unbalanced count the final segment is odd-indexed but has no closing
  // "**"; treat it as plain text (re-prepend the "**" that split removed).
  const balanced = parts.length % 2 === 1;

  return parts.map((part, i) => {
    if (i % 2 === 1) {
      if (!balanced && i === parts.length - 1) return `**${part}`; // dangling open
      return <strong key={i}>{part}</strong>;
    }
    return part;
  });
}
