"""Tests for backend/auth_entra.py — Entra ID (Azure AD) sign-in.

Hermetic: MSAL is never called for real. Tests monkeypatch
``backend.auth_entra._msal_app`` with a stub, so there is no network, no tenant,
and no client secret involved. What is under test is OUR half of the flow — the
state/nonce cookie, the claim-to-identity mapping, and the fact that a successful
exchange mints the SAME ``oil_session`` cookie every other route already trusts.
"""

import pytest
from fastapi.testclient import TestClient

from backend import auth_entra
from backend.auth import COOKIE_NAME, decode_token
from backend.main import app

client = TestClient(app)

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def entra_env(monkeypatch):
    """Configure Entra + a known dashboard URL for one test."""
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    monkeypatch.setenv("ENTRA_CLIENT_ID", CLIENT)
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "s3cret-value")
    monkeypatch.setenv("DASHBOARD_URL", "http://localhost:3000")
    monkeypatch.delenv("ENTRA_REDIRECT_URI", raising=False)


@pytest.fixture
def no_entra_env(monkeypatch):
    for var in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)


# --- config ------------------------------------------------------------------


def test_config_none_until_all_three_vars_present(monkeypatch, no_entra_env):
    assert auth_entra.entra_config() is None
    monkeypatch.setenv("ENTRA_TENANT_ID", TENANT)
    assert auth_entra.entra_config() is None
    monkeypatch.setenv("ENTRA_CLIENT_ID", CLIENT)
    assert auth_entra.entra_config() is None
    monkeypatch.setenv("ENTRA_CLIENT_SECRET", "s3cret-value")
    assert auth_entra.entra_config() is not None


def test_config_defaults_redirect_uri_and_builds_authority(entra_env):
    cfg = auth_entra.entra_config()
    assert cfg.redirect_uri == "http://localhost:8000/auth/entra/callback"
    assert cfg.authority == f"https://login.microsoftonline.com/{TENANT}"


def test_config_honours_explicit_redirect_uri(entra_env, monkeypatch):
    monkeypatch.setenv("ENTRA_REDIRECT_URI", "https://oil.example.com/auth/entra/callback")
    assert auth_entra.entra_config().redirect_uri == (
        "https://oil.example.com/auth/entra/callback"
    )


def test_dashboard_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "http://localhost:3000/")
    assert auth_entra.dashboard_url() == "http://localhost:3000"


# --- state cookie ------------------------------------------------------------


def test_state_token_round_trips():
    token = auth_entra._mint_state_token("st-abc", "no-xyz")
    assert auth_entra._read_state_token(token) == ("st-abc", "no-xyz")


def test_state_token_rejects_missing_tampered_and_expired(monkeypatch):
    token = auth_entra._mint_state_token("st-abc", "no-xyz")
    assert auth_entra._read_state_token(None) is None
    assert auth_entra._read_state_token("") is None
    assert auth_entra._read_state_token(token + "x") is None
    assert auth_entra._read_state_token("not-a-jwt") is None

    # Expired: mint with a negative TTL so `exp` is in the past.
    monkeypatch.setattr(auth_entra, "STATE_TTL_SECONDS", -10)
    stale = auth_entra._mint_state_token("st-abc", "no-xyz")
    assert auth_entra._read_state_token(stale) is None


# --- identity ----------------------------------------------------------------


def test_identity_prefers_preferred_username_then_email_then_oid():
    assert (
        auth_entra._identity_from_claims(
            {"preferred_username": "a@cc.com", "email": "b@cc.com", "oid": "oid-1"}
        )
        == "a@cc.com"
    )
    assert auth_entra._identity_from_claims({"email": "b@cc.com"}) == "b@cc.com"
    assert auth_entra._identity_from_claims({"oid": "oid-1"}) == "oid-1"


def test_identity_none_when_no_usable_claim():
    assert auth_entra._identity_from_claims({}) is None
    assert auth_entra._identity_from_claims({"preferred_username": "   "}) is None
    assert auth_entra._identity_from_claims({"preferred_username": 42}) is None


# --- /auth/config ------------------------------------------------------------


def test_auth_config_reports_both_methods(entra_env, monkeypatch):
    monkeypatch.delenv("PASSWORD_LOGIN_ENABLED", raising=False)
    body = client.get("/auth/config").json()
    assert body == {"entra_enabled": True, "password_login": True}


def test_auth_config_reports_entra_off_and_password_off(no_entra_env, monkeypatch):
    monkeypatch.setenv("PASSWORD_LOGIN_ENABLED", "false")
    body = client.get("/auth/config").json()
    assert body == {"entra_enabled": False, "password_login": False}


def test_auth_config_needs_no_session():
    """It is what the login screen reads BEFORE anyone is authenticated."""
    assert client.get("/auth/config").status_code == 200


