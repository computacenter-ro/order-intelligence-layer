"""[5] Core Backend — Microsoft Entra ID (Azure AD) sign-in.

A THIRD verification path that mints the SAME session as password login. The
split in :mod:`backend.auth` is what makes this cheap: this module only has to
prove who the caller is, then call :func:`backend.auth.issue_token` +
:func:`backend.auth.set_auth_cookie`. ``get_current_user``, every guarded route,
the ``/ws`` handshake and the dashboard's auth gate are untouched.

Flow (backend-driven OAuth 2.0 authorization code, confidential client)::

    GET /auth/entra/login     -> 307 to login.microsoftonline.com
                                 (state + nonce stored in a signed 5-min cookie)
    GET /auth/entra/callback  -> exchange code + client secret for tokens,
                                 verify nonce, issue_token(identity),
                                 302 to DASHBOARD_URL with the session cookie

Why the state/nonce live in a **cookie** rather than server-side: the backend does
not use Redis (only the AI service does), and this feature does not justify adding
it. A JWT signed with the existing ``JWT_SECRET`` is stateless, survives restarts
and works with multiple workers.

Config (env; Entra is simply DISABLED when any of the first three is missing —
never a crash, same posture as the unset ``TEAMS_WEBHOOK_*`` printing to stdout):

* ``ENTRA_TENANT_ID``     — Directory (tenant) ID from the app registration
* ``ENTRA_CLIENT_ID``     — Application (client) ID
* ``ENTRA_CLIENT_SECRET`` — client secret VALUE (.env only; it expires — that is
  why password login survives as an escape hatch)
* ``ENTRA_REDIRECT_URI``  — must match Azure byte-for-byte
  (default ``http://localhost:8000/auth/entra/callback``)
* ``DASHBOARD_URL``       — reused (already exists for Teams links) as the
  post-login redirect target

Authorization is deliberately NOT enforced here: any account in the tenant that
signs in successfully gets a session. Restricting access is an Azure-portal
concern (Enterprise Application -> "Assignment required" + user/group
assignment), which needs no code change.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from backend.auth import (
    COOKIE_SECURE,
    JWT_ALGORITHM,
    JWT_SECRET,
    issue_token,
    password_login_enabled,
    set_auth_cookie,
)

# --- constants ---------------------------------------------------------------

STATE_COOKIE_NAME = "oil_oauth_state"
# Scoped to /auth: the cookie is only ever read by the callback.
STATE_COOKIE_PATH = "/auth"
STATE_TTL_SECONDS = 300

_AUTHORITY_BASE = "https://login.microsoftonline.com"
_DEFAULT_REDIRECT_URI = "http://localhost:8000/auth/entra/callback"

# MSAL RESERVES "openid", "profile" and "offline_access" and raises if you pass
# them — it adds them itself. So we request no extra scopes: the identity claim
# we need (`preferred_username`) arrives with `profile`, and we never call Graph.
# MSAL's implicit `offline_access` yields a refresh token, which we ignore: the
# session is our own JWT, and when it expires the user signs in again.
_SCOPES: list[str] = []

# Ordered identity claims. `preferred_username` is the UPN/e-mail for a work
# account; `email` covers tenants that surface only that; `oid` is the immutable
# object id — an ugly last resort, but better than refusing a valid sign-in.
_IDENTITY_CLAIMS = ("preferred_username", "email", "oid")

# auth_error reasons handed to the dashboard's login screen.
ERR_BAD_STATE = "bad_state"
ERR_ACCESS_DENIED = "access_denied"
ERR_EXCHANGE_FAILED = "exchange_failed"
ERR_NOT_CONFIGURED = "not_configured"


# --- config ------------------------------------------------------------------


@dataclass(frozen=True)
class EntraConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def authority(self) -> str:
        return f"{_AUTHORITY_BASE}/{self.tenant_id}"


def entra_config() -> EntraConfig | None:
    """Return the Entra config, or None when sign-in is not configured.

    Read at CALL time (not import time) so deployments and tests can change the
    environment without re-importing. All three of tenant/client/secret must be
    present — a half-configured app would fail confusingly at the token exchange
    instead of honestly reporting itself disabled.
    """
    tenant = os.getenv("ENTRA_TENANT_ID", "").strip()
    client = os.getenv("ENTRA_CLIENT_ID", "").strip()
    secret = os.getenv("ENTRA_CLIENT_SECRET", "").strip()
    if not (tenant and client and secret):
        return None
    return EntraConfig(
        tenant_id=tenant,
        client_id=client,
        client_secret=secret,
        redirect_uri=os.getenv("ENTRA_REDIRECT_URI", _DEFAULT_REDIRECT_URI).strip()
        or _DEFAULT_REDIRECT_URI,
    )


def dashboard_url() -> str:
    """Base URL the user is sent back to after the redirect dance."""
    return os.getenv("DASHBOARD_URL", "http://localhost:3000").rstrip("/")


# --- MSAL client -------------------------------------------------------------

_msal_apps: dict[tuple[str, str], Any] = {}


def _msal_app(cfg: EntraConfig) -> Any:
    """Cached MSAL confidential client for ``cfg``.

    ``msal`` is imported HERE, not at module scope, so a missing package can
    never break ``import backend.main`` (same deferred-import posture as
    ``ai_service/semcache.py``). Cached per (tenant, client) because MSAL keeps
    the authority metadata it discovers on first use.

    Tests monkeypatch this function rather than installing a fake MSAL — that
    keeps them hermetic and free of the cache.
    """
    key = (cfg.tenant_id, cfg.client_id)
    cached = _msal_apps.get(key)
    if cached is not None:
        return cached
    import msal  # deferred: see above

    app = msal.ConfidentialClientApplication(
        cfg.client_id,
        authority=cfg.authority,
        client_credential=cfg.client_secret,
    )
    _msal_apps[key] = app
    return app


# --- state/nonce cookie ------------------------------------------------------


def _mint_state_token(state: str, nonce: str) -> str:
    """Sign the CSRF ``state`` + replay ``nonce`` into a short-lived JWT."""
    expires = datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS)
    return jwt.encode(
        {"state": state, "nonce": nonce, "exp": int(expires.timestamp())},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def _read_state_token(token: str | None) -> tuple[str, str] | None:
    """Return ``(state, nonce)`` from a valid token, else None.

    None covers every rejection (absent, tampered, expired, malformed) so the
    callback treats them all as one ``bad_state`` outcome.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    state, nonce = payload.get("state"), payload.get("nonce")
    if not isinstance(state, str) or not isinstance(nonce, str):
        return None
    return state, nonce


