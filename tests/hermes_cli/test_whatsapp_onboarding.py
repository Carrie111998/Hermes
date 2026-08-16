import asyncio
import time
from contextlib import contextmanager


class _FakeProc:
    def __init__(self, lines=None, returncode=0):
        self.stdout = iter(lines or [])
        self._returncode = returncode
        self.terminated = False
        self.killed = False
        self.pid = 12345

    def poll(self):
        return None if not self.terminated and not self.killed else self._returncode

    def wait(self, timeout=None):
        return self._returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True








def test_apply_whatsapp_onboarding_saves_pairing_policy(monkeypatch):
    from hermes_cli import web_server as ws

    saved = {}
    removed = []
    enabled = []

    monkeypatch.setattr(ws, "save_env_value", lambda key, value: saved.setdefault(key, value))
    monkeypatch.setattr(ws, "remove_env_value", lambda key: removed.append(key))
    monkeypatch.setattr(ws, "_write_platform_enabled", lambda platform, value: enabled.append((platform, value)))
    monkeypatch.setattr(
        ws,
        "_restart_gateway_after_whatsapp_onboarding",
        lambda profile=None: {"restart_started": True, "restart_pid": 12345},
    )

    record = ws._WhatsAppOnboardingSession(
        proc=None,
        mode="bot",
        allowed_users="",
        session_path="/tmp/session",
        expires_at="2099-01-01T00:00:00Z",
        expires_at_ts=time.time() + 600,
        status="connected",
    )
    ws._whatsapp_onboarding_sessions.clear()
    ws._whatsapp_onboarding_sessions["pairing"] = record

    result = asyncio.run(
        ws.apply_whatsapp_onboarding(
            "pairing",
            ws.WhatsAppOnboardingApply(mode="bot", allowed_users=""),
        )
    )

    assert result["ok"] is True
    assert saved["WHATSAPP_MODE"] == "bot"
    assert saved["WHATSAPP_DM_POLICY"] == "pairing"
    assert saved["WHATSAPP_ENABLED"] == "true"
    assert "WHATSAPP_ALLOWED_USERS" not in removed
    assert enabled == [("whatsapp", True)]
    assert "pairing" not in ws._whatsapp_onboarding_sessions


def test_start_whatsapp_onboarding_existing_creds_returns_linked_account(monkeypatch, tmp_path):
    from hermes_cli import web_server as ws

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "creds.json").write_text(
        '{"registered":true,"me":{"id":"15551234567:1@s.whatsapp.net","name":"Hermes Bot"}}',
        encoding="utf-8",
    )

    old_proc = _FakeProc(returncode=1)
    old_record = ws._WhatsAppOnboardingSession(
        proc=old_proc,
        mode="bot",
        allowed_users="",
        session_path=str(session_dir),
        expires_at="2099-01-01T00:00:00Z",
        expires_at_ts=time.time() + 600,
    )
    ws._whatsapp_onboarding_sessions.clear()
    ws._whatsapp_onboarding_sessions["old"] = old_record
    monkeypatch.setattr(ws, "_whatsapp_session_path", lambda: session_dir)
    monkeypatch.setattr(ws.secrets, "token_urlsafe", lambda size: "existing-creds")

    result = asyncio.run(
        ws.start_whatsapp_onboarding(
            ws.WhatsAppOnboardingStart(mode="self-chat", allowed_users="")
        )
    )

    assert result["pairing_id"] == "existing-creds"
    assert result["status"] == "connected"
    assert result["qr_payload"] is None
    assert result["account_id"] == "15551234567:1@s.whatsapp.net"
    assert result["account_name"] == "Hermes Bot"
    assert result["account_phone"] == "15551234567"
    assert old_record.status == "cancelled"
    assert old_proc.terminated is True
    assert ws._whatsapp_onboarding_sessions["existing-creds"].account_phone == "15551234567"
    ws._whatsapp_onboarding_sessions.clear()


