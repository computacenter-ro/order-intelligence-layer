import type { ReactNode } from "react";
import { badgeColors } from "@computacenter-ro/style-guide/tokens";
import type { BadgeStatus } from "@/lib/types";

interface BadgeProps {
  status: BadgeStatus;
  children: ReactNode;
}

export function Badge({ status, children }: BadgeProps) {
  const tone = badgeColors[status];
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        borderRadius: "9999px",
        border: `1px solid ${tone.border}`,
        backgroundColor: tone.bg,
        color: tone.text,
        fontSize: "12px",
        fontWeight: 600,
        lineHeight: "16px",
        padding: "2px 9px",
      }}
    >
      {children}
    </span>
  );
}
