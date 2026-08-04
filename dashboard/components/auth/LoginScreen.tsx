"use client";

import Image from "next/image";
import { useEffect, useState } from "react";
import { ArrowLeft } from "@phosphor-icons/react";
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
 * Two steps: **choose a method, then use it.** Step 1 offers the enabled sign-in
 * methods as equal-width buttons; step 2 is the chosen method's own screen, with a
 * Back link. No form fields are visible until the user asks for them, which keeps
 * the first screen to a single decision.
 *
 * **Entra ID is the primary path**: the Microsoft button leaves the SPA for the
 * backend's redirect endpoint, and the browser returns here already carrying the
 * session cookie. The username/password form is the escape hatch for local dev
 * and for an expired Entra client secret — the backend gates it behind
 * `PASSWORD_LOGIN_ENABLED`, and `GET /auth/config` is what tells us whether to
 * render it at all. The field is labelled "Email" but its value is sent as the
 * `username` (the hardcoded admin).
 *
 * With only ONE method enabled the chooser is skipped entirely — a menu of one is
 * a wasted click — so the screen collapses back to exactly what it was before.
 */
export function LoginScreen() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [step, setStep] = useState<"choose" | "password">("choose");

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
  const bothAvailable = showEntra && showPassword;
  // Two steps: choose a method, then use it. The chooser only earns its step when
  // there are actually two methods — with one enabled, showing a menu of one
  // would be a pointless click, so we land straight on it.
  const onPasswordStep = showPassword && (!bothAvailable || step === "password");
  const onChooserStep = !onPasswordStep && (showEntra || showPassword);

  // Extracted only because two branches of the chooser now render this action (a
  // quiet link when Entra is also available, the primary button when it is not).
  // Behaviour is byte-for-byte what the inline handler did — one definition is what
  // guarantees the two spellings cannot drift apart.
  const goToPasswordStep = () => {
    // Drop any Entra redirect error: it says nothing about the password form the
    // user is about to see.
    setAuthError(null);
    setStep("password");
  };

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
      {/* Left brand panel — light surface with the logo above an ambient graphic
          of an order flowing through the pipeline. Hidden below 768px by the media
          query at the bottom of this file, which takes the graphic with it: one
          breakpoint governs the whole panel. */}
      <aside
        className="login-brand-panel"
        style={{
          flex: "0 0 40%",
          background: "var(--cc-grey-six)",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: "40px",
        }}
      >
        {/* Half the previous rendered size (was 258x132), keeping the asset's
            aspect ratio so it is scaled, never redrawn: 132/258 x 150 ≈ 77. */}
        <Image
          src={ccLogoBlue}
          alt="Computacenter"
          height={77}
          width={150}
          priority
        />

        {/* An order travelling through the pipeline: a track, four stage nodes and
            two dots crossing it, the second offset half a cycle so the track is
            never empty. BOTH dots are Heritage Blue — see the note on
            `.login-pipeline-dot--trailing` for why neither is red.

            NO LABELS on the nodes, deliberately. This page is reachable
            unauthenticated, and naming internal pipeline stages here would be
            needless disclosure; the shape alone says "something flows through
            stages". aria-hidden for the same reason it has no text: it is ambient
            brand illustration with nothing for a screen reader to announce.

            CSS-only (no JS, no dependency, nothing for hydration to mismatch), and
            fully stilled under prefers-reduced-motion — see the style block below. */}
        <div className="login-pipeline" aria-hidden="true">
          <span className="login-pipeline-track" />
          <span className="login-pipeline-node" />
          <span className="login-pipeline-node" />
          <span className="login-pipeline-node" />
          <span className="login-pipeline-node" />
          <span className="login-pipeline-dot login-pipeline-dot--ok" />
          <span className="login-pipeline-dot login-pipeline-dot--trailing" />
        </div>
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
          {/* Just the product name — a sign-in screen does not need to say
              welcome, and at 32px inside a 440px column the greeting wrapped and
              left "Dashboard" orphaned on its own line. `textWrap: balance` keeps
              any future rewording splitting evenly instead of orphaning a word. */}
          <h1
            style={{
              fontSize: "40px",
              lineHeight: "48px",
              fontWeight: 700,
              color: "var(--cc-heritage-blue)",
              margin: 0,
              textWrap: "balance",
            }}
          >
            IT Support Dashboard
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

          {/* Step 1 — pick a method. Grid rather than flex: Button takes no
              style/className prop, and grid items blockify, so the primary
              stretches to the full width.

              The two methods are deliberately NOT peers any more. They used to be
              equal-width buttons side by side, which made the password path look as
              important as SSO when it is the escape hatch — and is meant to be
              PASSWORD_LOGIN_ENABLED=false in any deployment. Entra keeps the single
              full-width primary; the password option demotes to a quiet centred
              text action. */}
          {onChooserStep && (
            <div style={{ display: "grid", gap: "16px" }}>
              {showEntra && (
                <Button
                  variant="primary"
                  size="md"
                  type="button"
                  onClick={() => window.location.assign(entraLoginUrl())}
                >
                  Sign in with Microsoft
                </Button>
              )}
              {showPassword &&
                (showEntra ? (
                  // Ghost is the palette's quiet text action (transparent, no
                  // border, Heritage Blue text), so this reads as a link while
                  // still coming from the shared Button — which is what keeps its
                  // hover/pressed/focus/disabled states identical to every other
                  // action in the app. Wrapped in a centring flex so it shrinks to
                  // its label instead of blockifying to the grid's full width.
                  <div style={{ display: "flex", justifyContent: "center" }}>
                    <Button
                      variant="ghost"
                      size="compact"
                      type="button"
                      onClick={goToPasswordStep}
                    >
                      Sign in with your account
                    </Button>
                  </div>
                ) : (
                  // Password is the ONLY method: it is the primary action, so it
                  // gets the primary full-width button rather than being left as a
                  // small link on an otherwise empty screen. (Unreachable today —
                  // the step logic skips the chooser when only one method is
                  // enabled — but the chooser must not depend on that to stay
                  // usable if the skip is ever removed.)
                  <Button
                    variant="primary"
                    size="md"
                    type="button"
                    onClick={goToPasswordStep}
                  >
                    Sign in with your account
                  </Button>
                ))}
            </div>
          )}

          {onPasswordStep && (
            <>
              {/* Only offer Back when there is somewhere to go back to. */}
              {bothAvailable && (
                <div style={{ margin: "0 0 16px", marginLeft: "-16px" }}>
                  <Button
                    variant="ghost"
                    size="compact"
                    type="button"
                    leftIcon={<ArrowLeft size={20} />}
                    onClick={() => setStep("choose")}
                  >
                    Back
                  </Button>
                </div>
              )}
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
                {/* Primary is correct here despite the "one per page" rule: the
                    Microsoft button belongs to the other step, so the two are
                    never on screen together. */}
                <Button
                  variant="primary"
                  size="md"
                  type="submit"
                  loading={submitting}
                  disabled={submitting || !email || !password}
                >
                  Sign in
                </Button>
              </div>
            </>
          )}

          {/* On the chooser step there is no password field to hang the error on,
              so an Entra redirect failure needs its own line. */}
          {!onPasswordStep && message && (
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

        /* --- the pipeline graphic -------------------------------------------
           Sizes come from two custom properties so the travel distance is derived
           rather than duplicated: the keyframes below compute it from --track and
           --dot, which means changing the width here cannot leave the dots
           overshooting or stopping short. Both are multiples of 4px. */
        .login-pipeline {
          --track: 240px;
          --dot: 8px;
          position: relative;
          width: var(--track);
          height: var(--dot);
          display: flex;
          align-items: center;
          justify-content: space-between;
        }

        /* The line itself, behind the nodes. An operational grey, since it is a
           divider rather than a signal. */
        .login-pipeline-track {
          position: absolute;
          left: 0;
          right: 0;
          top: 50%;
          height: 2px;
          transform: translateY(-50%);
          background: var(--cc-grey-four);
        }

        /* Four stage nodes, evenly spaced by the flex row. */
        .login-pipeline-node {
          position: relative;
          width: var(--dot);
          height: var(--dot);
          border-radius: 9999px;
          background: var(--cc-heritage-blue);
          animation: login-pipeline-pulse 2.4s ease-in-out infinite;
        }
        /* Staggered so the row breathes in sequence instead of blinking as one. */
        .login-pipeline-node:nth-child(2) { animation-delay: 0s; }
        .login-pipeline-node:nth-child(3) { animation-delay: 0.3s; }
        .login-pipeline-node:nth-child(4) { animation-delay: 0.6s; }
        .login-pipeline-node:nth-child(5) { animation-delay: 0.9s; }

        /* The travelling dots. translateX only — deliberately not offset-path,
           which buys nothing here and carries compatibility caveats. */
        .login-pipeline-dot {
          position: absolute;
          left: 0;
          top: 50%;
          width: var(--dot);
          height: var(--dot);
          margin-top: calc(var(--dot) / -2);
          border-radius: 9999px;
          /* 6s per pass: ambient, not attention-seeking. */
          animation: login-pipeline-travel 6s linear infinite;
        }
        .login-pipeline-dot--ok {
          background: var(--cc-heritage-blue);
        }
        /* Both dots are blue. A red one read as a warning on the sign-in screen —
           the one place with no context to interpret it, before the user has even
           authenticated. Two orders in flight say "steady traffic", which is the
           honest ambient message here; alert colour belongs in the feed, where it
           refers to something the reader can open. Half a cycle behind the first,
           via a NEGATIVE delay so it is already mid-track on first paint instead of
           leaving three dead seconds. */
        .login-pipeline-dot--trailing {
          background: var(--cc-heritage-blue);
          animation-delay: -3s;
        }

        @keyframes login-pipeline-travel {
          /* Fade in and out at the ends so a dot does not pop into or out of
             existence at the edge of the track. */
          0%   { transform: translateX(0); opacity: 0; }
          8%   { opacity: 1; }
          92%  { opacity: 1; }
          100% { transform: translateX(calc(var(--track) - var(--dot))); opacity: 0; }
        }

        @keyframes login-pipeline-pulse {
          0%, 100% { opacity: 0.35; transform: scale(1); }
          50%      { opacity: 1; transform: scale(1.35); }
        }

        /* Reduced motion: stop everything and leave a STILL, legible graphic —
           the track, four solid nodes, and the two dots parked at rest on it. Not
           display:none, which would remove the illustration rather than calm it. */
        @media (prefers-reduced-motion: reduce) {
          .login-pipeline-node,
          .login-pipeline-dot {
            animation: none;
          }
          .login-pipeline-node {
            opacity: 1;
            transform: none;
          }
          .login-pipeline-dot {
            opacity: 1;
          }
          .login-pipeline-dot--ok {
            transform: translateX(calc((var(--track) - var(--dot)) * 0.35));
          }
          .login-pipeline-dot--trailing {
            transform: translateX(calc((var(--track) - var(--dot)) * 0.7));
          }
        }
      `}</style>
    </div>
  );
}
