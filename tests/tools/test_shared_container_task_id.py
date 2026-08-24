"""
Regression tests for the shared-container task_id mapping.

Ordinary persistent Docker calls share a profile's ``"default"`` sandbox, SSH
sessions use separate cache slots so profiles cannot share remote connections,
and RL/benchmark environments opt in to isolation with explicit overrides.
"""

import pytest

from tools import terminal_tool


@pytest.fixture(autouse=True)
def _clean_overrides():
    """Ensure no stray overrides from other tests leak in."""
    before = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    yield
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before)


def _select_backend(monkeypatch, backend: str) -> None:
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    monkeypatch.setenv("TERMINAL_ENV", backend)


def test_none_task_id_maps_to_default(monkeypatch):
    _select_backend(monkeypatch, "local")
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_empty_task_id_maps_to_default(monkeypatch):
    _select_backend(monkeypatch, "local")
    assert terminal_tool._resolve_container_task_id("") == "default"


def test_cwd_only_override_collapses_to_default(monkeypatch):
    """CWD-only tracking must not fragment an ordinary persistent sandbox."""
    _select_backend(monkeypatch, "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    terminal_tool.register_task_env_overrides(
        "acp-session-abc", {"cwd": "/home/user/project"}
    )
    try:
        assert terminal_tool._resolve_container_task_id("acp-session-abc") == "default"
    finally:
        terminal_tool.clear_task_env_overrides("acp-session-abc")


def test_env_type_override_keeps_own_id(monkeypatch):
    """env_type is an isolation key and must trigger a per-task environment."""
    _select_backend(monkeypatch, "docker")
    terminal_tool.register_task_env_overrides(
        "bench-env", {"env_type": "sandbox", "cwd": "/work"}
    )
    try:
        assert terminal_tool._resolve_container_task_id("bench-env") == "bench-env"
    finally:
        terminal_tool.clear_task_env_overrides("bench-env")


def test_ssh_session_key_scopes_to_its_own_slot(monkeypatch):
    _select_backend(monkeypatch, "ssh")
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    assert terminal_tool._resolve_container_task_id(None) == "session:sess-A"


def test_distinct_ssh_session_keys_get_distinct_slots(monkeypatch):
    _select_backend(monkeypatch, "ssh")
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    first = terminal_tool._resolve_container_task_id(None)
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-B")
    second = terminal_tool._resolve_container_task_id(None)
    assert first == "session:sess-A"
    assert second == "session:sess-B"
    assert first != second


def test_ssh_subagent_collapses_onto_parent_session(monkeypatch):
    _select_backend(monkeypatch, "ssh")
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    assert (
        terminal_tool._resolve_container_task_id("subagent-3-cafef00d")
        == "session:sess-A"
    )


def test_rl_override_wins_over_ssh_session_key(monkeypatch):
    _select_backend(monkeypatch, "ssh")
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-A")
    terminal_tool.register_task_env_overrides("tb2-z", {"docker_image": "z:1"})
    try:
        assert terminal_tool._resolve_container_task_id("tb2-z") == "tb2-z"
    finally:
        terminal_tool.clear_task_env_overrides("tb2-z")


def test_no_session_key_still_defaults(monkeypatch):
    _select_backend(monkeypatch, "ssh")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    assert terminal_tool._resolve_container_task_id(None) == "default"


def test_persistent_docker_session_key_still_shares_default(monkeypatch):
    _select_backend(monkeypatch, "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")
    monkeypatch.setenv("HERMES_SESSION_KEY", "agent:main:telegram:dm:123")

    assert terminal_tool._resolve_container_task_id(None) == "default"
    assert terminal_tool._resolve_container_task_id("subagent-1-cafe") == "default"


def test_session_key_from_contextvar_without_environ(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    _select_backend(monkeypatch, "ssh")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    tokens = set_session_vars(session_key="sess-ctx")
    try:
        assert terminal_tool._resolve_container_task_id(None) == "session:sess-ctx"
        assert (
            terminal_tool._resolve_container_task_id("subagent-1-cafe")
            == "session:sess-ctx"
        )
    finally:
        clear_session_vars(tokens)


def test_contextvar_session_key_wins_over_environ(monkeypatch):
    from gateway.session_context import clear_session_vars, set_session_vars

    _select_backend(monkeypatch, "ssh")
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess-ENV")
    tokens = set_session_vars(session_key="sess-CTX")
    try:
        assert terminal_tool._resolve_container_task_id(None) == "session:sess-CTX"
    finally:
        clear_session_vars(tokens)
