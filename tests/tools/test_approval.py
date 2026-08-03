"""Exact, non-semantic approval authority contracts."""

from __future__ import annotations

import builtins
import hashlib
import json
import os
import threading
import time
from unittest.mock import patch

from tools import approval


class TestApprovalModeParsing:
    def test_unquoted_yaml_off_boolean_maps_to_off(self):
        with patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"approvals": {"mode": False}},
        ):
            assert approval._get_approval_mode() == "off"

    def test_supported_modes_are_normalized_without_semantic_routing(self):
        assert approval._normalize_approval_mode("manual") == "manual"
        assert approval._normalize_approval_mode("smart") == "manual"
        assert approval._normalize_approval_mode("  OFF  ") == "off"

    def test_unknown_mode_defaults_to_manual(self):
        with patch.object(approval.logger, "warning") as warning:
            assert approval._normalize_approval_mode("auto") == "manual"
            warning.assert_called_once()


def test_manual_callback_receives_exact_unmodified_bytes(monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_is_cron_session", lambda: False)
    monkeypatch.setattr(
        approval,
        "check_exact_execution_authority",
        lambda *_args, **_kwargs: None,
    )
    raw = 'printf "%s" "sk-exact-owner-visible-value"'
    observed = []

    def approve_once(command, _description, **kwargs):
        observed.append((command, kwargs))
        return "once"

    result = approval.check_all_command_guards(
        raw,
        "local",
        approval_callback=approve_once,
    )

    assert result["approved"] is True
    assert observed[0][0] == raw
    assert observed[0][1]["exact_execution"] is True


def test_manual_callback_timeout_is_distinct_from_explicit_denial(monkeypatch):
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_is_cron_session", lambda: False)
    monkeypatch.setattr(
        approval,
        "check_exact_execution_authority",
        lambda *_args, **_kwargs: None,
    )

    result = approval.check_all_command_guards(
        "opaque exact bytes",
        "local",
        approval_callback=lambda *_args, **_kwargs: "timeout",
    )

    assert result["approved"] is False
    assert result["outcome"] == "timeout"
    assert result["error_code"] == "exact_execution_timeout"
    assert "Silence is not consent" in result["message"]


def test_mcp_cli_elicitation_is_exact_one_operation(monkeypatch):
    observed = {}

    def prompt(command, description, **kwargs):
        observed.update(command=command, description=description, **kwargs)
        return "once"

    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", prompt)

    result = approval.request_elicitation_consent(
        "exact MCP request bytes",
        "MCP server requests one explicit consent",
    )

    assert result == "accept"
    assert observed["command"] == "exact MCP request bytes"
    assert observed["allow_permanent"] is False
    assert observed["allow_session"] is False
    assert observed["exact_execution"] is True
    assert len(observed["approval_id"]) == 32
    int(observed["approval_id"], 16)


def test_exact_fallback_prompt_rejects_session_choice(monkeypatch):
    monkeypatch.setattr(builtins, "input", lambda _prompt: "session")

    result = approval.prompt_dangerous_approval(
        "opaque exact bytes",
        "one exact operation",
        timeout_seconds=1,
        allow_permanent=False,
        allow_session=False,
        approval_id="a" * 32,
        exact_execution=True,
    )

    assert result == "deny"


def test_exact_fallback_presents_canonical_bytes_before_once(
    monkeypatch,
    capsys,
):
    raw = "  first\nlast  "
    monkeypatch.setattr(builtins, "input", lambda _prompt: "once")

    result = approval.prompt_dangerous_approval(
        raw,
        "one exact operation",
        timeout_seconds=1,
        allow_permanent=False,
        allow_session=False,
        approval_id="c" * 32,
        exact_execution=True,
    )

    output = capsys.readouterr().out
    canonical = json.dumps(raw, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert result == "once"
    assert canonical in output
    assert f"SHA-256: {digest}" in output
    assert output.index(canonical) < output.index("[o]nce")


def test_exact_command_digest_preserves_boundary_whitespace():
    raw = "  first\nlast  "

    assert approval._exact_command_sha256(raw) == hashlib.sha256(raw.encode("utf-8")).hexdigest()


class TestApprovalTimeoutIsNotConsent:
    session_key = "test-no-consent-session"

    def setup_method(self):
        approval._gateway_queues.clear()
        approval._gateway_notify_cbs.clear()
        self.saved_env = {
            key: os.environ.get(key)
            for key in (
                "HERMES_GATEWAY_SESSION",
                "HERMES_CRON_SESSION",
                "HERMES_YOLO_MODE",
                "HERMES_SESSION_KEY",
                "HERMES_INTERACTIVE",
            )
        }
        os.environ.pop("HERMES_YOLO_MODE", None)
        os.environ.pop("HERMES_INTERACTIVE", None)
        os.environ.pop("HERMES_CRON_SESSION", None)
        os.environ["HERMES_GATEWAY_SESSION"] = "1"
        os.environ["HERMES_SESSION_KEY"] = self.session_key
        self.session_token = approval.set_current_session_key(self.session_key)

    def teardown_method(self):
        approval._gateway_queues.clear()
        approval._gateway_notify_cbs.clear()
        approval.reset_current_session_key(self.session_token)
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @staticmethod
    def _timeout(monkeypatch, seconds):
        monkeypatch.setattr(
            approval,
            "_get_approval_config",
            lambda: {"mode": "manual", "timeout": seconds},
        )

    def test_timeout_is_explicit_no_consent(self, monkeypatch):
        self._timeout(monkeypatch, 1)
        notified = []
        approval.register_gateway_notify(self.session_key, notified.append)

        result = approval.check_all_command_guards("opaque bytes", "local")

        assert result["approved"] is False
        assert result["user_consent"] is False
        assert result["outcome"] == "timeout"
        assert result["error_code"] == "exact_execution_timeout"
        assert "Silence is not consent" in result["message"]
        assert len(notified) == 1

    def test_exact_deny_resolves_only_by_opaque_id(self, monkeypatch):
        self._timeout(monkeypatch, 60)
        notified = []
        approval.register_gateway_notify(self.session_key, notified.append)
        holder = {}

        def check():
            holder["result"] = approval.check_all_command_guards(
                "opaque bytes",
                "local",
            )

        thread = threading.Thread(target=check)
        thread.start()
        for _ in range(100):
            if notified:
                break
            time.sleep(0.01)
        assert notified
        assert approval.resolve_gateway_approval(self.session_key, "deny") == 0
        assert approval.resolve_gateway_approval_by_id(
            self.session_key,
            notified[0]["approval_id"],
            "deny",
        ) == 1
        thread.join(timeout=5)

        result = holder["result"]
        assert result["approved"] is False
        assert result["user_consent"] is False
        assert result["outcome"] == "denied"
        assert "Silence is not consent" not in result["message"]

    def test_mcp_elicitation_uses_one_exact_gateway_capability(self, monkeypatch):
        self._timeout(monkeypatch, 60)
        notified = []
        approval.register_gateway_notify(self.session_key, notified.append)
        holder = {}

        def request():
            holder["result"] = approval.request_elicitation_consent(
                "exact MCP request bytes",
                "MCP server requests one explicit consent",
            )

        thread = threading.Thread(target=request)
        thread.start()
        for _ in range(100):
            if notified:
                break
            time.sleep(0.01)

        assert notified
        payload = notified[0]
        assert payload["exact_execution"] is True
        assert payload["allow_session"] is False
        assert payload["allow_permanent"] is False
        assert payload["command"] == "exact MCP request bytes"
        assert approval.resolve_gateway_approval_by_id(
            self.session_key,
            payload["approval_id"],
            "once",
        ) == 1
        thread.join(timeout=5)

        assert holder["result"] == "accept"
