"""OAuth state integrity.

The callback is unauthenticated by necessity (the provider redirects a browser
to it), so the signed `state` parameter IS the authorization boundary. If these
checks fail, one tenant can attach a mailbox to another tenant's account.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import HTTPException

from server.routes import oauth

from test_webui import TEST_CREDENTIAL_KEY, chat_tenant, make_client  # noqa: E402

SECRET = TEST_CREDENTIAL_KEY


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


def test_callback_reports_provider_denial_without_erroring():
    _app, client = make_client(
        google_oauth_client_id="cid", google_oauth_client_secret="sec")
    res = client.get("/api/v1/integrations/email/oauth/google/callback"
                     "?error=access_denied")
    assert res.status_code == 200 and "cancelled" in res.text.lower()


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} OAuth checks passed")
