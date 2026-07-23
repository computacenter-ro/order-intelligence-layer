"use client";

import { AppShell } from "@/components/layout/AppShell";
import { LoginScreen } from "@/components/auth/LoginScreen";
import { useAuth } from "@/lib/auth";

/**
 * Decides what the whole app renders based on session status:
 *   loading       -> nothing (brief flash while /auth/me resolves)
 *   anonymous     -> full-screen login (no app chrome)
 *   authenticated -> the normal AppShell + page content
 *
 * Sits inside AuthProvider, outside the page content, so protecting the app is
 * a single decision in one place rather than a check on every page.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();

  if (status === "loading") {
    return (
      <div
        style={{
          minHeight: "100vh",
          background: "var(--cc-foundation-blue)",
        }}
      />
    );
  }

  if (status === "anonymous") {
    return <LoginScreen />;
  }

  return <AppShell>{children}</AppShell>;
}
