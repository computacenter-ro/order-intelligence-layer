"use client";

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { ChatCircleDotsIcon } from "@phosphor-icons/react";
import { Button } from "@computacenter-ro/style-guide/components";
import { useChat } from "@/lib/chat";

/**
 * Routes whose page already offers its own scoped way in ("Ask About This
 * Journey" / "…Incident"), placed high in the layout where the record is. Two
 * assistant triggers on one screen is the ambiguity, not the redundancy: the
 * corner pill opens an UNSCOPED conversation, so it reads as "ask about this
 * page" while doing the opposite. Only the DETAIL routes qualify — the
 * ``/journeys`` and ``/incidents`` lists have no scoped button, so they keep it.
 */
const SCOPED_ENTRY_ROUTES = ["/journeys/", "/incidents/"];

/** A detail route = one of the prefixes above followed by a real id segment. */
function hasScopedEntryPoint(pathname: string): boolean {
  return SCOPED_ENTRY_ROUTES.some(
    (prefix) => pathname.startsWith(prefix) && pathname.length > prefix.length
  );
}

/**
 * The always-available way into the assistant: a floating pill in the
 * bottom-right corner, on every page under ``AppShell`` that does not already
 * have a scoped entry point of its own.
 *
 * It replaces the side-nav "Assistant" item. Keeping the icon (and the label —
 * an icon-only button would cost the discoverability the nav item had) makes the
 * swap read as a move rather than a new feature.
 *
 * ``zIndex`` 15 sits in the gap of the app's de-facto scale (10 = sticky banners
 * and header menus, 20 = scrims and portalled popovers, 21 = drawers): above the
 * banners it must clear, below the chat's own scrim so it cannot be clicked
 * while the modal panel owns the screen.
 */
export function ChatLauncher() {
  const { open, openChat } = useChat();
  const pathname = usePathname();
  const wrapperRef = useRef<HTMLDivElement>(null);
  /** Set only when the panel was opened from HERE, so closing a panel opened by
   *  a page's scoped "Ask about this" button never yanks focus into the corner. */
  const openedFromHere = useRef(false);

  // Return focus on close — the trigger is now a stable element, so the global
  // case is simply "focus the thing that was activated", per WCAG 2.4.3. The
  // style-guide Button takes no ref, hence the query on the positioning wrapper.
  useEffect(() => {
    if (open || !openedFromHere.current) return;
    openedFromHere.current = false;
    wrapperRef.current?.querySelector("button")?.focus();
  }, [open]);

  // Hidden while the panel is open: it would otherwise sit dimmed under the
  // scrim — visible and inert. Explicit beats relying on the stacking order.
  // Both bail-outs live below the hooks, which therefore always run.
  if (open || hasScopedEntryPoint(pathname)) return null;

  return (
    // The wrapper does the positioning; the Button does the appearance, so none
    // of its six states are reimplemented here (it accepts no style/className,
    // so the pill radius and shadow live in globals.css).
    <div
      ref={wrapperRef}
      className="oil-chat-launcher"
      style={{ position: "fixed", bottom: "24px", right: "24px", zIndex: 15 }}
    >
      <Button
        variant="primary"
        leftIcon={<ChatCircleDotsIcon size={20} />}
        onClick={() => {
          openedFromHere.current = true;
          openChat(null);
        }}
      >
        Assistant
      </Button>
    </div>
  );
}
