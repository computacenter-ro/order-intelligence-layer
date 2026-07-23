"use client";

import { useEffect, useRef, useState } from "react";
import { CheckCircleIcon, DotsThreeVerticalIcon } from "@phosphor-icons/react";

interface AlertActionsMenuProps {
  isResolved: boolean;
  onResolve: () => void;
}

export function AlertActionsMenu({ isResolved, onResolve }: AlertActionsMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onOutsideClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onOutsideClick);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onOutsideClick);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  if (isResolved) {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: "4px",
          fontSize: "12px",
          fontWeight: 600,
          color: "var(--cc-circuit-green)",
        }}
      >
        <CheckCircleIcon size={16} weight="regular" />
        Resolved
      </span>
    );
  }

  return (
    <div ref={rootRef} style={{ position: "relative" }}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Alert actions"
        onMouseDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          width: "28px",
          height: "28px",
          border: "none",
          background: "transparent",
          borderRadius: "8px",
          color: "var(--cc-heritage-blue)",
          cursor: "pointer",
          transition: "background-color 120ms ease",
        }}
        onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = "var(--cc-grey-six)")}
        onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = "transparent")}
      >
        <DotsThreeVerticalIcon size={20} weight="regular" />
      </button>
      {open && (
        <div
          role="menu"
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            position: "absolute",
            right: 0,
            top: "32px",
            zIndex: 10,
            minWidth: "160px",
            background: "var(--cc-cloud-white)",
            border: "1px solid var(--cc-grey-six)",
            borderRadius: "8px",
            boxShadow: "0 8px 24px rgba(1,23,73,0.16)",
            padding: "4px",
          }}
        >
          <button
            type="button"
            role="menuitem"
            onClick={(e) => {
              e.stopPropagation();
              setOpen(false);
              onResolve();
            }}
            style={{
              display: "block",
              width: "100%",
              textAlign: "left",
              padding: "8px 12px",
              fontSize: "14px",
              fontWeight: 500,
              color: "var(--cc-grey-one)",
              background: "transparent",
              border: "none",
              borderRadius: "4px",
              cursor: "pointer",
              transition: "background-color 120ms ease",
            }}
            onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = "var(--cc-grey-six)")}
            onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = "transparent")}
          >
            Mark Resolved
          </button>
        </div>
      )}
    </div>
  );
}
