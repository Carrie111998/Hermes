"""Tests for the oauth_reauth quote-reply no-agent short-circuit (Part 3).

Covers:
  * ``extract_oauth_code_from_reply`` — pure code-extraction function, both
    accepted input shapes (full redirect URL, bare code) and rejected ones.
  * ``GatewayRunner._handle_oauth_reauth_reply`` — the deterministic handler:
    successful exchange (pending discarded + state reset), failed exchange
    (pending kept, state untouched), missing/garbage code (no subprocess
    call attempted), and identity resolution from the pending record's
    payload.

The existing ``upstream_fix``/``upstream_pr_fix`` quote-reply flow is
covered separately by tests/gateway/test_check_reply_for_pending_ref.py,
re-run alongside these to confirm zero regressions (see the PR description
for the exact command).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def _stage_oauth_pending(hermes_home, pending_id, identity):
    d = hermes_home / "pending" / "oauth_reauth"
    d.mkdir(parents=True, exist_ok=True)
    record = {
        "id": pending_id,
        "subsystem": "oauth_reauth",
        "action": "oauth_reauth",
        "summary": "test",
        "origin": "cron:hermes-oauth-expiry-check",
        "created_at": 0,
        "payload": {"action": "oauth_reauth", "identity": identity, "kind": "heads_up"},
    }
    (d / f"{pending_id}.json").write_text(json.dumps(record), encoding="utf-8")


class TestExtractOauthCodeFromReply:
    def test_full_redirect_url(self):
        from gateway.run import extract_oauth_code_from_reply

        url = "http://localhost:1/?state=abc123&code=4%2F0AVGzR1abcXYZ&scope=email"
        assert extract_oauth_code_from_reply(url) == "4%2F0AVGzR1abcXYZ"

    def test_full_redirect_url_code_last_param(self):
        from gateway.run import extract_oauth_code_from_reply

        url = "http://localhost:1/?state=abc123&scope=email&code=my-code-here"
        assert extract_oauth_code_from_reply(url) == "my-code-here"

    def test_bare_code(self):
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply("4/0AVGzR1abcXYZ") == "4/0AVGzR1abcXYZ"

    def test_bare_code_with_surrounding_whitespace(self):
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply("  my-code-123  \n") == "my-code-123"

    def test_none_input(self):
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply(None) is None

    def test_empty_string(self):
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply("") is None
        assert extract_oauth_code_from_reply("   ") is None

    def test_url_with_no_code_param_returns_none(self):
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply("http://localhost:1/?state=abc123") is None

    def test_garbage_with_whitespace_and_no_url_rejected(self):
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply("hey here's my code: abc 123") is None

    def test_sentence_without_code_rejected_as_bare(self):
        """A prose reply with no URL and no whitespace-free token still
        shouldn't be treated as a code once it contains spaces."""
        from gateway.run import extract_oauth_code_from_reply

        assert extract_oauth_code_from_reply("I don't have it yet") is None


class TestResetOauthReauthNotifyState:
    def test_reset_writes_expected_shape(self, hermes_home):
        from gateway.run import _reset_oauth_reauth_notify_state

        state_path = hermes_home / "cron" / "oauth_reauth_notify_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {"zarkash": {"last_known_recorded_at": 123.0, "heads_up_sent_at": 456.0, "expired_sent_at": None}}
            ),
            encoding="utf-8",
        )

        _reset_oauth_reauth_notify_state(hermes_home, "zarkash")

        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["zarkash"] == {
            "last_known_recorded_at": None,
            "heads_up_sent_at": None,
            "expired_sent_at": None,
        }

    def test_reset_creates_file_when_missing(self, hermes_home):
        from gateway.run import _reset_oauth_reauth_notify_state

        _reset_oauth_reauth_notify_state(hermes_home, "jid")
        state_path = hermes_home / "cron" / "oauth_reauth_notify_state.json"
        assert state_path.is_file()
        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["jid"]["heads_up_sent_at"] is None


class _FakeSource:
    platform = "telegram"
    chat_id = "12345"
    user_id = "12345"


def _make_ref_check(pending_id):
    from gateway.run import ReplyRefCheck

    return ReplyRefCheck(
        tag_found=True, pending_exists=True,
        subsystem="oauth_reauth", pending_id=pending_id,
    )


