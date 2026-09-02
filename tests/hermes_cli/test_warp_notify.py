import json
import os

import pytest

from hermes_cli import warp_notify


def _clear_warp_env(monkeypatch):
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("WARP_CLI_AGENT_PROTOCOL_VERSION", raising=False)
    monkeypatch.delenv("WARP_CLIENT_VERSION", raising=False)


def _set_warp_env(monkeypatch, *, protocol_version="1", client_version="v0.2026.08.01.00.00.stable_01"):
    monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
    monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", protocol_version)
    monkeypatch.setenv("WARP_CLIENT_VERSION", client_version)


class TestShouldNotify:
    def test_false_with_no_env(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        assert warp_notify._should_notify() is False

    def test_false_outside_warp(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", "1")
        monkeypatch.setenv("WARP_CLIENT_VERSION", "v0.2026.08.01.00.00.stable_01")
        # TERM_PROGRAM still unset/wrong -> must stay silent even though
        # Warp's own vars are present (e.g. a nested shell inheriting env).
        assert warp_notify._should_notify() is False
        monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
        assert warp_notify._should_notify() is False

    def test_false_missing_protocol_version(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
        monkeypatch.setenv("WARP_CLIENT_VERSION", "v0.2026.08.01.00.00.stable_01")
        assert warp_notify._should_notify() is False

    def test_false_missing_client_version(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        monkeypatch.setenv("TERM_PROGRAM", "WarpTerminal")
        monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", "1")
        assert warp_notify._should_notify() is False

    def test_true_with_full_env(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        _set_warp_env(monkeypatch)
        assert warp_notify._should_notify() is True


class TestNegotiateProtocolVersion:
    def test_defaults_to_one_when_unset(self, monkeypatch):
        monkeypatch.delenv("WARP_CLI_AGENT_PROTOCOL_VERSION", raising=False)
        assert warp_notify._negotiate_protocol_version() == 1

    def test_min_of_ours_and_warps(self, monkeypatch):
        monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", "5")
        assert warp_notify._negotiate_protocol_version() == min(5, warp_notify._PROTOCOL_VERSION)

    def test_falls_back_on_garbage_value(self, monkeypatch):
        monkeypatch.setenv("WARP_CLI_AGENT_PROTOCOL_VERSION", "not-a-number")
        assert warp_notify._negotiate_protocol_version() == 1


class TestBuildPayload:
    def test_shape_and_defaults(self, monkeypatch):
        monkeypatch.delenv("WARP_CLI_AGENT_PROTOCOL_VERSION", raising=False)
        raw = warp_notify._build_payload("stop", session_id="abc123", cwd="/tmp/my-project")
        payload = json.loads(raw)
        assert payload == {
            "v": 1,
            "agent": "hermes",
            "event": "stop",
            "session_id": "abc123",
            "cwd": "/tmp/my-project",
            "project": "my-project",
        }

    def test_defaults_cwd_to_os_getcwd(self, monkeypatch):
        raw = warp_notify._build_payload("stop")
        payload = json.loads(raw)
        assert payload["cwd"] == os.getcwd()
        assert payload["project"] == os.path.basename(os.getcwd())

    def test_extra_kwargs_merge_in(self):
        raw = warp_notify._build_payload("permission_request", tool_name="Bash", summary="rm -rf /tmp/x")
        payload = json.loads(raw)
        assert payload["event"] == "permission_request"
        assert payload["tool_name"] == "Bash"
        assert payload["summary"] == "rm -rf /tmp/x"

    def test_is_valid_json_no_trailing_whitespace_separators(self):
        # Reference plugin uses jq -nc (compact); match that shape so any
        # terminal-side parser expecting a single-line OSC body doesn't choke.
        raw = warp_notify._build_payload("stop")
        assert "\n" not in raw
        assert ", " not in raw
        assert ": " not in raw


class TestEmitOsc777:
    def test_writes_expected_sequence_to_tty(self, monkeypatch, tmp_path):
        fake_tty = tmp_path / "fake_tty"

        real_open = open

        def fake_open(path, mode="r", *a, **kw):
            if path == "/dev/tty":
                return real_open(fake_tty, mode, *a, **kw)
            return real_open(path, mode, *a, **kw)

        monkeypatch.setattr("builtins.open", fake_open)
        warp_notify._emit_osc777("warp://cli-agent", '{"agent":"hermes"}')

        written = fake_tty.read_text()
        assert written == '\x1b]777;notify;warp://cli-agent;{"agent":"hermes"}\x07'

    def test_never_raises_when_tty_unavailable(self, monkeypatch):
        def raise_oserror(*a, **kw):
            raise OSError("no such device")

        monkeypatch.setattr("builtins.open", raise_oserror)
        # Must not raise -- a missing/unavailable tty is expected on CI,
        # over some SSH sessions, and in any non-interactive invocation.
        warp_notify._emit_osc777("warp://cli-agent", "{}")


class TestNotifyStop:
    def test_noop_outside_warp(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        called = []
        monkeypatch.setattr(warp_notify, "_emit_osc777", lambda *a: called.append(a))
        warp_notify.notify_stop(session_id="s1", query="hi", response="hello")
        assert called == []

    def test_emits_with_correct_event_and_truncation(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        _set_warp_env(monkeypatch)
        called = []
        monkeypatch.setattr(warp_notify, "_emit_osc777", lambda title, body: called.append((title, body)))

        long_query = "x" * 500
        warp_notify.notify_stop(session_id="s1", query=long_query, response="short")

        assert len(called) == 1
        title, body = called[0]
        assert title == "warp://cli-agent"
        payload = json.loads(body)
        assert payload["event"] == "stop"
        assert payload["agent"] == "hermes"
        assert payload["session_id"] == "s1"
        assert len(payload["query"]) == 200
        assert payload["response"] == "short"


class TestNotifyPermissionRequest:
    def test_noop_outside_warp(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        called = []
        monkeypatch.setattr(warp_notify, "_emit_osc777", lambda *a: called.append(a))
        warp_notify.notify_permission_request("Bash", summary="rm -rf /")
        assert called == []

    def test_emits_with_default_summary(self, monkeypatch):
        _clear_warp_env(monkeypatch)
        _set_warp_env(monkeypatch)
        called = []
        monkeypatch.setattr(warp_notify, "_emit_osc777", lambda title, body: called.append((title, body)))

        warp_notify.notify_permission_request("Bash", session_id="s2")

        assert len(called) == 1
        title, body = called[0]
        assert title == "warp://cli-agent"
        payload = json.loads(body)
        assert payload["event"] == "permission_request"
        assert payload["tool_name"] == "Bash"
        assert payload["summary"] == "Wants to run Bash"
        assert payload["session_id"] == "s2"
