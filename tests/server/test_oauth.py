"""OAuth state integrity.

The callback is unauthenticated by necessity (the provider redirects a browser
to it), so the signed `state` parameter IS the authorization boundary. If these
checks fail, one tenant can attach a mailbox to another tenant's account.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import HTTPException

from server.crypto import CredentialCipher
from server.routes import oauth

from test_webui import TEST_CREDENTIAL_KEY, chat_tenant, make_client  # noqa: E402

SECRET = TEST_CREDENTIAL_KEY


class _TokenResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _configured_client(**overrides):
    return make_client(
        google_oauth_client_id="google-client",
        google_oauth_client_secret="google-secret",
        microsoft_oauth_client_id="microsoft-client",
        microsoft_oauth_client_secret="microsoft-secret",
        public_base_url="https://api.example.test",
        **overrides,
    )


def _seed_pre_oauth_email_rows(app, company_id: str) -> dict[str, dict]:
    rows = {
        "legacy_google": {
            "id": "int_legacy_google",
            "provider": "google",
            "credentials": None,
            "stamp": 50.0,
        },
        "legacy_microsoft": {
            "id": "int_legacy_microsoft",
            "provider": "microsoft",
            "credentials": "",
            "stamp": 40.0,
        },
        "valid_google": {
            "id": "int_valid_google",
            "provider": "google",
            "credentials": app.state.cipher.encrypt({"refresh_token": "google-valid"}),
            "stamp": 30.0,
        },
        "valid_microsoft": {
            "id": "int_valid_microsoft",
            "provider": "microsoft",
            "credentials": app.state.cipher.encrypt({"refresh_token": "microsoft-valid"}),
            "stamp": 20.0,
        },
        "unrelated": {
            "id": "int_unrelated_smtp",
            "provider": "smtp",
            "credentials": None,
            "stamp": 10.0,
        },
    }
    with app.state.db.transaction() as connection:
        connection.executemany(
            "INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["id"], company_id, "email", row["provider"], "connected",
                    row["credentials"], "{}", row["stamp"], row["stamp"],
                )
                for row in rows.values()
            ],
        )
    return rows


@pytest.mark.parametrize("provider", ["google", "microsoft"])
@pytest.mark.parametrize("payload", [None, {}])
def test_legacy_direct_connect_requires_oauth_and_creates_no_integration(provider, payload):
    app, client = _configured_client()
    _admin, headers, company_id = chat_tenant(client)
    kwargs = {"headers": headers}
    if payload is not None:
        kwargs["json"] = payload

    res = client.post(f"/api/v1/integrations/email/connect/{provider}", **kwargs)

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["message"] == f"Connect {provider} mailboxes with OAuth"
    assert detail["oauth_start"] == (
        f"/api/v1/integrations/email/oauth/{provider}/start"
    )
    assert app.state.db.one(
        "SELECT id FROM integrations WHERE company_id=? AND kind='email' AND provider=?",
        (company_id, provider),
    ) is None


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_legacy_direct_connect_requires_company_for_admin(provider):
    _app, client = _configured_client()
    admin_headers, _headers, _company_id = chat_tenant(client)

    res = client.post(
        f"/api/v1/integrations/email/connect/{provider}",
        headers=admin_headers,
    )

    assert res.status_code == 400


@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_legacy_direct_connect_rejects_cross_company_user(provider):
    _app, client = _configured_client()
    admin_headers, company_a_headers, _company_a_id = chat_tenant(client, "Tenant A")
    company_b = client.post(
        "/api/v1/admin/companies",
        headers=admin_headers,
        json={"name": "Tenant B"},
    ).json()
    user = client.post(
        "/api/v1/admin/users",
        headers=company_a_headers,
        json={
            "email": "sales@tenant-a.test",
            "password": "another-secure-password",
            "role": "customer",
            "company_id": company_a_headers["X-Company-ID"],
        },
    ).json()
    login = client.post(
        "/api/v1/auth/login",
        json={"email": user["email"], "password": "another-secure-password"},
    ).json()

    res = client.post(
        f"/api/v1/integrations/email/connect/{provider}",
        headers={"Authorization": f"Bearer {login['access_token']}",
                 "X-Company-ID": company_b["id"]},
    )

    assert res.status_code == 403


def test_email_list_hides_only_credentialless_legacy_hosted_rows():
    app, client = _configured_client()
    _admin, headers, company_id = chat_tenant(client)
    rows = _seed_pre_oauth_email_rows(app, company_id)

    res = client.get("/api/v1/integrations/email", headers=headers)

    assert res.status_code == 200
    assert [item["id"] for item in res.json()] == [
        rows["valid_google"]["id"],
        rows["valid_microsoft"]["id"],
        rows["unrelated"]["id"],
    ]


def test_outreach_selector_skips_credentialless_legacy_hosted_rows():
    app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    rows = _seed_pre_oauth_email_rows(app, company_id)

    integration, credentials = app.state.outreach._integration(company_id, "email")

    assert integration["id"] == rows["valid_google"]["id"]
    assert credentials == {"refresh_token": "google-valid"}


def _callback(client, provider: str, state: str, **query):
    params = {"state": state, **query}
    return client.get(
        f"/api/v1/integrations/email/oauth/{provider}/callback",
        params=params,
    )


def _expect_400(fn, *args):
    try:
        fn(*args)
    except HTTPException as exc:
        assert exc.status_code == 400, exc.detail
        return exc
    raise AssertionError("expected an HTTPException(400)")


def test_state_round_trips():
    state = oauth.sign_state(SECRET, "cmp_1", "google")
    assert oauth.verify_state(SECRET, state, "google") == "cmp_1"


def test_state_rejects_tampering_and_foreign_signatures():
    state = oauth.sign_state(SECRET, "cmp_1", "google")
    head, sig = state.rsplit(".", 1)
    _expect_400(oauth.verify_state, SECRET, f"{head}x.{sig}", "google")
    # signed by a different deployment
    foreign = oauth.sign_state("b" * 43 + "=", "cmp_1", "google")
    _expect_400(oauth.verify_state, SECRET, foreign, "google")
    _expect_400(oauth.verify_state, SECRET, "garbage", "google")
    _expect_400(oauth.verify_state, SECRET, "", "google")


def test_state_cannot_be_replayed_against_another_provider():
    """A Google state must not authorize a Microsoft mailbox attach."""
    state = oauth.sign_state(SECRET, "cmp_1", "google")
    _expect_400(oauth.verify_state, SECRET, state, "microsoft")


def test_state_expires():
    real = time.time
    try:
        state = oauth.sign_state(SECRET, "cmp_1", "google")
        time.time = lambda: real() + oauth.STATE_TTL_SECONDS + 5
        exc = _expect_400(oauth.verify_state, SECRET, state, "google")
        assert "expired" in str(exc.detail)
    finally:
        time.time = real


def test_state_is_not_guessable_across_tenants():
    """Two tenants' states differ, and neither resolves to the other."""
    a = oauth.sign_state(SECRET, "cmp_a", "google")
    b = oauth.sign_state(SECRET, "cmp_b", "google")
    assert a != b
    assert oauth.verify_state(SECRET, a, "google") == "cmp_a"
    assert oauth.verify_state(SECRET, b, "google") == "cmp_b"


