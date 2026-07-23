"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import Image from "next/image";
import { SideNav } from "@computacenter-ro/style-guide/components";
import type { BaseNavItem, SideNavItem } from "@computacenter-ro/style-guide/components";
import { BellIcon, ClockCounterClockwiseIcon, MapTrifoldIcon, SignOutIcon } from "@phosphor-icons/react";
import ccLogoWhite from "@computacenter-ro/style-guide/logos/cc-logo-white.png";
import ccLogoWhiteMark from "@computacenter-ro/style-guide/logos/cc-logo-white-mark.png";
import { useAuth } from "@/lib/auth";

const COLLAPSE_STORAGE_KEY = "oil-sidenav-collapsed";

interface AppShellProps {
  children: React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, logout } = useAuth();
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
      label: "History",
      href: "/history",
      icon: <ClockCounterClockwiseIcon size={20} />,
      active: pathname === "/history",
    },
  ];

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
    </div>
  );
}