def test_start_whatsapp_onboarding_unregistered_creds_starts_pairing(monkeypatch, tmp_path):
    from hermes_cli import web_server as ws

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "creds.json").write_text(
        '{"registered":false,"me":{"id":"15551234567:1@s.whatsapp.net"}}',
        encoding="utf-8",
    )

    ws._whatsapp_onboarding_sessions.clear()
    scoped_profiles = []

    @contextmanager
    def profile_scope(profile):
        scoped_profiles.append(profile)
        yield

    monkeypatch.setattr(ws, "_config_profile_scope", profile_scope)
    monkeypatch.setattr(ws, "_whatsapp_session_path", lambda: session_dir)
    monkeypatch.setattr(ws, "_run_whatsapp_pairing", lambda *args: None)
    monkeypatch.setattr(ws.secrets, "token_urlsafe", lambda size: "unregistered-creds")

    result = asyncio.run(
        ws.start_whatsapp_onboarding(
            ws.WhatsAppOnboardingStart(mode="bot", allowed_users="15557654321"),
            profile="work",
        )
    )

    assert result["pairing_id"] == "unregistered-creds"
    assert result["status"] == "starting"
    assert result["account_id"] is None
    assert scoped_profiles == ["work"]
    assert ws._whatsapp_onboarding_sessions["unregistered-creds"].profile == "work"
    assert ws._whatsapp_onboarding_sessions["unregistered-creds"].session_path == str(session_dir)
    ws._whatsapp_onboarding_sessions.clear()


def test_linked_account_accepts_legacy_creds_with_account_identity(tmp_path):
    from hermes_cli import web_server as ws

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "creds.json").write_text(
        '{"me":{"id":"15551234567:1@s.whatsapp.net","name":"Legacy Bot"}}',
        encoding="utf-8",
    )

    assert ws._whatsapp_linked_account_from_session(session_dir) == (
        True,
        "15551234567:1@s.whatsapp.net",
        "Legacy Bot",
        "15551234567",
    )


def test_apply_whatsapp_onboarding_rejects_a_different_profile(monkeypatch):
    from hermes_cli import web_server as ws

    @contextmanager
    def profile_scope(_profile):
        yield

    monkeypatch.setattr(ws, "_config_profile_scope", profile_scope)
    monkeypatch.setattr(ws, "save_env_value", lambda *_args: None)
    monkeypatch.setattr(ws, "_write_platform_enabled", lambda *_args: None)
    monkeypatch.setattr(
        ws,
        "_restart_gateway_after_whatsapp_onboarding",
        lambda profile=None: {"restart_started": True, "restart_pid": 12345},
    )
    ws._whatsapp_onboarding_sessions.clear()
    ws._whatsapp_onboarding_sessions["profile-pairing"] = ws._WhatsAppOnboardingSession(
        proc=None,
        mode="bot",
        allowed_users="15557654321",
        session_path="/tmp/session",
        expires_at="2099-01-01T00:00:00Z",
        expires_at_ts=time.time() + 600,
        profile="work",
        status="connected",
    )

    try:
        asyncio.run(
            ws.apply_whatsapp_onboarding(
                "profile-pairing",
                ws.WhatsAppOnboardingApply(mode="bot", allowed_users="15557654321"),
                profile="other",
            )
        )
    except ws.HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("cross-profile apply must be rejected")

    assert "profile-pairing" in ws._whatsapp_onboarding_sessions
    ws._whatsapp_onboarding_sessions.clear()


def test_status_and_cancel_hide_pairing_from_a_different_profile():
    from hermes_cli import web_server as ws

    proc = _FakeProc()
    ws._whatsapp_onboarding_sessions.clear()
    ws._whatsapp_onboarding_sessions["private-pairing"] = ws._WhatsAppOnboardingSession(
        proc=proc,
        mode="bot",
        allowed_users="15557654321",
        session_path="/tmp/session",
        expires_at="2099-01-01T00:00:00Z",
        expires_at_ts=time.time() + 600,
        profile="work",
        status="waiting",
    )

    for operation in (
        lambda: ws.get_whatsapp_onboarding_status("private-pairing", profile="other"),
        lambda: ws.cancel_whatsapp_onboarding("private-pairing", profile="other"),
    ):
        try:
            asyncio.run(operation())
        except ws.HTTPException as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError("cross-profile pairing access must be rejected")

    assert "private-pairing" in ws._whatsapp_onboarding_sessions
    assert proc.terminated is False
    ws._whatsapp_onboarding_sessions.clear()




