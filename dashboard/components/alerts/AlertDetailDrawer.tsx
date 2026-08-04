import { useEffect } from "react";
import { XIcon } from "@phosphor-icons/react";
import { AlertActionsMenu } from "@/components/alerts/AlertActionsMenu";
import { AlertDetailBody } from "@/components/alerts/AlertDetailBody";
import { useChat } from "@/lib/chat";
import type { ProcessedAlert } from "@/lib/types";

/**
 * The right-side alert drawer opened from the feed and history.
 *
 * Only the chrome lives here — the overlay, the scrim, Escape, the close X, and
 * the actions menu. What an alert actually shows is ``AlertDetailBody``, shared
 * with the assistant panel's citation detail view so the two can never drift.
 *
 * **Resolve belongs to this chrome, NOT to the body.** ``AlertDetailBody`` is
 * rendered by ``ChatPanel`` too, over a conversation, where there is no resolve
 * concept and no handler to call — putting the menu in the body would push a
 * mutating action into a surface that never asked for one. The drawer is also the
 * only surface a Teams `/?alert=<id>` deep link lands on, and before this the
 * action lived solely on the feed *card*: an alert reached by link may have no card
 * behind it (filtered out, or older than the first page), which made the drawer
 * read-only and the notification informative but not actionable.
 *
 * It reuses ``AlertActionsMenu`` rather than adding a button, so "Mark Resolved"
 * keeps one label and one behaviour everywhere — same reasoning as the comment in
 * ``app/incidents/[incidentId]/page.tsx``.
 */

interface AlertDetailDrawerProps {
  alert: ProcessedAlert | null;
  onClose: () => void;
  /** Active search term, highlighted in the explanation and the raw log. */
  search?: string;
  /**
   * Resolve this alert. Optional: a host that offers no resolve action simply
   * omits it and the menu is not rendered.
   *
   * The host is expected to close the drawer as part of resolving — on the feed a
   * resolved alert leaves the list, so leaving it open would show a row that is no
   * longer there.
   */
  onResolve?: (alert: ProcessedAlert) => void;
  /**
   * Why there is no alert to show, when a host tried to load one and failed.
   *
   * Only the `/?alert=<id>` deep link can produce this: a Teams card outlives the
   * alert it points at, so a 404 is expected rather than exceptional. The drawer
   * opens on the message so the reader learns the link is stale — an empty drawer
   * would read as a broken dashboard instead.
   */
  error?: string | null;
}

export function AlertDetailDrawer({
  alert,
  onClose,
  search,
  onResolve,
  error,
}: AlertDetailDrawerProps) {
  const { openChat } = useChat();

  const shown = Boolean(alert || error);

  useEffect(() => {
    if (!shown) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [shown, onClose]);

  if (!shown) return null;

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
        {/* Both controls float right, so they sit on one row in the top-right
            corner with Close outermost — the position it has always had, so the
            new menu does not move it. `AlertDetailBody` clears the float itself. */}
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
        {alert && (onResolve || alert.is_resolved) && (
          <div style={{ float: "right", marginRight: "4px" }}>
            {/* `AlertActionsMenu` already branches on isResolved — a resolved alert
                renders the "Resolved" chip and NO menu item — so neither this
                component nor its hosts branch on it, same as the incidents detail
                page. That is also why an already-resolved alert is shown even
                without an `onResolve`: on /history the chip is information, not an
                action, and the handler below is unreachable in that case (there is
                no menu item to click). */}
            <AlertActionsMenu
              isResolved={alert.is_resolved}
              onResolve={() => onResolve?.(alert)}
            />
          </div>
        )}
        {alert ? (
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
        ) : (
          <p
            style={{
              clear: "both",
              margin: 0,
              paddingTop: "8px",
              fontSize: "16px",
              lineHeight: "22px",
              color: "var(--cc-grey-three)",
            }}
          >
            {error}
          </p>
        )}
      </aside>
    </>
  );
}
