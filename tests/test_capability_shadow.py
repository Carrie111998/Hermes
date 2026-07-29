import logging

import pytest

from capability.decision import PermissionDecision, check_capability
from capability.mapping import load_tool_mapping, resolve_capability
from capability.policy import load_profile_policy
from capability.source import load_sources, resolve_source


@pytest.fixture(autouse=True)
def clear_capability_caches():
    load_tool_mapping.cache_clear()
    load_profile_policy.cache_clear()
    load_sources.cache_clear()
    yield
    load_tool_mapping.cache_clear()
    load_profile_policy.cache_clear()
    load_sources.cache_clear()


def test_shipped_mapping_and_source_resolution():
    assert resolve_capability("terminal") == "execution.command"
    assert resolve_source("codex", "terminal") == "execution.command"
    assert resolve_capability("missing") is None


def test_unknown_capability_requires_approval():
    assert (
        check_capability(profile="default", capability=None)
        is PermissionDecision.APPROVAL_REQUIRED
    )
    assert (
        check_capability(profile="default", capability="missing")
        is PermissionDecision.APPROVAL_REQUIRED
    )


def test_policy_profile_cannot_escape_directory(tmp_path, monkeypatch):
    import capability.policy as policy

    policy_dir = tmp_path / "policies"
    policy_dir.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text(
        "filesystem.read:\n  decision: DENY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(policy, "POLICY_DIR", policy_dir)

    assert load_profile_policy("../outside") == {}


def test_invalid_policy_decision_requires_approval(monkeypatch):
    monkeypatch.setattr(
        "capability.decision.get_policy_decision",
        lambda *_: "INVALID",
    )
    assert (
        check_capability(profile="default", capability="filesystem.read")
        is PermissionDecision.APPROVAL_REQUIRED
    )


def test_shadow_log_contains_metadata_not_arguments(caplog, monkeypatch):
    import capability.shadow_hook as shadow_hook

    monkeypatch.setattr(
        "agent.file_safety._resolve_active_profile_name",
        lambda: "default",
    )
    caplog.set_level(logging.INFO, logger=shadow_hook.__name__)

    decision = shadow_hook.observe_tool_call(function_name="terminal")

    assert decision is PermissionDecision.APPROVAL_REQUIRED
    assert "runtime=hermes" in caplog.text
    assert "tool=terminal" in caplog.text
    assert "args=" not in caplog.text


def test_codex_shadow_uses_active_profile(caplog, monkeypatch):
    import capability.codex_hook as codex_hook

    monkeypatch.setattr(
        "agent.file_safety._resolve_active_profile_name",
        lambda: "tech-ops",
    )
    caplog.set_level(logging.INFO, logger=codex_hook.__name__)

    decision = codex_hook.observe_codex_tool(tool="terminal")

    assert decision is PermissionDecision.APPROVAL_REQUIRED
    assert "profile=tech-ops" in caplog.text
