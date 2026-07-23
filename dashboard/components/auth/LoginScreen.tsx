"use client";

import Image from "next/image";
import { useState } from "react";
import { Button, TextInput } from "@computacenter-ro/style-guide/components";
import ccLogoBlue from "@computacenter-ro/style-guide/logos/cc-logo-blue.png";
import { UnauthorizedError } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Full-screen login gate — a two-panel split: a light brand panel on the left
 * anchoring the Computacenter logo bottom-left, and the sign-in form on the
 * right. Shown by the auth gate whenever there is no valid session (fresh
 * visit, expired cookie, or after logout). On success the AuthProvider flips to
 * `authenticated` and the gate swaps in the app.
 *
 * The field is labelled "Email" to match the product design, but its value is
 * sent to the backend as the `username` (Phase 1 accepts `admin`). Password is
 * shown because magic-link isn't wired yet; when it is, this becomes an
 * email-only step.
 */
export function LoginScreen() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      // No redirect needed: the gate re-renders the app once status flips.
    } catch (err) {
      setError(
        err instanceof UnauthorizedError
          ? "Incorrect email or password."
          : "Could not reach the server. Try again."
      );
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "flex",
        background: "var(--cc-cloud-white)",
      }}
    >
      {/* Left brand panel — light surface with the logo centered.
          Hidden on narrow screens so the form takes the full width. */}
      <aside
        className="login-brand-panel"
        style={{
          flex: "0 0 40%",
          background: "var(--cc-grey-six)",
          overflow: "hidden",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <Image
          src={ccLogoBlue}
          alt="Computacenter"
          height={132}
          width={258}
          priority
        />
      </aside>

      {/* Right panel — the sign-in form, left-aligned, no card. */}
      <main
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          padding: "48px",
        }}
      >
        <form
          onSubmit={handleSubmit}
          noValidate
          style={{ width: "100%", maxWidth: "440px" }}
        >
          <h1
            style={{
              fontSize: "32px",
              lineHeight: "44px",
              fontWeight: 700,
              color: "var(--cc-heritage-blue)",
              margin: 0,
            }}
          >
            Welcome to the IT Support Dashboard
          </h1>
          <p
            style={{
              fontSize: "16px",
              lineHeight: "24px",
              color: "var(--cc-grey-three)",
              margin: "12px 0 32px",
            }}
          >
            Real-time order-journey tracking and AI-explained alerts for the
            Order Intelligence Layer.
          </p>

          <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <TextInput
              label="Email"
              type="email"
              value={email}
              onChange={setEmail}
              placeholder="Enter your email"
              state={error ? "error" : "default"}
            />
            <TextInput
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder="Enter your password"
              state={error ? "error" : "default"}
              errorText={error ?? undefined}
            />
          </div>

          <div style={{ marginTop: "32px" }}>
            <Button
              variant="primary"
              size="md"
              type="submit"
              loading={submitting}
              disabled={submitting || !email || !password}
            >
              Sign In
            </Button>
          </div>
        </form>
      </main>

      <style>{`
        @media (max-width: 768px) {
          .login-brand-panel { display: none; }
        }
      `}</style>
    </div>
  );
}
