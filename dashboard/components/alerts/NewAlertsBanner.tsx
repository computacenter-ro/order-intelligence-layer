import { badgeColors } from "@computacenter-ro/style-guide/tokens";

interface NewAlertsBannerProps {
  count: number;
  onReveal: () => void;
}

export function NewAlertsBanner({ count, onReveal }: NewAlertsBannerProps) {
  if (count === 0) return null;

  const tone = badgeColors.pending;
  const label = count === 1 ? "1 new alert" : `${count} new alerts`;

  return (
    <button
      type="button"
      className="oil-new-alerts-banner"
      onClick={onReveal}
      style={{
        display: "block",
        width: "100%",
        boxSizing: "border-box",
        position: "sticky",
        top: 0,
        zIndex: 10,
        marginBottom: "12px",
        padding: "8px 16px",
        borderRadius: "9999px",
        border: `1px solid ${tone.border}`,
        backgroundColor: tone.bg,
        color: tone.text,
        fontSize: "13px",
        fontWeight: 600,
        textAlign: "center",
        cursor: "pointer",
      }}
    >
      {label}
    </button>
  );
}
