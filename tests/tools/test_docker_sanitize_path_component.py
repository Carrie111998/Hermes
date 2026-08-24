"""Regression coverage for Docker sandbox path components."""

from tools.environments.docker import _sanitize_path_component


def test_sanitize_path_component_preserves_safe_kanban_id():
    assert _sanitize_path_component("t_50621cac") == "t_50621cac"


def test_sanitize_path_component_replaces_docker_mount_delimiters():
    component = _sanitize_path_component("session:20260822_232116_cef735/child")

    assert component == "session_20260822_232116_cef735_child"
    assert ":" not in component
    assert "/" not in component


def test_sanitize_path_component_replaces_interactive_chat_session_colon():
    component = _sanitize_path_component("session:20260823_140621_59dd51")

    assert component == "session_20260823_140621_59dd51"
    assert ":" not in component
    assert "/" not in component


def test_sanitize_path_component_uses_hash_suffix_for_long_distinct_inputs():
    first = _sanitize_path_component("a" * 80)
    second = _sanitize_path_component("a" * 79 + "b")

    assert len(first) == 64
    assert len(second) == 64
    assert first != second
    assert first[:51] == "a" * 51
    assert len(first.rsplit("_", 1)[1]) == 12
    assert ":" not in first
    assert "/" not in first


def test_sanitize_path_component_uses_default_for_empty_value():
    assert _sanitize_path_component("") == "default"
    assert _sanitize_path_component(None) == "default"  # type: ignore[arg-type]