# --- GET /auth/entra/login ---------------------------------------------------


class _StubMsal:
    """Records what we hand MSAL and returns a canned authorization URL.

    The unit boundary is deliberate: MSAL's own URL construction is Microsoft's
    to test. What matters here is that WE pass the state/nonce we stored in the
    cookie and our configured redirect URI, and that we redirect to whatever URL
    comes back.
    """

    URL = "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize?stub=1"

    def __init__(self, token_result=None, raises: Exception | None = None):
        self.auth_url_calls: list[dict] = []
        self.code_calls: list[dict] = []
        self._token_result = token_result
        self._raises = raises

    def get_authorization_request_url(self, scopes, state=None, nonce=None, redirect_uri=None):
        self.auth_url_calls.append(
            {"scopes": scopes, "state": state, "nonce": nonce, "redirect_uri": redirect_uri}
        )
        return self.URL

    def acquire_token_by_authorization_code(self, code, scopes, redirect_uri=None, nonce=None):
        self.code_calls.append(
            {"code": code, "scopes": scopes, "redirect_uri": redirect_uri, "nonce": nonce}
        )
        if self._raises is not None:
            raise self._raises
        return self._token_result


@pytest.fixture
def stub_msal(monkeypatch):
    """Install a _StubMsal and hand it back for assertions."""

    def _install(token_result=None, raises=None) -> _StubMsal:
        stub = _StubMsal(token_result=token_result, raises=raises)
        monkeypatch.setattr(auth_entra, "_msal_app", lambda cfg: stub)
        return stub

    return _install


def test_login_redirects_to_microsoft_and_stores_state(entra_env, stub_msal):
    stub = stub_msal()
    res = client.get("/auth/entra/login", follow_redirects=False)

    assert res.status_code == 307
    assert res.headers["location"] == _StubMsal.URL

    # No reserved scopes: MSAL adds openid/profile/offline_access itself.
    call = stub.auth_url_calls[0]
    assert call["scopes"] == []
    assert call["redirect_uri"] == "http://localhost:8000/auth/entra/callback"

    # The cookie must carry exactly the state/nonce handed to Microsoft,
    # otherwise the callback can never validate the round trip.
    cookie = res.cookies.get(auth_entra.STATE_COOKIE_NAME)
    assert cookie
    assert auth_entra._read_state_token(cookie) == (call["state"], call["nonce"])
    assert call["state"] and call["nonce"] and call["state"] != call["nonce"]


def test_login_state_cookie_is_httponly_and_lax(entra_env, stub_msal):
    """SameSite MUST be lax: the Microsoft -> callback hop is a cross-site
    top-level GET, and `strict` would drop the cookie, breaking every sign-in."""
    stub_msal()
    res = client.get("/auth/entra/login", follow_redirects=False)
    header = res.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert f"path={auth_entra.STATE_COOKIE_PATH}".lower() in header


def test_login_503s_when_entra_not_configured(no_entra_env):
    res = client.get("/auth/entra/login", follow_redirects=False)
    assert res.status_code == 503
    assert res.cookies.get(auth_entra.STATE_COOKIE_NAME) is None


# --- GET /auth/entra/callback ------------------------------------------------

CLAIMS = {"preferred_username": "user@computacenter.com", "oid": "oid-1"}


def _begin_login(stub: _StubMsal) -> None:
    """Drive /auth/entra/login so the client holds a real state cookie, and
    prime the stub's nonce into the id_token claims the exchange will return."""
    client.get("/auth/entra/login", follow_redirects=False)


def _nonce_of(stub: _StubMsal) -> str:
    return stub.auth_url_calls[-1]["nonce"]


def _state_of(stub: _StubMsal) -> str:
    return stub.auth_url_calls[-1]["state"]


def test_callback_happy_path_mints_the_same_session_cookie(entra_env, stub_msal):
    """The whole point of the feature: Entra ends in the SAME oil_session JWT
    that password login produces, so nothing downstream had to change."""
    stub = stub_msal()
    _begin_login(stub)
    result = {
        "id_token_claims": {**CLAIMS, "nonce": _nonce_of(stub)},
        "access_token": "ignored",
        "refresh_token": "ignored-too",
    }
    stub._token_result = result

    res = client.get(
        f"/auth/entra/callback?code=the-code&state={_state_of(stub)}",
        follow_redirects=False,
    )

    assert res.status_code == 302
    assert res.headers["location"] == "http://localhost:3000"
    session = res.cookies.get(COOKIE_NAME)
    assert session
    assert decode_token(session) == "user@computacenter.com"

    # We handed MSAL our code, our redirect URI and the nonce we stored.
    call = stub.code_calls[0]
    assert call["code"] == "the-code"
    assert call["scopes"] == []
    assert call["redirect_uri"] == "http://localhost:8000/auth/entra/callback"
    assert call["nonce"] == _nonce_of(stub)

    client.cookies.clear()


