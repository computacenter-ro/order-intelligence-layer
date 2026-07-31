"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Image from "next/image";
import { SideNav } from "@computacenter-ro/style-guide/components";
import type { BaseNavItem, SideNavItem } from "@computacenter-ro/style-guide/components";
import {
  BellIcon,
  ChartBarIcon,
  ClockCounterClockwiseIcon,
  GaugeIcon,
  MapTrifoldIcon,
  SignOutIcon,
  TreeStructureIcon,
  WarningIcon,
} from "@phosphor-icons/react";
// One mark for both nav states — the expanded header pairs it with the app
// name, so the wordmark logo it replaced was redundant (and set the brand twice).
import ccLogoWhiteMark from "@/assets/cc-logo-white-mark.png";
import { ChatLauncher } from "@/components/chat/ChatLauncher";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { useAuth } from "@/lib/auth";
import { ChatProvider, useChat } from "@/lib/chat";
import { displayName } from "@/lib/format";

const COLLAPSE_STORAGE_KEY = "oil-sidenav-collapsed";

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
  const { open: chatOpen, scope, scopeLabel, closeChat } = useChat();
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
      label: "Architecture",
      href: "/architecture",
      icon: <TreeStructureIcon size={20} />,
      active: pathname === "/architecture",
    },
  ];

  // Every nav item is now a real route — the assistant moved out to
  // ChatLauncher, so there is no sentinel href left to intercept.
  const handleItemClick = (item: BaseNavItem) => {
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
      // The full identity stays here and in the title: the row shows a name, but
      // "which account am I signed in as" must still be answerable.
      aria-label={`Sign out${user ? ` (${user.username})` : ""}`}
      title={user?.username}
    >
      <SignOutIcon size={20} />
      <span>Sign Out{user ? ` · ${displayName(user.username)}` : ""}</span>
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
    // The collapsed class is the hook the side-nav header rules in
    // app/globals.css need: the caret stacks under the logo only on the narrow
    // rail, and there is no other way to select the nav state from CSS (the
    // style guide sets the width inline). Put on the existing root rather than a
    // new wrapper, so the flex row the nav and <main> live in is untouched.
    <div
      className={collapsed ? "oil-nav-collapsed" : undefined}
      style={{ display: "flex", height: "100vh", overflow: "hidden" }}
    >
      <SideNav
        logo={
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            {/* Same mark AND same 32x24 as the collapsed rail, so the logo does
                not change size when the panel toggles. 40px was the ceiling (the
                collapsed rail is 64px wide with 12px padding either side); 32
                sits under it deliberately, so the mark reads as a mark rather
                than competing with the app name beside it. */}
            <Image src={ccLogoWhiteMark} alt="Computacenter" height={24} width={32} />
            <span style={{ color: "var(--cc-cloud-white)", fontSize: "14px", fontWeight: 600 }}>
              IT Support Dashboard
            </span>
          </div>
        }
        // A fixed 40px track, not `flex: 1`. The collapsed header is 64px with
        // 12px padding either side, so 40px of content width — but while the
        // caret shared this row it took 24px, leaving a flex child just 16px,
        // and the global `img { max-width: 100% }` reset then shrank the image
        // to fit. That is why asking for 40x30 once silently rendered 16x11.8.
        // An explicit width is immune to that, and centres on the rail's axis
        // (x=32) alongside the 20px item icons.
        //
        // While COLLAPSED the caret sits on its own row below this one — see the
        // side-nav header rules in app/globals.css, which also zero the negative
        // margin this wrapper used to need to overlap it. Expanded, the caret
        // stays on the header row at its right end, which is the vendored
        // component's own `space-between` default.
        //
        // Done here rather than in the shared SideNav, which is vendored from
        // the style guide and used by other apps.
        logoMark={
          <span
            style={{
              flex: "0 0 auto",
              width: "40px",
              display: "flex",
              justifyContent: "center",
              alignItems: "center",
            }}
          >
            {/* 32x24 holds the same 1.333 the previous 40x30 did against the
                asset's 1033x765 (1.3503) — the brand rules forbid stretching the
                logo, so width and height move together and the fidelity is
                unchanged, only the scale. Same size as the expanded header's
                mark. The track above stays 40px: it is what keeps the global
                `img { max-width: 100% }` reset from shrinking the image, and a
                32px mark still centres on the rail's axis inside it. */}
            <Image src={ccLogoWhiteMark} alt="Computacenter" height={24} width={32} />
          </span>
        }
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
      {/* The floating trigger for an unscoped conversation, on every page. It
          hides itself while the panel is open, so the two are never both here. */}
      <ChatLauncher />
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
