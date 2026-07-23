"""[5] Core Backend — authentication (Phase 1: single hardcoded admin user).

Two layers, deliberately separated so later auth methods are cheap to add:

* **Verification** — *how* someone proves who they are. Phase 1 is a username +
  bcrypt-password check against ONE env-configured admin. This is the ONLY part
  a later magic-link / SSO flow replaces.
* **Session** — *how the rest of the app trusts a caller*. A signed JWT carried
  in an **httpOnly cookie**; :func:`get_current_user` verifies it. This layer is
  the stable seam: magic-link and SSO will mint the *same* JWT via
  :func:`issue_token` and set the *same* cookie via :func:`set_auth_cookie`, so
  every protected route, the WS handshake, and the frontend guard stay untouched.

The cookie is httpOnly + SameSite so JS can't read it (XSS-safe) and the browser
sends it automatically — including on the WebSocket upgrade, which is how the WS
endpoint authenticates without the browser being able to set custom headers.

Config (env; defaults keep local dev zero-config but are NOT safe for deploy):

* ``ADMIN_USERNAME``       — the one login name (default ``admin``)
* ``ADMIN_PASSWORD_HASH``  — bcrypt hash of the password (see ``hash_password``)
* ``JWT_SECRET``           — HMAC signing secret; MUST be set outside dev
* ``JWT_TTL_SECONDS``      — token lifetime (default 8h)
* ``AUTH_COOKIE_SECURE``   — send cookie only over HTTPS (default false for
  localhost http; set true in any real deployment)

Generate a hash for your chosen password with::

    python -c "from backend.auth import hash_password; print(hash_password('your-pw'))"
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from passlib.context import CryptContext
from pydantic import BaseModel

# --- config ------------------------------------------------------------------

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
# Default is bcrypt("admin") — fine for a first local run, useless for deploy.
# Override with ADMIN_PASSWORD_HASH from .env (gitignored).
_DEFAULT_ADMIN_HASH = "$2b$12$QAn5G1TvIZy9GP8KZlx6A.wkNW3D1nNpG9U0aaE.7xhboXoVOVgZq"
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", _DEFAULT_ADMIN_HASH)

# In dev a stable fallback keeps tokens valid across reloads; in deploy an unset
# secret is a hard error rather than a silently-guessable one.
JWT_SECRET = os.getenv("JWT_SECRET", "dev-insecure-change-me")
JWT_ALGORITHM = "HS256"
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", str(8 * 60 * 60)))

COOKIE_NAME = "oil_session"
COOKIE_SECURE = os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true"

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# --- password + token primitives (the swappable / stable split) --------------


def hash_password(password: str) -> str:
    """Return a bcrypt hash for ``password`` (used to produce ADMIN_PASSWORD_HASH)."""
    return _pwd.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time-ish bcrypt verify. False on any malformed hash."""
    try:
        return _pwd.verify(password, hashed)
    except ValueError:
        return False


def issue_token(username: str) -> str:
    """Mint a signed session JWT for ``username``.

    THE SEAM: password login, magic-link, and SSO all funnel through here so
    they produce byte-for-byte the same session token. Do not add auth-method
    specifics to the payload — keep it to identity + expiry.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=JWT_TTL_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> str | None:
    """Return the username (``sub``) from a valid token, else None.

    None covers every rejection (bad signature, expired, malformed) so callers
    treat "no valid session" uniformly. Used by both the HTTP dependency and the
    WS handshake check.
    """
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return sub if isinstance(sub, str) else None


# --- cookie helpers ----------------------------------------------------------


def set_auth_cookie(response: Response, token: str) -> None:
    """Attach the session JWT as an httpOnly cookie. Shared by every login flow."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=JWT_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    """Delete the session cookie (logout)."""
    response.delete_cookie(key=COOKIE_NAME, path="/")


# --- dependency: the trust boundary every protected route reuses -------------


async def get_current_user(
    oil_session: str | None = Cookie(default=None),
) -> str:
    """FastAPI dependency: return the authenticated username or raise 401.

    Verifies only the JWT in the session cookie — it neither knows nor cares how
    that token was minted (password today, magic-link/SSO tomorrow). Add it to a
    route with ``user: str = Depends(get_current_user)``.
    """
    if oil_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated"
        )
    username = decode_token(oil_session)
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired session"
        )
    return username


# --- routes ------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    username: str


router = APIRouter(prefix="/auth", tags=["auth"])


def authenticate(username: str, password: str) -> bool:
    """Phase-1 verification: match the single hardcoded admin.

    Replace ONLY this function (and add sibling login endpoints) for magic-link /
    SSO — everything downstream keys off :func:`issue_token` / the cookie.
    """
    if username != ADMIN_USERNAME:
        return False
    return verify_password(password, ADMIN_PASSWORD_HASH)


@router.post("/login", response_model=UserOut)
async def login(body: LoginRequest, response: Response) -> UserOut:
    if not authenticate(body.username, body.password):
        # One message for both wrong-user and wrong-password — no user enumeration.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials"
        )
    set_auth_cookie(response, issue_token(body.username))
    return UserOut(username=body.username)


@router.post("/logout")
async def logout(response: Response) -> dict:
    clear_auth_cookie(response)
    return {"status": "logged out"}


@router.get("/me", response_model=UserOut)
async def me(user: str = Depends(get_current_user)) -> UserOut:
    """Who am I? The frontend guard calls this on load: 200 → app, 401 → login."""
    return UserOut(username=user)
