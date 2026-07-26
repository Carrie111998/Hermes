"""BrowserWebmailProvider dispatch/parse behavior with a faked hermes process."""
import pytest

from server.email_providers import EMAIL_PROVIDERS, BrowserWebmailProvider
from server.email_providers.base import OutgoingEmail


class _FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _provider():
    p = BrowserWebmailProvider()
    p.connect_account({"webmail_url": "https://mail.x.com", "username": "u@x.com",
                       "password": "s3cret"})
    return p


def test_registered():
    assert EMAIL_PROVIDERS["browser"] is BrowserWebmailProvider


def test_requires_webmail_url():
    with pytest.raises(ValueError):
        BrowserWebmailProvider().connect_account({"username": "u", "password": "p"})


def test_send_builds_prompt_and_parses_last_json(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return _FakeProc('log {"noise": 1}\n{"provider_message_id": "wm-1", "status": "sent"}')

    monkeypatch.setattr("subprocess.run", fake_run)
    result = _provider().send_email(OutgoingEmail(to="b@y.com", cc=["m@y.com"],
                                                  subject="Hallo", body="Merhaba"))
    assert result.provider_message_id == "wm-1" and result.status == "sent"
    prompt = calls["cmd"][2]
    assert "https://mail.x.com" in prompt and "s3cret" in prompt and '"action": "send"' in prompt
    assert calls["cmd"][0] == "hermes" and "webmail-send" in calls["cmd"]


def test_agent_error_raises(monkeypatch):
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: _FakeProc('{"error": "CAPTCHA"}'))
    with pytest.raises(RuntimeError, match="CAPTCHA"):
        _provider().send_email(OutgoingEmail(to="a@b.co", subject="s", body="b"))


def test_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: _FakeProc("", returncode=3, stderr="browser crashed"))
    with pytest.raises(RuntimeError, match="status 3"):
        _provider().list_recent_replies()
