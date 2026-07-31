"use client";

import { useEffect, useRef, useState } from "react";
import { MagnifyingGlassIcon, XIcon } from "@phosphor-icons/react";
import { radii, semanticSpacing } from "@computacenter-ro/style-guide/tokens";

// Long enough that ordinary typing produces one request instead of one per
// keystroke, short enough that the list still feels like it reacts to typing.
const DEBOUNCE_MS = 300;

interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  /** What the box searches, for screen readers. Defaults to the alert wording. */
  ariaLabel?: string;
  placeholder?: string;
}

/**
 * Debounced search box for the filter bar.
 *
 * Keystrokes land in local state so the caret never lags, and `onChange` fires
 * ~300ms after typing stops — each call re-fetches the list AND the facet counts,
 * so per-keystroke commits would be two requests per character.
 *
 * The subtle part is staying in sync with the outside world without fighting it.
 * `value` can change externally (Reset Filters, the clear button), so the input
 * must follow it; but `value` ALSO changes as an echo of this component's own
 * debounced emission, and blindly resyncing on that echo overwrites whatever the
 * user typed during the round trip — type "abc" fast enough and you get "ab"
 * back. So the last emitted value is remembered and echoes of it are ignored;
 * only a genuinely external change resyncs the field.
 *
 * Only the two user-visible strings are parameterised (defaulting to the alert
 * wording, so every existing call site is untouched). Everything else here — the
 * debounce, the echo suppression, the immediate clear, Escape-to-clear — is
 * already generic, which is why the journeys list reuses this rather than forking
 * a near-identical copy that would drift on the next fix.
 */
export function SearchInput({
  value,
  onChange,
  ariaLabel = "Search alerts by message or explanation",
  placeholder = "Search alerts…",
}: SearchInputProps) {
  const [text, setText] = useState(value);
  // onChange is an inline arrow at the call site, so its identity changes every
  // render. Held in a ref so the debounce effect can depend on `text` alone
  // instead of restarting its timer whenever the parent re-renders.
  const onChangeRef = useRef(onChange);
  const lastEmittedRef = useRef(value);

  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  useEffect(() => {
    // Already committed (including right after an emission) — nothing to send.
    if (text === value) return;
    const timer = setTimeout(() => {
      lastEmittedRef.current = text;
      onChangeRef.current(text);
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [text, value]);

  useEffect(() => {
    // Ignore the echo of our own emission (see the note above); resync only for
    // a real external change such as Reset Filters.
    if (value === lastEmittedRef.current) return;
    lastEmittedRef.current = value;
    // Mirroring an external prop into local state is exactly what this effect is
    // for; there is nothing to derive it from during render.
    setText(value);
  }, [value]);

  const clear = () => {
    // Immediate, not debounced: the user asked for "empty now", and waiting
    // 300ms to act on a button press reads as a broken control.
    lastEmittedRef.current = "";
    setText("");
    onChange("");
  };

  return (
    // The wrapper carries the border so the icon and clear button sit inside one
    // control; the input itself is borderless and transparent.
    <div
      className="oil-filter-select"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: semanticSpacing.sm,
        height: "32px",
        width: "clamp(200px, 100%, 320px)",
        padding: `0 ${semanticSpacing.md}`,
        backgroundColor: "var(--cc-cloud-white)",
        border: "1px solid var(--cc-grey-four)",
        borderRadius: radii.md,
      }}
    >
      <MagnifyingGlassIcon
        size={16}
        aria-hidden="true"
        // Carries the "this field searches" meaning in place of a visible label —
        // the whole bar is name-in-box, so a label above just this one control
        // would break the row.
        style={{ color: "var(--cc-grey-three)", flexShrink: 0 }}
      />
      <input
        type="search"
        value={text}
        onChange={(e) => setText(e.target.value)}
        // Escape clears, matching the popovers' Escape-to-dismiss.
        onKeyDown={(e) => {
          if (e.key === "Escape" && text !== "") {
            e.preventDefault();
            clear();
          }
        }}
        aria-label={ariaLabel}
        placeholder={placeholder}
        style={{
          flex: 1,
          minWidth: 0,
          border: "none",
          outline: "none",
          background: "transparent",
          color: "var(--cc-grey-one)",
          fontSize: "14px",
          fontFamily: "inherit",
        }}
      />
      {text !== "" && (
        <button
          type="button"
          onClick={clear}
          aria-label="Clear search"
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
            padding: 0,
            border: "none",
            background: "transparent",
            color: "var(--cc-grey-three)",
            cursor: "pointer",
          }}
        >
          <XIcon size={14} />
        </button>
      )}
    </div>
  );
}
