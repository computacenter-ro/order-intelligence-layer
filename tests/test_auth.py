"""Tests for backend/auth.py — the password-login gate.

Entra ID is the intended sign-in path; password login stays as a local-dev and
secret-expiry escape hatch and must be switchable off in a deployment, where the
default admin/admin hash would otherwise be an SSO bypass.
"""

from fastapi.testclient import TestClient

from backend.auth import password_login_enabled
from backend.main import app

client = TestClient(app)


def test_password_login_enabled_by_default(monkeypatch):
    monkeypatch.delenv("PASSWORD_LOGIN_ENABLED", raising=False)
    assert password_login_enabled() is True


def test_password_login_disabled_by_env(monkeypatch):
    for value in ("false", "False", "0", "no"):
        monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", value)
        assert password_login_enabled() is False, value


def test_login_route_available_by_default(monkeypatch):
    monkeypatch.delenv("PASSWORD_LOGIN_ENABLED", raising=False)
    res = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert res.status_code == 200
    assert res.cookies.get("oil_session")


def test_login_route_404s_when_disabled(monkeypatch):
    """404, not 403: with the flag off the endpoint does not exist as far as a
    caller is concerned, so it advertises nothing about the admin account."""
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "false")
    res = client.post("/auth/login", json={"username": "admin", "password": "admin"})
    assert res.status_code == 404
    assert res.cookies.get("oil_session") is None


# =============================================================================
# Session-cookie SameSite — env-driven (AUTH_COOKIE_SAMESITE)
#
# A cross-site deployment (dashboard and backend on different subdomains, as on
# Azure Container Apps) needs SameSite=None, or the browser withholds the session
# cookie on cross-site fetch AND on the WebSocket upgrade — login would look like
# it worked and then every guarded call would 401.
#
# These exercise the pure resolver + the two cookie helpers via monkeypatched
# module constants. Deliberately NO importlib.reload: backend/api.py captures
# get_current_user at import, so reloading backend.auth swaps that function
# object out and silently detaches every guarded route from the object the other
# test modules override (it broke all of test_chat.py when tried).
# =============================================================================
import pathlib

import pytest
from fastapi import Response

import backend.auth as auth_mod
from backend.auth import resolve_cookie_samesite


def test_cookie_samesite_defaults_to_lax():
    """Unset => today's behavior, so local docker-compose dev is unchanged."""
    assert resolve_cookie_samesite(None, secure=False) == "lax"
    # And the live module constant reflects that default in the test env.
    assert auth_mod.COOKIE_SAMESITE == "lax"


def test_cookie_samesite_accepts_the_valid_values():
    assert resolve_cookie_samesite("lax", secure=False) == "lax"
    assert resolve_cookie_samesite("strict", secure=False) == "strict"
    assert resolve_cookie_samesite("none", secure=True) == "none"


def test_cookie_samesite_is_normalized():
    assert resolve_cookie_samesite("  None  ", secure=True) == "none"
    assert resolve_cookie_samesite("LAX", secure=False) == "lax"


def test_cookie_samesite_none_requires_secure():
    """SameSite=None without Secure is rejected outright by browsers, which would
    silently drop the session cookie — so it must fail loudly at startup."""
    with pytest.raises(RuntimeError, match="requires AUTH_COOKIE_SECURE"):
        resolve_cookie_samesite("none", secure=False)


def test_cookie_samesite_rejects_an_invalid_value():
    with pytest.raises(RuntimeError, match="must be one of"):
        resolve_cookie_samesite("sometimes", secure=True)


@pytest.mark.parametrize(
    "samesite,secure",
    [("lax", False), ("strict", False), ("none", True)],
)
def test_set_and_clear_cookie_use_the_same_samesite(monkeypatch, samesite, secure):
    """THE invariant: a logout whose SameSite/Secure differ from the login's is
    ignored by the browser, leaving the user signed in. Both helpers must emit
    identical attributes for every configuration."""
    monkeypatch.setattr(auth_mod, "COOKIE_SAMESITE", samesite)
    monkeypatch.setattr(auth_mod, "COOKIE_SECURE", secure)

    set_res, clear_res = Response(), Response()
    auth_mod.set_auth_cookie(set_res, "tok")
    auth_mod.clear_auth_cookie(clear_res)

    set_header = set_res.headers["set-cookie"].lower()
    clear_header = clear_res.headers["set-cookie"].lower()

    assert f"samesite={samesite}" in set_header
    assert f"samesite={samesite}" in clear_header
    assert ("secure" in set_header) is secure
    assert ("secure" in set_header) == ("secure" in clear_header)
    # httponly + path must also match, or the deletion targets a different cookie.
    assert "httponly" in set_header and "httponly" in clear_header
    assert "path=/" in set_header and "path=/" in clear_header


def test_entra_state_cookie_stays_lax():
    """The oauth_state cookie must stay lax REGARDLESS of the session setting:
    the Microsoft -> callback hop is a top-level redirect with its own rules.
    Asserted against the source so a future refactor can't quietly make it
    follow AUTH_COOKIE_SAMESITE."""
    import backend.auth_entra as entra_mod

    source = pathlib.Path(entra_mod.__file__).read_text(encoding="utf-8")
    assert 'samesite="lax"' in source
    assert "COOKIE_SAMESITE" not in source
