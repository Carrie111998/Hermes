"""SmtpProvider send/draft/reply behavior with fake smtplib/imaplib."""
import imaplib

import pytest

from server.email_providers import EMAIL_PROVIDERS, SmtpProvider
from server.email_providers.base import OutgoingEmail


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.tls = False

    def starttls(self):
        self.tls = True

    def login(self, user, pw):
        self.user = user

    def send_message(self, message, from_addr=None, to_addrs=None):
        _FakeSMTP.sent.append({"from": from_addr, "to": to_addrs,
                               "subject": message["Subject"], "body": message.get_content()})

    def quit(self):
        pass


def test_registered():
    assert EMAIL_PROVIDERS["smtp"] is SmtpProvider


def test_requires_core_credentials():
    with pytest.raises(ValueError):
        SmtpProvider().connect_account({"username": "u", "password": "p"})  # no smtp_host


def test_send_uses_starttls_and_recipients(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    p = SmtpProvider()
    p.connect_account({"username": "sales@corp.com", "password": "x",
                       "smtp_host": "mail.corp.com", "smtp_port": 587})
    result = p.send_email(OutgoingEmail(to="buyer@x.com", cc=["mgr@x.com"],
                                        subject="Hallo", body="Merhaba"))
    assert result.status == "sent"
    assert _FakeSMTP.sent[0]["to"] == ["buyer@x.com", "mgr@x.com"]
    assert _FakeSMTP.sent[0]["from"] == "sales@corp.com"


def test_draft_without_imap_is_synthetic():
    p = SmtpProvider()
    p.connect_account({"username": "u@x.com", "password": "x", "smtp_host": "mail.x.com"})
    assert p.create_draft(OutgoingEmail(to="a@x.com", subject="s", body="b")).status == "draft"
    assert p.list_recent_replies() == []  # no imap_host -> nothing to poll