# --- endpoint behavior -------------------------------------------------------

def test_start_requires_server_oauth_configuration():
    """Fail with a clear 503 rather than emitting a broken authorize URL."""
    _app, client = make_client()
    _admin, headers, _company_id = chat_tenant(client)
    res = client.post("/api/v1/integrations/email/oauth/google/start", headers=headers)
    assert res.status_code == 503 and "GOOGLE_OAUTH_CLIENT_ID" in res.text


def test_start_builds_an_authorize_url_when_configured():
    _app, client = make_client(
        google_oauth_client_id="cid-123", google_oauth_client_secret="csecret",
        public_base_url="https://api.example.test")
    _admin, headers, _company_id = chat_tenant(client)
    res = client.post("/api/v1/integrations/email/oauth/google/start", headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["authorize_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "cid-123" in body["authorize_url"]
    # offline access is what yields a refresh token; without it the connection
    # silently dies after an hour.
    assert "access_type=offline" in body["authorize_url"]
    assert body["redirect_uri"] == (
        "https://api.example.test/api/v1/integrations/email/oauth/google/callback")


def test_unknown_provider_is_rejected():
    _app, client = make_client()
    assert client.get(
        "/api/v1/integrations/email/oauth/zoho/callback?code=x&state=y").status_code == 404


def test_callback_rejects_a_forged_state_before_any_token_exchange():
    _app, client = make_client(
        google_oauth_client_id="cid", google_oauth_client_secret="sec")
    res = client.get("/api/v1/integrations/email/oauth/google/callback"
                     "?code=stolen&state=forged.signature")
    assert res.status_code == 400, res.text
    assert res.headers["content-type"].startswith("text/html")
    assert '"status": "failed"' in res.text


def test_callback_reports_provider_denial_without_rendering_provider_text():
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")
    provider_error = '<script>window.opener.pwned="provider-secret"</script>'

    res = _callback(client, "google", state, error=provider_error)

    assert res.status_code == 200
    assert "Authorization cancelled" in res.text
    assert '"type": "interfaze:oauth"' in res.text
    assert '"provider": "google"' in res.text
    assert '"status": "cancelled"' in res.text
    assert provider_error not in res.text
    assert "provider-secret" not in res.text
    assert "window.close()" not in res.text


def test_callback_rejects_denial_without_valid_state_as_html_failure():
    _app, client = _configured_client()

    res = client.get(
        "/api/v1/integrations/email/oauth/google/callback",
        params={"error": "access_denied"},
    )

    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")
    assert '"status": "failed"' in res.text
    assert "Return to Interfaze and start again" in res.text


def test_callback_missing_code_is_an_html_failure_after_state_validation():
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")

    res = _callback(client, "google", state)

    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")
    assert '"status": "failed"' in res.text


def test_callback_page_escapes_visible_content_and_serializes_message_data():
    res = oauth._page(
        '<script id="title">bad</script>',
        '<img src=x onerror="bad()">',
        provider="google",
        status="failed",
        status_code=400,
    )

    assert res.status_code == 400
    html = res.body.decode()
    assert '<script id="title">' not in html
    assert '<img src=x' not in html
    assert "&lt;script" in html
    assert "&lt;img" in html
    assert '"provider": "google"' in html


def test_successful_callback_stores_only_encrypted_credentials_for_state_tenant(monkeypatch):
    app, client = _configured_client()
    admin_headers, _headers_a, company_a = chat_tenant(client, "Tenant A")
    company_b_res = client.post(
        "/api/v1/admin/companies",
        headers=admin_headers,
        json={"name": "Tenant B"},
    )
    company_b = company_b_res.json()["id"]
    state = oauth.sign_state(SECRET, company_a, "google")
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, {
            "refresh_token": "refresh-secret",
            "access_token": "access-secret",
        }),
    )

    res = _callback(client, "google", state, code="authorization-code")

    assert res.status_code == 200
    assert '"status": "connected"' in res.text
    assert "window.close()" in res.text
    assert "refresh-secret" not in res.text
    assert "access-secret" not in res.text
    assert "google-secret" not in res.text
    row = app.state.db.one(
        "SELECT * FROM integrations WHERE company_id=? AND kind='email' AND provider='google'",
        (company_a,),
    )
    assert row is not None
    assert row["status"] == "connected"
    assert "refresh-secret" not in row["encrypted_credentials"]
    assert app.state.cipher.decrypt(row["encrypted_credentials"]) == {
        "refresh_token": "refresh-secret",
        "access_token": "access-secret",
        "client_id": "google-client",
        "client_secret": "google-secret",
    }
    assert app.state.db.one(
        "SELECT id FROM integrations WHERE company_id=? AND provider='google'",
        (company_b,),
    ) is None


