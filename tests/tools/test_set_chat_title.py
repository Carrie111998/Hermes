import json

from gateway.session_context import clear_session_vars, set_session_vars
from model_tools import (
    _clear_tool_defs_cache,
    get_tool_definitions,
    handle_function_call,
)
from tools.registry import invalidate_check_fn_cache, registry
from tools.set_chat_title_tool import SET_CHAT_TITLE_SCHEMA
from toolsets import resolve_toolset


def test_schema_is_single_argument_and_gives_safe_semantic_guidance():
    parameters = SET_CHAT_TITLE_SCHEMA["parameters"]

    assert list(parameters["properties"]) == ["title"]
    assert parameters["required"] == ["title"]
    assert parameters["additionalProperties"] is False
    guidance = " ".join(
        (
            SET_CHAT_TITLE_SCHEMA["description"],
            parameters["properties"]["title"]["description"],
        )
    )
    assert "CURRENT chat only" in guidance
    assert "semantic base" in guidance
    assert "omit lifecycle status emoji" in guidance
    assert "gateway adds lifecycle status" in guidance
    assert "Never inspect platform credentials" in guidance
    assert "API scripts" in guidance


def test_only_matrix_platform_bundle_resolves_set_chat_title():
    assert "set_chat_title" in resolve_toolset("hermes-matrix")
    for toolset in ("hermes-cli", "hermes-telegram", "hermes-api-server"):
        assert "set_chat_title" not in resolve_toolset(toolset)


def test_model_tools_registry_dispatch_uses_bound_gateway_context():
    calls = []
    tokens = set_session_vars(
        platform="matrix",
        current_chat_rename_callback=lambda title: (
            calls.append(title) or {"success": True, "title": title}
        ),
    )
    invalidate_check_fn_cache()
    try:
        entry = registry.get_entry("set_chat_title")
        assert entry is not None
        assert entry.check_fn()
        result = json.loads(
            handle_function_call("set_chat_title", {"title": "Fortress"})
        )
    finally:
        clear_session_vars(tokens)
        invalidate_check_fn_cache()

    assert result == {"success": True, "title": "Fortress"}
    assert calls == ["Fortress"]


def _definition_names(toolsets):
    return {
        definition["function"]["name"]
        for definition in get_tool_definitions(
            enabled_toolsets=toolsets,
            quiet_mode=True,
            skip_tool_search_assembly=True,
        )
    }


def test_unbound_cached_schema_build_does_not_hide_later_bound_matrix_tool():
    unbound_tokens = set_session_vars(platform="matrix")
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    try:
        assert "set_chat_title" in _definition_names(["hermes-matrix"])
        tokens = set_session_vars(
            platform="matrix",
            current_chat_rename_callback=lambda _title: {"success": True},
        )
        try:
            assert "set_chat_title" in _definition_names(["hermes-matrix"])
        finally:
            clear_session_vars(tokens)
    finally:
        clear_session_vars(unbound_tokens)
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()


def test_bound_first_schema_build_never_leaks_into_non_matrix_toolsets():
    tokens = set_session_vars(
        platform="matrix",
        current_chat_rename_callback=lambda _title: {"success": True},
    )
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()
    try:
        assert "set_chat_title" in _definition_names(["hermes-matrix"])
        for toolset in ("hermes-cli", "hermes-telegram", "hermes-api-server"):
            assert "set_chat_title" not in _definition_names([toolset])
    finally:
        clear_session_vars(tokens)
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()


def test_direct_registry_dispatch_truthfully_rejects_unsupported_context():
    tokens = set_session_vars(platform="telegram")
    invalidate_check_fn_cache()
    try:
        entry = registry.get_entry("set_chat_title")
        assert entry is not None
        assert entry.check_fn()
        result = json.loads(
            registry.dispatch("set_chat_title", {"title": "Fortress"})
        )
    finally:
        clear_session_vars(tokens)
        invalidate_check_fn_cache()

    assert "error" in result
    assert "Matrix gateway session" in result["error"]
