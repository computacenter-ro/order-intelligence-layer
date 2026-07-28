interface EmptyStateProps {
  /** The headline — what is (not) here, e.g. "No active alerts". */
  title: string;
  /** Optional second line explaining why it's empty or what to do next. */
  hint?: string;
}

/**
 * A quiet, centered placeholder shown when a list has genuinely nothing to
 * show (loading finished, zero items). Kept deliberately plain — an empty
 * state should reassure, not shout.
 */
export function EmptyState({ title, hint }: EmptyStateProps) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: "6px",
        padding: "48px 16px",
        textAlign: "center",
      }}
    >
      <p style={{ margin: 0, fontSize: "16px", fontWeight: 600, color: "var(--cc-grey-one)" }}>
        {title}
      </p>
      {hint && (
        <p style={{ margin: 0, fontSize: "14px", color: "var(--cc-grey-three)", maxWidth: "420px" }}>
          {hint}
        </p>
      )}
    </div>
  );
}