def test_callback_sanitizes_provider_http_and_invalid_json_failures(monkeypatch):
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")
    provider_body = "upstream refresh_token=leaked client_secret=leaked"
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(401, text=provider_body),
    )

    res = _callback(client, "google", state, code="bad-code")

    assert res.status_code == 502
    assert '"status": "failed"' in res.text
    assert provider_body not in res.text
    assert "refresh_token" not in res.text
    assert "client_secret" not in res.text

    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, payload=None),
    )
    res = _callback(client, "google", state, code="bad-json")
    assert res.status_code == 502
    assert "not json" not in res.text


def test_callback_sanitizes_network_and_missing_refresh_token_failures(monkeypatch):
    _app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "microsoft")

    def fail_network(*args, **kwargs):
        raise oauth.httpx.ConnectError("network-secret")

    monkeypatch.setattr(oauth.httpx, "post", fail_network)
    res = _callback(client, "microsoft", state, code="code")
    assert res.status_code == 502
    assert "network-secret" not in res.text

    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, {"access_token": "only-access"}),
    )
    res = _callback(client, "microsoft", state, code="code")
    assert res.status_code == 502
    assert "only-access" not in res.text


def test_callback_reports_unconfigured_encryption_as_sanitized_html(monkeypatch):
    app, client = _configured_client()
    _admin, _headers, company_id = chat_tenant(client)
    state = oauth.sign_state(SECRET, company_id, "google")
    monkeypatch.setattr(
        oauth.httpx,
        "post",
        lambda *args, **kwargs: _TokenResponse(200, {
            "refresh_token": "refresh-secret",
            "access_token": "access-secret",
        }),
    )
    app.state.cipher = CredentialCipher("")

    res = _callback(client, "google", state, code="code")

    assert res.status_code == 503
    assert '"status": "failed"' in res.text
    assert "refresh-secret" not in res.text
    assert "access-secret" not in res.text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} OAuth checks passed")