@pytest.mark.asyncio
class TestHandleOauthReauthReply:
    async def _run(self, hermes_home, monkeypatch, *, event_text, proc_returncode, pending_id="abc123", identity="zarkash"):
        from gateway.run import GatewayRunner

        _stage_oauth_pending(hermes_home, pending_id, identity)

        fake_self = SimpleNamespace()
        notices = []

        async def _fake_notice(source, content):
            notices.append(content)

        fake_self._deliver_platform_notice = _fake_notice

        fake_proc = MagicMock()
        fake_proc.returncode = proc_returncode
        fake_proc.stdout = "OK: Authenticated." if proc_returncode == 0 else ""
        fake_proc.stderr = "" if proc_returncode == 0 else "ERROR: Token exchange failed"

        import subprocess as real_subprocess
        monkeypatch.setattr(real_subprocess, "run", lambda *a, **k: fake_proc)

        from gateway.run import get_hermes_home
        monkeypatch.setattr("gateway.run.get_hermes_home", lambda: hermes_home)

        event = SimpleNamespace(text=event_text, reply_to_text="[ref:oauth_reauth:%s]" % pending_id)
        source = _FakeSource()
        ref_check = _make_ref_check(pending_id)

        await GatewayRunner._handle_oauth_reauth_reply(fake_self, event, source, ref_check)
        return notices

    async def test_success_discards_pending_and_resets_state(self, hermes_home, monkeypatch):
        from tools import write_approval as wa

        # Pre-seed non-empty notify state so we can prove it gets reset.
        state_path = hermes_home / "cron" / "oauth_reauth_notify_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"zarkash": {"last_known_recorded_at": 1.0, "heads_up_sent_at": 2.0, "expired_sent_at": None}}),
            encoding="utf-8",
        )

        notices = await self._run(
            hermes_home, monkeypatch, event_text="4/0AVGzR1abcXYZ", proc_returncode=0,
        )

        assert any("reconnected" in n.lower() or "all set" in n.lower() for n in notices)
        assert wa.get_pending("oauth_reauth", "abc123") is None

        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["zarkash"]["heads_up_sent_at"] is None

    async def test_failure_keeps_pending_and_does_not_reset_state(self, hermes_home, monkeypatch):
        from tools import write_approval as wa

        state_path = hermes_home / "cron" / "oauth_reauth_notify_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"zarkash": {"last_known_recorded_at": 1.0, "heads_up_sent_at": 2.0, "expired_sent_at": None}}),
            encoding="utf-8",
        )

        notices = await self._run(
            hermes_home, monkeypatch, event_text="bad-code", proc_returncode=1,
        )

        assert any("didn't work" in n.lower() or "try again" in n.lower() for n in notices)
        # Pending record must still exist -- NOT discarded on failure.
        assert wa.get_pending("oauth_reauth", "abc123") is not None

        # State must be untouched (not reset) on failure.
        reloaded = json.loads(state_path.read_text(encoding="utf-8"))
        assert reloaded["zarkash"]["heads_up_sent_at"] == 2.0

    async def test_no_extractable_code_sends_error_without_subprocess_call(self, hermes_home, monkeypatch):
        import subprocess as real_subprocess
        from tools import write_approval as wa

        _stage_oauth_pending(hermes_home, "abc123", "zarkash")

        called = []
        monkeypatch.setattr(real_subprocess, "run", lambda *a, **k: called.append(1))
        monkeypatch.setattr("gateway.run.get_hermes_home", lambda: hermes_home)

        from gateway.run import GatewayRunner, ReplyRefCheck

        fake_self = SimpleNamespace()
        notices = []

        async def _fake_notice(source, content):
            notices.append(content)

        fake_self._deliver_platform_notice = _fake_notice

        event = SimpleNamespace(text="I don't have the code yet", reply_to_text="[ref:oauth_reauth:abc123]")
        ref_check = ReplyRefCheck(tag_found=True, pending_exists=True, subsystem="oauth_reauth", pending_id="abc123")

        await GatewayRunner._handle_oauth_reauth_reply(fake_self, event, _FakeSource(), ref_check)

        assert called == []
        assert any("couldn't find" in n.lower() for n in notices)
        # Pending record must still exist.
        assert wa.get_pending("oauth_reauth", "abc123") is not None
