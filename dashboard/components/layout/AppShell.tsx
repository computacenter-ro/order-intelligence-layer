"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Image from "next/image";
import { SideNav } from "@computacenter-ro/style-guide/components";
import type { BaseNavItem, SideNavItem } from "@computacenter-ro/style-guide/components";
import {
  BellIcon,
  ChartBarIcon,
  ChatCircleDotsIcon,
  ClockCounterClockwiseIcon,
  GaugeIcon,
  MapTrifoldIcon,
  SignOutIcon,
  WarningIcon,
} from "@phosphor-icons/react";
import ccLogoWhite from "@computacenter-ro/style-guide/logos/cc-logo-white.png";
import ccLogoWhiteMark from "@computacenter-ro/style-guide/logos/cc-logo-white-mark.png";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { useAuth } from "@/lib/auth";
import { ChatProvider, useChat } from "@/lib/chat";

const COLLAPSE_STORAGE_KEY = "oil-sidenav-collapsed";

// The assistant is a drawer, not a page, but SideNavItem requires an href. This
// sentinel is intercepted in onItemClick and never routed to — so the item sits
// with the others (icons are required on every side-nav item) without adding a
// route that would 404.
const ASSISTANT_HREF = "#assistant";

interface AppShellProps {
  children: React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  return (
    <ChatProvider>
      <AppShellInner>{children}</AppShellInner>
    </ChatProvider>
  );
}

function AppShellInner({ children }: AppShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, logout } = useAuth();
  const { open: chatOpen, scope, scopeLabel, openChat, closeChat } = useChat();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    // Sync from localStorage after mount, not during the initial render —
    // reading it synchronously (e.g. via a useState lazy initializer) would
    // make the client's first paint diverge from the server's (which has no
    // localStorage), causing a hydration mismatch. Effects run after
    // hydration completes, so this update is safe.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (window.localStorage.getItem(COLLAPSE_STORAGE_KEY) === "true") setCollapsed(true);
  }, []);

  const handleCollapse = (next: boolean) => {
    setCollapsed(next);
    window.localStorage.setItem(COLLAPSE_STORAGE_KEY, String(next));
  };

  const items: SideNavItem[] = [
    { label: "Alert Feed", href: "/", icon: <BellIcon size={20} />, active: pathname === "/" },
    {
      label: "Journeys",
      href: "/journeys",
      icon: <MapTrifoldIcon size={20} />,
      active: pathname === "/journeys" || pathname.startsWith("/journeys/"),
    },
    {
      label: "Incidents",
      href: "/incidents",
      icon: <WarningIcon size={20} />,
      active: pathname === "/incidents" || pathname.startsWith("/incidents/"),
    },
    {
      label: "History",
      href: "/history",
      icon: <ClockCounterClockwiseIcon size={20} />,
      active: pathname === "/history",
    },
    {
      label: "Insights",
      href: "/insights",
      icon: <ChartBarIcon size={20} />,
      active: pathname === "/insights",
    },
    {
      label: "AI Performance",
      href: "/ai-performance",
      icon: <GaugeIcon size={20} />,
      active: pathname === "/ai-performance",
    },
    {
      label: "Assistant",
      href: ASSISTANT_HREF,
      icon: <ChatCircleDotsIcon size={20} />,
      // Never "active": it is an overlay, not a location. Marking it active
      // would break the one-active-item-at-a-time rule against the real page
      // underneath it.
      active: false,
    },
  ];

  const handleItemClick = (item: BaseNavItem) => {
    // The assistant opens the drawer over the current page instead of navigating.
    if (item.href === ASSISTANT_HREF) {
      openChat(null);
      return;
    }
    router.push(item.href);
  };

  // Bottom user + logout row. Logout is destructive-ish (ends the session) but
  // not data-destructive, so it uses the standard interactive treatment; the
  // sign-out icon carries the meaning per the icon rules (never decorative).
  const footer = (
    <button
      type="button"
      onClick={() => {
        void logout();
      }}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "8px",
        width: "100%",
        padding: "8px 12px",
        background: "transparent",
        border: "none",
        borderRadius: "8px",
        color: "var(--cc-cloud-white)",
        fontSize: "14px",
        fontWeight: 500,
        cursor: "pointer",
      }}
      aria-label={`Sign out${user ? ` (${user.username})` : ""}`}
    >
      <SignOutIcon size={20} />
      <span>Sign Out{user ? ` · ${user.username}` : ""}</span>
    </button>
  );

  const collapsedFooter = (
    <button
      type="button"
      onClick={() => {
        void logout();
      }}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        width: "100%",
        padding: "12px",
        background: "transparent",
        border: "none",
        color: "var(--cc-cloud-white)",
        cursor: "pointer",
      }}
      aria-label="Sign out"
    >
      <SignOutIcon size={20} />
    </button>
  );

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      <SideNav
        logo={
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <Image src={ccLogoWhite} alt="Computacenter" height={22} width={43} />
            <span style={{ color: "var(--cc-cloud-white)", fontSize: "14px", fontWeight: 600 }}>
              IT Support Dashboard
            </span>
          </div>
        }
        logoMark={<Image src={ccLogoWhiteMark} alt="Computacenter" height={24} width={32} />}
        items={items}
        collapsed={collapsed}
        onCollapse={handleCollapse}
        onItemClick={handleItemClick}
        footer={footer}
        collapsedFooter={collapsedFooter}
      />
      <main
        style={{
          flex: 1,
          minWidth: 0,
          background: "var(--cc-cloud-white)",
          padding: "48px",
          overflowY: "auto",
        }}
      >
        {children}
      </main>
      {/* Mounted once at the shell so every entry point drives the same drawer.
          The `key` makes a change of scope remount it, discarding the previous
          conversation — carrying turns about a different record over would
          mislead, since every request from this panel is scoped to `context`.
          Doing it with a key rather than a reset-in-effect keeps the state
          derivation declarative (and satisfies react-hooks/set-state-in-effect). */}
      <ChatPanel
        key={scope ? `${scope.kind}:${scope.id}` : "global"}
        open={chatOpen}
        onClose={closeChat}
        context={scope}
        contextLabel={scopeLabel}
      />
    </div>
  );
}
