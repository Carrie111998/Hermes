
import pytest


def test_gmail_auth_missing_credentials_fails_closed(tmp_path):
    from app.gmail.auth import GmailAuthError, GmailAuthenticator
    with pytest.raises(GmailAuthError, match="credentials missing"):
        GmailAuthenticator(tmp_path / "missing.json", tmp_path / "token.json").authorize()


def test_gmail_auth_refresh_reuses_local_token(tmp_path, monkeypatch):
    from app.gmail import auth

    credentials = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    credentials.write_text("{}")
    token.write_text("{}")

    class FakeCredentials:
        expired = True
        refresh_token = "refresh-token-not-logged"
        valid = False
        @classmethod
        def from_authorized_user_file(cls, path, scopes):
            assert path == str(token)
            assert scopes == auth.SCOPES
            return cls()
        def refresh(self, request): self.valid = True
        def to_json(self): return '{"token":"redacted-test-token"}'

    monkeypatch.setattr("google.oauth2.credentials.Credentials", FakeCredentials)
    result = auth.GmailAuthenticator(credentials, token).authorize()
    assert result.valid is True
    assert token.read_text() == '{"token":"redacted-test-token"}'
    assert oct(token.stat().st_mode & 0o777) == "0o600"


def test_gmail_auth_uses_persistent_loopback_listener(tmp_path, monkeypatch):
    from app.gmail import auth

    credentials = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    credentials.write_text("{}")
    calls = {}

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, **kwargs):
            calls.update(kwargs)
            return type("Credentials", (), {"to_json": lambda self: "{}"})()

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", FakeFlow)
    auth.GmailAuthenticator(credentials, token).authorize()

    assert calls == {
        "host": "localhost",
        "bind_addr": "127.0.0.1",
        "port": 8765,
        "timeout_seconds": None,
        "open_browser": False,
    }
