"""Behavioral tests for the pure string helpers in hermes_cli._scan_venv_blockers."""

import pytest

from hermes_cli._scan_venv_blockers import (
    _find_flag,
    _is_pausable_gateway,
    _redact_sensitive_cmdline,
)


# ── _find_flag ────────────────────────────────────────────────────────────────

class TestFindFlag:
    def test_finds_flag_at_start(self):
        assert _find_flag("--model gpt-5 --verbose", "--model") == 0

    def test_finds_flag_mid_string(self):
        idx = _find_flag("hermes serve --model gpt-5", "--model")
        assert idx > 0

    def test_returns_minus1_when_absent(self):
        assert _find_flag("hermes serve --verbose", "--model") == -1

    def test_flag_not_matched_as_substring_of_value(self):
        # "--model" must not match a path/value that embeds "--model" mid-word,
        # e.g. "/some--model-thing" (no preceding space)
        idx = _find_flag("/some--model-thing", "--model")
        assert idx == -1

    def test_flag_matches_at_word_boundary_even_with_suffix(self):
        # _find_flag checks that the flag starts at position 0 or after a space;
        # callers pass the flag with " " or "=" appended when they need to
        # distinguish --model from --model-extra. The raw helper returns the
        # word-start position regardless of what follows.
        idx = _find_flag("hermes --model-extra foo", "--model")
        assert idx == len("hermes ")

    def test_empty_text(self):
        assert _find_flag("", "--model") == -1

    def test_empty_flag(self):
        # empty flag: present at position 0
        assert _find_flag("anything", "") >= 0


# ── _redact_sensitive_cmdline ─────────────────────────────────────────────────

class TestRedactSensitiveCmdline:
    def test_redacts_api_key_value(self):
        cmd = "hermes serve --api-key sk-realkey123 --model gpt-5"
        result = _redact_sensitive_cmdline(cmd)
        assert "sk-realkey123" not in result
        assert "--api-key" in result

    def test_redacts_token_value(self):
        cmd = "hermes serve --token my-secret-token --port 8080"
        result = _redact_sensitive_cmdline(cmd)
        assert "my-secret-token" not in result

    def test_redacts_password_value(self):
        cmd = "hermes serve --password hunter2 --host localhost"
        result = _redact_sensitive_cmdline(cmd)
        assert "hunter2" not in result

    def test_non_sensitive_args_preserved(self):
        cmd = "hermes serve --model gpt-5 --port 8080"
        result = _redact_sensitive_cmdline(cmd)
        assert "--model" in result
        assert "gpt-5" in result
        assert "--port" in result
        assert "8080" in result

    def test_empty_cmdline(self):
        assert _redact_sensitive_cmdline("") == ""


# ── _is_pausable_gateway ──────────────────────────────────────────────────────

class TestIsPausableGateway:
    def test_recognizes_gateway_run(self):
        assert _is_pausable_gateway("hermes gateway run") is True

    def test_recognizes_gateway_run_with_flags(self):
        assert _is_pausable_gateway("/usr/bin/hermes gateway run --port 8080") is True

    def test_rejects_hermes_serve(self):
        assert _is_pausable_gateway("hermes serve") is False

    def test_rejects_hermes_dashboard(self):
        assert _is_pausable_gateway("hermes dashboard") is False

    def test_rejects_empty_cmdline(self):
        assert _is_pausable_gateway("") is False

    def test_rejects_partial_gateway_no_run(self):
        # "gateway" without "run" is not a pausable gateway process
        assert _is_pausable_gateway("hermes gateway status") is False