# --- identity ----------------------------------------------------------------


def _identity_from_claims(claims: dict) -> str | None:
    """Pick the session identity out of the id_token claims, else None."""
    for key in _IDENTITY_CLAIMS:
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --- routes ------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthConfigOut(BaseModel):
    entra_enabled: bool
    password_login: bool


@router.get("/config", response_model=AuthConfigOut)
async def auth_config() -> AuthConfigOut:
    """Which sign-in methods this server offers.

    Deliberately unauthenticated: the login screen reads it before anyone has a
    session, to decide whether to render the password form at all.
    """
    return AuthConfigOut(
        entra_enabled=entra_config() is not None,
        password_login=password_login_enabled(),
    )


def _set_state_cookie(response: RedirectResponse, state: str, nonce: str) -> None:
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=_mint_state_token(state, nonce),
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        # MUST be lax, not strict: Microsoft redirects the browser back here as a
        # cross-site top-level GET, and strict would withhold the cookie.
        samesite="lax",
        path=STATE_COOKIE_PATH,
    )


@router.get("/entra/login")
async def entra_login() -> RedirectResponse:
    """Start the sign-in dance: redirect the browser to Microsoft.

    ``state`` defends the callback against CSRF, ``nonce`` against id_token
    replay; both are stored in a signed 5-minute cookie so the callback can
    verify the round trip without server-side state.
    """
    cfg = entra_config()
    if cfg is None:
        # 503, not a redirect: nobody is mid-flow yet, and bouncing back to the
        # login screen would hide a plain server misconfiguration.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="entra sign-in is not configured",
        )
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    # MSAL is synchronous and may fetch authority metadata on first use — off the
    # event loop, which also runs the RabbitMQ consumers.
    auth_url = await asyncio.to_thread(
        lambda: _msal_app(cfg).get_authorization_request_url(
            _SCOPES,
            state=state,
            nonce=nonce,
            redirect_uri=cfg.redirect_uri,
        )
    )
    response = RedirectResponse(auth_url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    _set_state_cookie(response, state, nonce)
    return response


def _failure(reason: str) -> RedirectResponse:
    """Send the user back to the login screen with a reason.

    Never a raw 4xx/5xx body: the browser is mid-navigation on :8000, so an error
    page here would dump JSON at a URL the user cannot recover from. The login
    screen turns ``?auth_error=`` into one sentence.
    """
    response = RedirectResponse(
        f"{dashboard_url()}/?auth_error={reason}", status_code=status.HTTP_302_FOUND
    )
    response.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    return response


@router.get("/entra/callback")
async def entra_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish the sign-in dance: code -> tokens -> our own session cookie."""
    cfg = entra_config()
    if cfg is None:
        return _failure(ERR_NOT_CONFIGURED)
    if error:
        # User cancelled, or the tenant refused (e.g. app assignment required).
        return _failure(ERR_ACCESS_DENIED)

    stored = _read_state_token(request.cookies.get(STATE_COOKIE_NAME))
    if (
        stored is None
        or not state
        or not code
        or not secrets.compare_digest(stored[0], state)
    ):
        return _failure(ERR_BAD_STATE)
    expected_nonce = stored[1]

    try:
        # Synchronous MSAL: HTTP round trip to the token endpoint, off the loop.
        result = await asyncio.to_thread(
            lambda: _msal_app(cfg).acquire_token_by_authorization_code(
                code,
                scopes=_SCOPES,
                redirect_uri=cfg.redirect_uri,
                nonce=expected_nonce,
            )
        )
    except Exception:
        # Network failure, expired/rotated secret, MSAL validation error — the
        # user gets one honest message rather than a stack trace.
        return _failure(ERR_EXCHANGE_FAILED)

    if not isinstance(result, dict) or "error" in result:
        return _failure(ERR_EXCHANGE_FAILED)
    claims = result.get("id_token_claims") or {}
    # Order matters: NO claims means the exchange itself came back unusable, which
    # is not the same diagnosis as claims whose nonce is wrong (a replay). Check
    # emptiness first, or a broken token response gets reported as bad_state.
    if not isinstance(claims, dict) or not claims:
        return _failure(ERR_EXCHANGE_FAILED)
    # MSAL checks the nonce too; re-checking here keeps the guarantee local and
    # independent of MSAL's version-to-version behaviour.
    if claims.get("nonce") != expected_nonce:
        return _failure(ERR_BAD_STATE)
    identity = _identity_from_claims(claims)
    if identity is None:
        return _failure(ERR_EXCHANGE_FAILED)

    # THE SEAM: same token, same cookie as password login.
    response = RedirectResponse(dashboard_url(), status_code=status.HTTP_302_FOUND)
    response.delete_cookie(STATE_COOKIE_NAME, path=STATE_COOKIE_PATH)
    set_auth_cookie(response, issue_token(identity))
    return response
