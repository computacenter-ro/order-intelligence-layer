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
