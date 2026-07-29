"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { Button, TextInput } from "@computacenter-ro/style-guide/components";
import ccLogoBlue from "@computacenter-ro/style-guide/logos/cc-logo-blue.png";
import {
  UnauthorizedError,
  entraLoginUrl,
  fetchAuthConfig,
  type AuthConfig,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

/** ?auth_error= reasons the backend's Entra callback can send us back with. */
const AUTH_ERROR_TEXT: Record<string, string> = {
  bad_state: "That sign-in attempt expired. Please try again.",
  access_denied: "Sign-in was cancelled or is not permitted for your account.",
  exchange_failed: "Microsoft sign-in failed. Please try again.",
  not_configured: "Microsoft sign-in is not configured on this server.",
};

/**
 * Full-screen login gate — a two-panel split: a light brand panel on the left
 * anchoring the Computacenter logo bottom-left, and the sign-in form on the
 * right. Shown by the auth gate whenever there is no valid session (fresh
 * visit, expired cookie, or after logout). On success the AuthProvider flips to
 * `authenticated` and the gate swaps in the app.
 *
 * **Entra ID is the primary path**: the Microsoft button leaves the SPA for the
 * backend's redirect endpoint, and the browser returns here already carrying the
 * session cookie. The username/password form below it is the escape hatch for
 * local dev and for an expired Entra client secret — the backend gates it behind
 * `PASSWORD_LOGIN_ENABLED`, and `GET /auth/config` is what tells us whether to
 * render it at all. The field is labelled "Email" but its value is sent as the
 * `username` (the hardcoded admin).
 */
export function LoginScreen() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);

  useEffect(() => {
    // A failed Entra round trip lands here as ?auth_error=<reason>. Read it after
    // mount, not during the initial render — reading the URL synchronously (e.g.
    // via a useState lazy initializer) would make the client's first paint diverge
    // from the server's, causing a hydration mismatch. Then strip the param so a
    // refresh doesn't resurrect a message we've already shown.
    const reason = new URLSearchParams(window.location.search).get("auth_error");
    if (reason) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setAuthError(AUTH_ERROR_TEXT[reason] ?? "Sign-in failed. Please try again.");
      window.history.replaceState({}, "", window.location.pathname);
    }
    let active = true;
    fetchAuthConfig()
      .then((cfg) => active && setConfig(cfg))
      .catch(() => {
        // Backend unreachable: assume password login, matching AuthProvider's
        // "fail toward the login screen" behaviour.
        if (active) setConfig({ entra_enabled: false, password_login: true });
      });
    return () => {
      active = false;
    };
  }, []);

  const message = error ?? authError;
  // Render the Microsoft button until config says otherwise, so the primary
  // action never waits on a round trip. The password block is the opposite: it
  // appears only once confirmed, so it never flashes where it is disabled.
  const showEntra = config === null || config.entra_enabled;
  const showPassword = config?.password_login === true;

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

          {showEntra && (
            <Button
              variant="primary"
              size="md"
              type="button"
              onClick={() => window.location.assign(entraLoginUrl())}
            >
              Sign In With Microsoft
            </Button>
          )}

          {showEntra && showPassword && (
            <div
              aria-hidden="true"
              style={{
                display: "flex",
                alignItems: "center",
                gap: "12px",
                margin: "24px 0",
                color: "var(--cc-grey-three)",
                fontSize: "14px",
                lineHeight: "18px",
              }}
            >
              <span
                style={{ flex: 1, height: "1px", background: "var(--cc-grey-five)" }}
              />
              or
              <span
                style={{ flex: 1, height: "1px", background: "var(--cc-grey-five)" }}
              />
            </div>
          )}

          {showPassword && (
            <>
              <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
                <TextInput
                  label="Email"
                  type="email"
                  value={email}
                  onChange={setEmail}
                  placeholder="Enter your email"
                  state={message ? "error" : "default"}
                />
                <TextInput
                  label="Password"
                  type="password"
                  value={password}
                  onChange={setPassword}
                  placeholder="Enter your password"
                  state={message ? "error" : "default"}
                  errorText={message ?? undefined}
                />
              </div>

              <div style={{ marginTop: "32px" }}>
                <Button
                  variant="secondary"
                  size="md"
                  type="submit"
                  loading={submitting}
                  disabled={submitting || !email || !password}
                >
                  Sign In
                </Button>
              </div>
            </>
          )}

          {/* With the password form hidden there is no field to hang the error
              on, so an Entra failure needs its own line. */}
          {!showPassword && message && (
            <p
              role="alert"
              style={{
                color: "var(--cc-united-red)",
                fontSize: "14px",
                lineHeight: "18px",
                margin: "16px 0 0",
              }}
            >
              {message}
            </p>
          )}

          {config !== null && !showEntra && !showPassword && (
            <p
              role="alert"
              style={{
                color: "var(--cc-grey-three)",
                fontSize: "14px",
                lineHeight: "18px",
                margin: 0,
              }}
            >
              No sign-in method is enabled on this server. Contact support.
            </p>
          )}
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
