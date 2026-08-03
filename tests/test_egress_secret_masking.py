"""E2E wiring tests: applied external secrets are masked at provider egress.

External secret sources (Bitwarden / 1Password / command) apply values
under arbitrary names (``DATABASE_URL``, ``FOO``, arbitrary 1Password item
keys) via ``hermes_cli.env_loader.get_secret_source_values``. Shape-based
redaction cannot catch those opaque values. These tests prove the per-home
applied-secrets snapshot is wired into the memory-context sanitizer and the
pre-send assistant-content sanitizer, and that failures are best-effort.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.chat_completion_helpers import build_assistant_message  # noqa: E402
from agent.context_engine import sanitize_memory_context  # noqa: E402
from tools.process_registry import _redact_process_result  # noqa: E402

# No vendor prefix, no recognisable shape -- invisible to regex redaction,
# exactly like an opaque value applied under an arbitrary secret name.
OPAQUE = "abc123randomstring"


def _fake_applied_snapshot(monkeypatch, tmp_path, snapshot):
    """Point egress snapshot resolution at a fake per-home applied snapshot."""
    monkeypatch.setattr(
        "hermes_cli.env_loader.get_secret_source_values",
        lambda home: snapshot,
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)


def test_sanitize_memory_context_masks_applied_secret(monkeypatch, tmp_path):
    _fake_applied_snapshot(monkeypatch, tmp_path, {"DATABASE_URL": OPAQUE})
    out = sanitize_memory_context(f"connecting to {OPAQUE}")
    assert OPAQUE not in out
    assert "***" in out


def test_sanitize_memory_context_does_not_mask_other_homes_secrets(monkeypatch, tmp_path):
    # Per-home scoping: only THIS home's applied values are masked. A
    # snapshot registered under a different home must not leak into egress.
    other_home = tmp_path / "other"

    def _snapshot(home):
        return {"DATABASE_URL": OPAQUE} if str(home) == str(other_home) else {}

    monkeypatch.setattr("hermes_cli.env_loader.get_secret_source_values", _snapshot)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    out = sanitize_memory_context(f"connecting to {OPAQUE}")
    assert OPAQUE in out


def test_sanitize_memory_context_survives_resolution_failure(monkeypatch, tmp_path):
    # Best-effort: any exception in snapshot resolution must never break
    # the sanitizer call -- it falls back to shape-based redaction alone.
    def _boom(_home):
        raise RuntimeError("secrets backend unavailable")

    monkeypatch.setattr("hermes_cli.env_loader.get_secret_source_values", _boom)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    assert sanitize_memory_context("plain memory context") == "plain memory context"


class _FakeAssistantMsg:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None
        self.function_call = None
        self.reasoning_content = None
        self.model_extra = None
        self.reasoning_details = None

    def __getattr__(self, _name):
        return None


class _FakeAgent:
    stream_delta_callback = None
    _stream_callback = None
    reasoning_callback = None
    verbose_logging = False

    def _extract_reasoning(self, _msg):
        return None

    def _strip_think_blocks(self, text):
        return text

    def _needs_thinking_reasoning_pad(self):
        return False


def test_build_assistant_message_masks_applied_secret(monkeypatch, tmp_path):
    # The pre-send assistant-content sanitizer (the seam at
    # chat_completion_helpers.py ~:1457) must mask this home's applied
    # secret values before the message enters conversation history.
    _fake_applied_snapshot(monkeypatch, tmp_path, {"DATABASE_URL": OPAQUE})
    msg = build_assistant_message(
        _FakeAgent(), _FakeAssistantMsg(f"the token {OPAQUE} leaked"), "stop"
    )
    assert OPAQUE not in msg["content"]
    assert "***" in msg["content"]


def test_redact_process_result_masks_applied_secret(monkeypatch, tmp_path):
    # Background-process output (poll/log/wait) passes through
    # redact_terminal_output; the applied snapshot must be wired there too.
    _fake_applied_snapshot(monkeypatch, tmp_path, {"DATABASE_URL": OPAQUE})
    result = _redact_process_result(
        {"command": "cat x", "output": f"the token {OPAQUE} leaked"}
    )
    assert OPAQUE not in result["output"]
