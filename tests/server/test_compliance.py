"""Cold-outreach compliance checks.

These guard a legal requirement, not a feature: CAN-SPAM, GDPR Art. 21, and
KVKK all require a working opt-out. If any of these fail, outbound email must
not ship.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # reuse test_api_mvp helpers

from server import compliance
from server.email_providers import EMAIL_PROVIDERS
from server.email_providers.base import OutgoingEmail, SendResult

from test_api_mvp import (  # noqa: E402  (path is set up above)
    TEST_CREDENTIAL_KEY, make_client, seed_lead_and_contact, wait_for_run,
)

SECRET = TEST_CREDENTIAL_KEY


# --- token integrity ---------------------------------------------------------

def test_token_round_trips_and_rejects_tampering():
    token = compliance.sign_token(SECRET, "cmp_1", "Buyer@Example.COM")
    assert compliance.verify_token(SECRET, token) == ("cmp_1", "buyer@example.com")
    # a flipped payload byte must not validate
    head, sig = token.rsplit(".", 1)
    assert compliance.verify_token(SECRET, f"{head}x.{sig}") is None
    # a token signed by another deployment must not validate
    other = compliance.sign_token("b" * 43 + "=", "cmp_1", "buyer@example.com")
    assert compliance.verify_token(SECRET, other) is None
    assert compliance.verify_token(SECRET, "garbage") is None


def test_token_cannot_be_retargeted_across_tenants():
    """The company id is inside the signed payload, so it cannot be swapped."""
    a = compliance.verify_token(SECRET, compliance.sign_token(SECRET, "cmp_a", "x@y.com"))
    b = compliance.verify_token(SECRET, compliance.sign_token(SECRET, "cmp_b", "x@y.com"))
    assert a == ("cmp_a", "x@y.com") and b == ("cmp_b", "x@y.com")


def test_missing_secret_fails_closed():
    try:
        compliance.sign_token("", "cmp_1", "x@y.com")
    except RuntimeError:
        return
    raise AssertionError("signing without a secret must raise, not emit an unsigned link")


# --- footer injection --------------------------------------------------------

def test_footer_is_appended_when_template_has_no_marker():
    body = compliance.inject_footer("Hello there.", "https://x.test/u/tok")
    assert "https://x.test/u/tok" in body and body.startswith("Hello there.")


def test_footer_respects_an_explicit_marker_and_does_not_duplicate():
    body = compliance.inject_footer(
        f"Hi.\nOpt out: {compliance.UNSUBSCRIBE_MARKER}", "https://x.test/u/tok")
    assert body.count("https://x.test/u/tok") == 1
    assert compliance.UNSUBSCRIBE_MARKER not in body
    # already-present URL is not appended a second time
    assert compliance.inject_footer(body, "https://x.test/u/tok").count("https://x.test/u/tok") == 1


def test_turkish_footer_is_used_for_turkish_sends():
    body = compliance.inject_footer("Merhaba.", "https://x.test/u/t", language="tr")
    assert "durdurmak" in body


# --- the send path always injects, and suppression blocks it -----------------

class CapturingProvider:
    """Records what the delivery boundary actually handed to a provider."""

    last: OutgoingEmail | None = None

    def connect_account(self, credentials: dict) -> None: pass
    def refresh_token(self) -> None: pass

    def create_draft(self, email: OutgoingEmail) -> SendResult:
        CapturingProvider.last = email
        return SendResult("draft_capture", "draft")

    def send_email(self, email: OutgoingEmail) -> SendResult:
        CapturingProvider.last = email
        return SendResult("sent_capture", "sent")

    def send_draft(self, draft_id: str) -> SendResult:
        return SendResult(draft_id, "sent")

    def get_message_status(self, provider_message_id: str) -> str: return "sent"
    def list_recent_replies(self) -> list[dict]: return []
    def disconnect_account(self) -> None: pass


def _approved_message(app, client, headers, company_id, provider="stub"):
    from server.db import json_dump, new_id, now
    stamp = now()
    app.state.db.execute("INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)", (
        new_id("int"), company_id, "email", provider, "connected", None, json_dump({}), stamp, stamp,
    ))
    lead, _ = seed_lead_and_contact(client, headers)
    run = wait_for_run(
        client, headers,
        client.post(f"/api/v1/leads/{lead['id']}/generate-outreach", headers=headers).json()["id"],
    )
    message_id = run["output_ref"]
    client.post(f"/api/v1/outreach/messages/{message_id}/approve", headers=headers)
    return lead, message_id


def test_delivered_body_always_carries_an_opt_out_link():
    EMAIL_PROVIDERS["capture"] = CapturingProvider
    try:
        app, client, headers, company_id = make_client()
        _lead, message_id = _approved_message(app, client, headers, company_id, provider="capture")
        res = client.post(f"/api/v1/outreach/messages/{message_id}/create-draft", headers=headers)
        assert res.status_code == 200, res.text
        sent = CapturingProvider.last
        assert sent is not None, "provider was never invoked"
        assert "/api/v1/unsubscribe/" in sent.body, sent.body
        # RFC 2369/8058 headers travel with it
        assert sent.headers["List-Unsubscribe"].startswith("<http")
        assert sent.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
        # and the link in the body actually resolves to this tenant + recipient
        token = sent.body.split("/api/v1/unsubscribe/")[1].split()[0].strip()
        assert compliance.verify_token(SECRET, token) == (company_id, sent.to.lower())
    finally:
        EMAIL_PROVIDERS.pop("capture", None)
        CapturingProvider.last = None


def test_suppressed_recipient_cannot_be_sent_to():
    app, client, headers, company_id = make_client()
    _lead, message_id = _approved_message(app, client, headers, company_id)
    to = client.get(f"/api/v1/outreach/messages/{message_id}", headers=headers).json()["content"]["to"]
    # Insert the suppression directly, without the do_not_contact mirror, so
    # this asserts the suppression table itself blocks the send. Otherwise the
    # pre-existing do_not_contact check would pass the test on its own.
    from server.db import now
    app.state.db.execute(
        "INSERT INTO suppressions(company_id,email,reason,created_at) VALUES(?,?,?,?)",
        (company_id, compliance.normalize_email(to), "recipient_unsubscribed", now()),
    )
    res = client.post(f"/api/v1/outreach/messages/{message_id}/send", headers=headers)
    assert res.status_code == 409, res.text
    assert "unsubscribed" in res.text, res.text


def test_unsubscribe_endpoint_suppresses_and_is_idempotent():
    app, client, headers, company_id = make_client()
    token = compliance.sign_token(SECRET, company_id, "buyer@example.test")
    assert client.get(f"/api/v1/unsubscribe/{token}").status_code == 200
    # GET only renders the confirmation; it must not opt anyone out on its own,
    # or a link scanner in a mail client would unsubscribe every recipient.
    assert compliance.is_suppressed(app.state.db, company_id, "buyer@example.test") is False
    assert client.post(f"/api/v1/unsubscribe/{token}").status_code == 200
    assert compliance.is_suppressed(app.state.db, company_id, "buyer@example.test") is True
    # replaying the same link must not error
    assert client.post(f"/api/v1/unsubscribe/{token}").status_code == 200


def test_unsubscribe_rejects_a_forged_token():
    _app, client, _headers, _company_id = make_client()
    assert client.get("/api/v1/unsubscribe/not.avalidtoken").status_code == 404
    assert client.post("/api/v1/unsubscribe/not.avalidtoken").status_code == 404


def test_suppression_is_tenant_scoped():
    app, client, headers, company_id = make_client()
    compliance.suppress(app.state.db, company_id, "shared@buyer.test", "recipient_unsubscribed")
    assert compliance.is_suppressed(app.state.db, company_id, "shared@buyer.test") is True
    assert compliance.is_suppressed(app.state.db, "cmp_other", "shared@buyer.test") is False


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} compliance checks passed")
