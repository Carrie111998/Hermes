from types import SimpleNamespace

from agent.fallback_safety import (
    fallback_runtime_block_reason,
    fallback_tool_block_reason,
)


def _agent(entry=None, *, activated=True):
    chain = [entry] if entry is not None else []
    return SimpleNamespace(
        _fallback_activated=activated,
        _active_fallback_entry=entry,
        _fallback_chain=chain,
        _fallback_index=1 if chain else 0,
        provider="openai-api",
        model="local-fallback",
    )


def test_inactive_fallback_does_not_restrict_tools():
    agent = _agent({"safety_mode": "read_only"}, activated=False)
    assert fallback_tool_block_reason(agent, "terminal", {"command": "rm -rf /tmp/x"}) is None


def test_legacy_entry_without_safety_mode_is_unrestricted():
    agent = _agent({"provider": "openai-api", "model": "fallback"})
    assert fallback_tool_block_reason(agent, "terminal", {"command": "touch /tmp/x"}) is None


def test_read_only_entry_allows_inspection_and_blocks_mutation():
    entry = {
        "provider": "openai-api",
        "model": "fallback",
        "safety_mode": "read_only",
    }
    agent = _agent(entry)
    assert fallback_tool_block_reason(agent, "read_file", {"path": "/tmp/x"}) is None
    assert fallback_tool_block_reason(agent, "process", {"action": "poll"}) is None
    assert fallback_tool_block_reason(agent, "process", {"action": "kill"}) is not None
    assert fallback_tool_block_reason(agent, "terminal", {"command": "true"}) is not None


def test_unknown_safety_mode_fails_closed():
    agent = _agent({"provider": "custom", "model": "x", "safety_mode": "mystery"})
    assert fallback_tool_block_reason(agent, "write_file", {"path": "/tmp/x"}) is not None


def test_explicit_unrestricted_mode_preserves_legacy_behavior():
    agent = _agent({"provider": "custom", "model": "x", "safety_mode": "full"})
    assert fallback_tool_block_reason(agent, "write_file", {"path": "/tmp/x"}) is None


def test_bounded_entry_rejects_embedded_execution_runtime():
    entry = {"provider": "custom", "model": "x", "safety_mode": "read_only"}
    reason = fallback_runtime_block_reason(entry, "codex_app_server")
    assert reason is not None
    assert "outside Hermes tool dispatch" in reason


def test_bounded_entry_rejects_embedded_execution_provider_alias():
    entry = {
        "provider": "github-copilot-acp",
        "model": "x",
        "safety_mode": "read_only",
    }
    assert fallback_runtime_block_reason(entry, "chat_completions") is not None


def test_unrestricted_entry_allows_embedded_runtime():
    entry = {"provider": "custom", "model": "x", "safety_mode": "full"}
    assert fallback_runtime_block_reason(entry, "codex_app_server") is None
