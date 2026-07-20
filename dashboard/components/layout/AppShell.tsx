"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { SideNav } from "@computacenter-ro/style-guide/components";
import type { BaseNavItem, SideNavItem } from "@computacenter-ro/style-guide/components";
import { BellIcon, MapTrifoldIcon } from "@phosphor-icons/react";

const COLLAPSE_STORAGE_KEY = "oil-sidenav-collapsed";

interface AppShellProps {
  children: React.ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  const router = useRouter();
  const pathname = usePathname();
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
  ];

  const handleItemClick = (item: BaseNavItem) => {
    router.push(item.href);
  };

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      <SideNav
        logo={
          <span style={{ color: "var(--cc-cloud-white)", fontSize: "14px", fontWeight: 600 }}>
            IT Support Dashboard
          </span>
        }
        logoMark={<span style={{ color: "var(--cc-cloud-white)", fontSize: "18px", fontWeight: 700 }}>IT</span>}
        items={items}
        collapsed={collapsed}
        onCollapse={handleCollapse}
        onItemClick={handleItemClick}
        footer={
          <div style={{ padding: "12px 16px", fontSize: "12px", color: "var(--cc-grey-four)" }}>
            Sample data · backend not connected
          </div>
        }
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
