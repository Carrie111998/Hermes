"""Regression tests for suppressing the built-in file-memory writer.

The built-in ``memory`` tool writes to MEMORY.md / USER.md. External memory
providers (Hindsight, etc.) are additive and should remain usable when the
built-in writer is hidden from the model.
"""

from types import SimpleNamespace

import yaml


def _tool_names(tool_defs):
    return {tool.get("function", {}).get("name") for tool in tool_defs}


def _write_config(memory_config):
    from hermes_cli.config import get_config_path

    path = get_config_path()
    path.write_text(yaml.safe_dump({"memory": memory_config}), encoding="utf-8")
    return path


def _reset_initial_state():
    """Establish one clean baseline; transition assertions must not call this."""
    import hermes_cli.config as config
    import model_tools
    from tools.registry import invalidate_check_fn_cache

    config._LOAD_CONFIG_CACHE.clear()
    invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()


def test_builtin_memory_writer_available_by_default():
    """Back-compat: existing configs keep exposing the built-in writer."""
    from model_tools import get_tool_definitions

    _reset_initial_state()
    tools = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)

    assert "memory" in _tool_names(tools)


def test_builtin_memory_writer_config_flip_is_immediate_without_cache_clear():
    """Both flip directions bypass generic check_fn grace and schema-cache staleness."""
    from model_tools import get_tool_definitions
    from tools.memory_tool import check_memory_requirements

    _write_config({"builtin_writer_enabled": True})
    _reset_initial_state()
    enabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert "memory" in _tool_names(enabled)

    _write_config({"builtin_writer_enabled": False})
    disabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert check_memory_requirements() is True
    assert "memory" not in _tool_names(disabled)

    _write_config({"builtin_writer_enabled": True})
    reenabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert "memory" in _tool_names(reenabled)


def test_builtin_memory_writer_env_reference_changes_cache_key(monkeypatch):
    """The effective bool, not only config.yaml stat, keys schema caching."""
    from model_tools import get_tool_definitions

    monkeypatch.setenv("HERMES_TEST_BUILTIN_WRITER", "true")
    _write_config({"builtin_writer_enabled": "${HERMES_TEST_BUILTIN_WRITER}"})
    _reset_initial_state()
    enabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert "memory" in _tool_names(enabled)

    monkeypatch.setenv("HERMES_TEST_BUILTIN_WRITER", "false")
    disabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert "memory" not in _tool_names(disabled)


def test_malformed_config_hides_builtin_memory_writer():
    """A broken policy file must not fall back to exposing a mutating tool."""
    from hermes_cli.config import get_config_path
    from model_tools import get_tool_definitions

    get_config_path().write_text("memory: [unterminated", encoding="utf-8")
    _reset_initial_state()

    tools = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)

    assert "memory" not in _tool_names(tools)


def test_builtin_memory_writer_managed_override_changes_cache_key(tmp_path, monkeypatch):
    """Managed-scope edits take effect even when user config.yaml is unchanged."""
    from hermes_cli import managed_scope
    from model_tools import get_tool_definitions

    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    _write_config({"builtin_writer_enabled": True})
    (managed_dir / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"builtin_writer_enabled": True}}),
        encoding="utf-8",
    )
    managed_scope.invalidate_managed_cache()
    _reset_initial_state()
    enabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert "memory" in _tool_names(enabled)

    (managed_dir / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"builtin_writer_enabled": False}}),
        encoding="utf-8",
    )
    managed_scope.invalidate_managed_cache()
    disabled = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=True)
    assert "memory" not in _tool_names(disabled)


def test_non_quiet_definitions_apply_builtin_writer_policy():
    from model_tools import get_tool_definitions

    _write_config({"builtin_writer_enabled": False})
    _reset_initial_state()

    tools = get_tool_definitions(enabled_toolsets=["memory"], quiet_mode=False)

    assert "memory" not in _tool_names(tools)


def test_memory_provider_tools_still_inject_when_builtin_writer_disabled():
    """Provider tools are keyed by the memory toolset, not by the built-in writer."""
    import importlib

    import pytest

    memory_manager = importlib.import_module("agent.memory_manager")
    inject_memory_provider_tools = getattr(memory_manager, "inject_memory_provider_tools", None)
    if inject_memory_provider_tools is None:
        pytest.skip("this live branch injects provider tools inline in agent_init")

    class DummyMemoryManager:
        def get_all_tool_schemas(self):
            return [
                {
                    "name": "hindsight_recall",
                    "description": "Recall memories from Hindsight.",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "name": "hindsight_retain",
                    "description": "Retain memories in Hindsight.",
                    "parameters": {"type": "object", "properties": {}},
                },
            ]

    agent = SimpleNamespace(
        enabled_toolsets=["memory"],
        tools=[],
        valid_tool_names=set(),
        _memory_manager=DummyMemoryManager(),
    )

    added = inject_memory_provider_tools(agent)

    assert added == 2
    assert "memory" not in agent.valid_tool_names
    assert {"hindsight_recall", "hindsight_retain"} <= agent.valid_tool_names


def test_builtin_prompt_memory_remains_injected_when_writer_tool_absent():
    """Read-injected MEMORY.md / USER.md bootloader is independent of writer tool."""
    from agent.system_prompt import build_system_prompt_parts
    from hermes_constants import get_hermes_home
    from tools.memory_tool import ENTRY_DELIMITER, MemoryStore

    memories_dir = get_hermes_home() / "memories"
    (memories_dir / "MEMORY.md").write_text(
        ENTRY_DELIMITER.join(["Compact bootloader guardrail"]),
        encoding="utf-8",
    )
    (memories_dir / "USER.md").write_text(
        ENTRY_DELIMITER.join(["User compact profile fact"]),
        encoding="utf-8",
    )
    store = MemoryStore()
    store.load_from_disk()

    agent = SimpleNamespace(
        load_soul_identity=False,
        skip_context_files=True,
        valid_tool_names=set(),
        provider="test-provider",
        model="test-model",
        platform="cli",
        _tool_use_enforcement=False,
        _environment_probe=False,
        _memory_store=store,
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_manager=None,
        pass_session_id=False,
        session_id="test-session",
    )

    volatile = build_system_prompt_parts(agent)["volatile"]

    assert "MEMORY (your personal notes)" in volatile
    assert "Compact bootloader guardrail" in volatile
    assert "USER PROFILE (who the user is)" in volatile
    assert "User compact profile fact" in volatile