def test_callback_identity_falls_back_to_email_then_oid(entra_env, stub_msal):
    for claims, expected in (
        ({"email": "b@cc.com"}, "b@cc.com"),
        ({"oid": "oid-9"}, "oid-9"),
    ):
        stub = stub_msal()
        _begin_login(stub)
        stub._token_result = {"id_token_claims": {**claims, "nonce": _nonce_of(stub)}}
        res = client.get(
            f"/auth/entra/callback?code=c&state={_state_of(stub)}",
            follow_redirects=False,
        )
        assert decode_token(res.cookies.get(COOKIE_NAME)) == expected
        client.cookies.clear()


@pytest.mark.parametrize(
    "query, expected_reason",
    [
        ("code=c&state=not-the-stored-state", auth_entra.ERR_BAD_STATE),
        ("code=c", auth_entra.ERR_BAD_STATE),  # no state at all
        ("state=whatever", auth_entra.ERR_BAD_STATE),  # no code
        ("error=access_denied&error_description=user+cancelled", auth_entra.ERR_ACCESS_DENIED),
    ],
)
def test_callback_rejections_redirect_without_a_session(
    entra_env, stub_msal, query, expected_reason
):
    stub = stub_msal()
    _begin_login(stub)
    res = client.get(f"/auth/entra/callback?{query}", follow_redirects=False)

    assert res.status_code == 302
    assert res.headers["location"] == f"http://localhost:3000/?auth_error={expected_reason}"
    assert res.cookies.get(COOKIE_NAME) is None
    assert stub.code_calls == []  # never exchanged anything
    client.cookies.clear()


def test_callback_without_state_cookie_is_bad_state(entra_env, stub_msal):
    """A callback arriving cold (bookmarked, replayed, or cookie expired)."""
    stub = stub_msal()
    client.cookies.clear()
    res = client.get("/auth/entra/callback?code=c&state=anything", follow_redirects=False)
    assert res.headers["location"].endswith(f"auth_error={auth_entra.ERR_BAD_STATE}")
    assert res.cookies.get(COOKIE_NAME) is None


def test_callback_nonce_mismatch_is_rejected(entra_env, stub_msal):
    """Belt and braces over MSAL's own nonce check: an id_token whose nonce is
    not the one we stored is a replay, not a sign-in."""
    stub = stub_msal()
    _begin_login(stub)
    stub._token_result = {"id_token_claims": {**CLAIMS, "nonce": "some-other-nonce"}}
    res = client.get(
        f"/auth/entra/callback?code=c&state={_state_of(stub)}", follow_redirects=False
    )
    assert res.headers["location"].endswith(f"auth_error={auth_entra.ERR_BAD_STATE}")
    assert res.cookies.get(COOKIE_NAME) is None
    client.cookies.clear()


def test_callback_exchange_error_dict_and_exception(entra_env, stub_msal):
    for kwargs in (
        {"token_result": {"error": "invalid_client", "error_description": "secret expired"}},
        {"token_result": {}},  # no claims at all
        {"raises": RuntimeError("network down")},
    ):
        stub = stub_msal(**kwargs)
        _begin_login(stub)
        res = client.get(
            f"/auth/entra/callback?code=c&state={_state_of(stub)}", follow_redirects=False
        )
        assert res.headers["location"].endswith(
            f"auth_error={auth_entra.ERR_EXCHANGE_FAILED}"
        ), kwargs
        assert res.cookies.get(COOKIE_NAME) is None
        client.cookies.clear()


def test_callback_when_entra_not_configured(no_entra_env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_URL", "http://localhost:3000")
    res = client.get("/auth/entra/callback?code=c&state=s", follow_redirects=False)
    assert res.headers["location"].endswith(f"auth_error={auth_entra.ERR_NOT_CONFIGURED}")
    assert res.cookies.get(COOKIE_NAME) is None


def test_entra_session_authenticates_rest_and_ws(entra_env, stub_msal):
    """THE SEAM REGRESSION: an Entra-minted cookie is accepted by a guarded REST
    route AND the /ws handshake, with no auth-specific code in either."""
    stub = stub_msal()
    _begin_login(stub)
    stub._token_result = {"id_token_claims": {**CLAIMS, "nonce": _nonce_of(stub)}}
    res = client.get(
        f"/auth/entra/callback?code=c&state={_state_of(stub)}", follow_redirects=False
    )
    session = res.cookies.get(COOKIE_NAME)

    authed = TestClient(app)
    authed.cookies.set(COOKIE_NAME, session)
    assert authed.get("/auth/me").json() == {"username": "user@computacenter.com"}
    with authed.websocket_connect("/ws") as ws:
        assert ws is not None
    client.cookies.clear()
