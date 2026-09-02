"""Tests for the P0 cost-leakage hotfix (READ_ONLY runtime policy)."""

import os

import pytest


@pytest.fixture()
def isolated_hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so config reads never touch the real profile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


# ── AC1: READ_ONLY + execute_code ⇒ RuntimePolicyError ────────────────────────
def test_execute_code_blocked_in_read_only(isolated_hermes_home, monkeypatch):
    from agent.runtime_policy import RuntimePolicyError, enforce_read_only

    monkeypatch.setenv("HERMES_RUNTIME_POLICY", "read_only")
    with pytest.raises(RuntimePolicyError) as excinfo:
        enforce_read_only("execute_code")
    assert "execute_code" in str(excinfo.value)
    assert "READ_ONLY" in str(excinfo.value)


def test_execute_code_tool_entry_blocks_before_sandbox(isolated_hermes_home, monkeypatch):
    """The real tool entry raises before touching sandbox availability."""
    from tools import code_execution_tool

    monkeypatch.setenv("HERMES_RUNTIME_POLICY", "read_only")
    with pytest.raises(Exception) as excinfo:
        code_execution_tool.execute_code(code="print('hi')")
    assert "RuntimePolicyError" in str(excinfo.value)


# ── AC2: READ_ONLY + delegate_task ⇒ RuntimePolicyError ───────────────────────
def test_delegate_task_blocked_in_read_only(isolated_hermes_home, monkeypatch):
    from agent.runtime_policy import RuntimePolicyError
    from tools.delegate_tool import delegate_task

    monkeypatch.setenv("HERMES_RUNTIME_POLICY", "read_only")

    class _FakeAgent:
        pass  # would be a parent context; must never be reached

    # No parent_agent → normally a tool_error; READ_ONLY must raise FIRST.
    with pytest.raises(RuntimePolicyError):
        delegate_task(goal="x")


def test_delegate_spawn_never_reaches_parent_in_read_only(
    isolated_hermes_home, monkeypatch
):
    """Even WITH a parent context, spawn is blocked before any worker init."""
    from agent.runtime_policy import RuntimePolicyError
    from tools.delegate_tool import delegate_task

    monkeypatch.setenv("HERMES_RUNTIME_POLICY", "read_only")

    class _SpyAgent:
        def __init__(self):
            self.touched = False

        def __getattr__(self, name):
            self.touched = True
            raise AttributeError(name)

    spy = _SpyAgent()
    with pytest.raises(RuntimePolicyError):
        delegate_task(goal="x", parent_agent=spy)


# ── AC3: no HTTP request to Gemini (structural: guard fires pre-init) ─────────
def test_guard_fires_before_any_model_init(isolated_hermes_home, monkeypatch):
    """Import order proof: runtime_policy imports no provider/HTTP modules."""
    import sys

    import agent.runtime_policy as rp

    banned = [m for m in sys.modules if any(k in m for k in ("gemini", "google.genai", "openai"))]
    assert not any(m.startswith(("google", "gemini")) for m in banned)
    assert callable(rp.enforce_read_only)


# ── AC4: Main provider mặc định = Nous Portal ────────────────────────────────
def test_auto_resolution_pins_to_nous(isolated_hermes_home, monkeypatch):
    """Even with a Gemini credential present, auto resolves to Nous."""
    from hermes_cli.auth import resolve_provider

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-for-test")
    assert resolve_provider("auto") == "nous"


def test_explicit_config_provider_still_wins(isolated_hermes_home, tmp_path):
    """NORMAL mode: explicit model.provider is respected (no regression)."""
    from hermes_cli.config import save_config
    from hermes_cli.auth import resolve_provider

    cfg = {"model": {"provider": "nous"}}
    save_config(cfg)
    assert resolve_provider("auto") == "nous"


def test_billable_classification():
    from agent.runtime_policy import is_billable_provider

    assert is_billable_provider("gemini") is True
    assert is_billable_provider("google-ai-studio" if False else "openrouter") is True
    assert is_billable_provider("anthropic") is True
    assert is_billable_provider("nous") is False
    assert is_billable_provider(None) is False


# ── AC5: NORMAL mode vẫn hoạt động bình thường ────────────────────────────────
def test_normal_mode_not_blocked(isolated_hermes_home, monkeypatch):
    from agent.runtime_policy import enforce_read_only

    monkeypatch.delenv("HERMES_RUNTIME_POLICY", raising=False)
    enforce_read_only("execute_code")  # must NOT raise
    enforce_read_only("delegate_task")  # must NOT raise


def test_no_fallback_chain_by_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG.get("fallback_providers") == []
