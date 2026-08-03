import { useEffect } from "react";
import { XIcon } from "@phosphor-icons/react";
import { AlertDetailBody } from "@/components/alerts/AlertDetailBody";
import { useChat } from "@/lib/chat";
import type { ProcessedAlert } from "@/lib/types";

/**
 * The right-side alert drawer opened from the feed and history.
 *
 * Only the chrome lives here — the overlay, the scrim, Escape, the close X. What
 * an alert actually shows is ``AlertDetailBody``, shared with the assistant
 * panel's citation detail view so the two can never drift.
 */

interface AlertDetailDrawerProps {
  alert: ProcessedAlert | null;
  onClose: () => void;
  /** Active search term, highlighted in the explanation and the raw log. */
  search?: string;
}

export function AlertDetailDrawer({ alert, onClose, search }: AlertDetailDrawerProps) {
  const { openChat } = useChat();

  useEffect(() => {
    if (!alert) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [alert, onClose]);

  if (!alert) return null;

  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(1, 23, 73, 0.5)",
          zIndex: 20,
        }}
      />
      <aside
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "440px",
          background: "var(--cc-cloud-white)",
          boxShadow: "var(--shadow-cc-xl, 0 32px 64px rgba(1,23,73,0.20))",
          zIndex: 21,
          overflowY: "auto",
          padding: "24px",
          boxSizing: "border-box",
        }}
      >
        <button
          onClick={onClose}
          aria-label="Close"
          style={{
            float: "right",
            background: "none",
            border: "none",
            color: "var(--cc-heritage-blue)",
            cursor: "pointer",
            padding: "4px",
          }}
        >
          <XIcon size={20} />
        </button>
        <AlertDetailBody
          alert={alert}
          search={search}
          // Closing BEFORE opening the chat is load-bearing: both overlays are
          // z-index 21 with their own Escape handler, so leaving this drawer open
          // would stack two dialogs fighting for the same key.
          onAsk={() => {
            onClose();
            openChat({ kind: "alert", id: alert.alert_id }, `alert ${alert.app_name}`);
          }}
          onNavigate={onClose}
        />
      </aside>
    </>
  );
}
