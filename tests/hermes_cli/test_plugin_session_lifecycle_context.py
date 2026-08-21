from types import SimpleNamespace

from cli import HermesCLI
from hermes_cli import lifecycle
from hermes_cli.middleware import (
    apply_tool_request_middleware,
    is_classic_cli_runtime,
    run_tool_execution_middleware,
)
from tui_gateway import server


def test_caller_influenced_cli_platform_does_not_mint_classic_authority():
    spoofed = SimpleNamespace(platform="cli")
    trusted = SimpleNamespace(platform="cli", _classic_cli_runtime=True)

    assert is_classic_cli_runtime(spoofed) is False
    assert is_classic_cli_runtime(trusted) is True


def test_cli_boundary_payload_carries_explicit_transition_and_cwd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(lifecycle, "finalize_session", lambda **kwargs: calls.append(("finalize", kwargs)))
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda event, **kwargs: calls.append((event, kwargs)))

    cli = object.__new__(HermesCLI)
    cli.agent = SimpleNamespace(session_id="new-session")
    cli.platform = "cli"
    cli._notify_session_boundary(
        "on_session_finalize",
        old_session_id="old-session",
        new_session_id="new-session",
        cwd=str(tmp_path),
    )

    assert calls == [("finalize", {
        "session_id": "old-session",
        "platform": "cli",
        "reason": "new_session",
        "old_session_id": "old-session",
        "new_session_id": "new-session",
        "cwd": str(tmp_path),
        "profile_name": "default",
    })]


def test_classic_cli_boundary_derives_profile_and_configured_cwd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        lifecycle,
        "finalize_session",
        lambda **kwargs: calls.append(("finalize", kwargs)),
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "reviewer",
    )
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

    cli = object.__new__(HermesCLI)
    cli.agent = SimpleNamespace(session_id="new-session")
    cli.platform = "cli"
    cli._notify_session_boundary(
        "on_session_finalize",
        old_session_id="old-session",
        new_session_id="new-session",
    )

    assert calls == [("finalize", {
        "session_id": "old-session",
        "platform": "cli",
        "reason": "new_session",
        "old_session_id": "old-session",
        "new_session_id": "new-session",
        "cwd": str(tmp_path),
        "profile_name": "reviewer",
    })]


def test_tui_boundary_payload_matches_gateway_transition_contract(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(lifecycle, "finalize_session", lambda **kwargs: calls.append(("finalize", kwargs)))
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda event, **kwargs: calls.append((event, kwargs)))

    server._notify_session_boundary(
        "on_session_reset",
        "new-session",
        "tui",
        reason="new_session",
        old_session_id="old-session",
        new_session_id="new-session",
        cwd=str(tmp_path),
    )

    assert calls == [("on_session_reset", {
        "session_id": "new-session",
        "platform": "tui",
        "reason": "new_session",
        "old_session_id": "old-session",
        "new_session_id": "new-session",
        "cwd": str(tmp_path),
    })]


def test_plugin_hook_cwd_uses_task_workspace_registry(monkeypatch, tmp_path):
    from agent import turn_context
    from tools import file_tools
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda task_id: None)
    monkeypatch.setattr(
        file_tools,
        "_registered_task_cwd_override",
        lambda task_id="default": str(tmp_path) if task_id == "task-a" else None,
    )

    assert turn_context._plugin_hook_cwd("task-a") == str(tmp_path)
    assert turn_context._plugin_hook_cwd("missing") == ""


def test_plugin_hook_cwd_never_uses_terminal_cwd_fallback(monkeypatch, tmp_path):
    from agent import turn_context
    from tools import file_tools, terminal_tool

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda task_id: None)
    monkeypatch.setattr(file_tools, "_registered_task_cwd_override", lambda task_id: None)

    assert turn_context._plugin_hook_cwd("missing") == ""


def test_trusted_classic_cli_ignores_import_side_effect_gateway_marker(
    monkeypatch, tmp_path
):
    from agent import turn_context
    from tools import file_tools, terminal_tool

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    # Importing gateway.run marks the process even when the active agent is a
    # trusted one-shot/classic CLI. The constructor seal—not this ambient module
    # side effect—is the authority boundary.
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setattr(terminal_tool, "get_session_cwd", lambda task_id: None)
    monkeypatch.setattr(file_tools, "_registered_task_cwd_override", lambda task_id: None)

    assert turn_context._plugin_hook_cwd(
        "classic-cli", allow_cli_fallback=True
    ) == str(tmp_path)


def test_plugin_hook_profile_uses_active_profile_only_for_classic_cli(monkeypatch):
    from agent import turn_context

    monkeypatch.delenv("HERMES_SESSION_PROFILE", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "reviewer",
    )

    assert turn_context._plugin_hook_profile_name(
        allow_process_fallback=True
    ) == "reviewer"
    assert turn_context._plugin_hook_profile_name(
        allow_process_fallback=False
    ) == ""


def test_gateway_lifecycle_cwd_never_uses_process_or_environment_fallback(
    monkeypatch, tmp_path
):
    from agent import runtime_cwd

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    runtime_cwd.clear_session_cwd()
    assert runtime_cwd.authoritative_session_cwd() == ""

    bound = tmp_path / "bound"
    bound.mkdir()
    runtime_cwd.set_session_cwd(str(bound))
    assert runtime_cwd.authoritative_session_cwd() == str(bound)


def test_tool_request_middleware_carries_bound_session_profile(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "hermes_cli.middleware._has_middleware", lambda kind: True
    )
    monkeypatch.setattr(
        "hermes_cli.middleware._invoke_middleware",
        lambda kind, **kwargs: seen.append(kwargs) or [],
    )
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "reviewer"
        if name == "HERMES_SESSION_PROFILE"
        else default,
    )

    apply_tool_request_middleware("read_file", {"path": "README.md"})

    assert seen[0]["profile_name"] == "reviewer"


def test_tool_execution_middleware_carries_bound_session_profile(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "hermes_cli.middleware._get_middleware_callbacks",
        lambda kind: [object()],
    )
    monkeypatch.setattr(
        "hermes_cli.middleware._run_execution_chain",
        lambda kind, callbacks, next_call, **kwargs: seen.append(kwargs)
        or next_call(kwargs["args"]),
    )
    monkeypatch.setattr(
        "gateway.session_context.get_session_env",
        lambda name, default="": "reviewer"
        if name == "HERMES_SESSION_PROFILE"
        else default,
    )

    result = run_tool_execution_middleware(
        "read_file", {"path": "README.md"}, lambda args: args
    )

    assert result == {"path": "README.md"}
    assert seen[0]["profile_name"] == "reviewer"


def test_tool_request_middleware_does_not_invent_profile_on_unknown_surface(monkeypatch):
    seen = []
    monkeypatch.setattr("hermes_cli.middleware._has_middleware", lambda _kind: True)
    monkeypatch.setattr(
        "hermes_cli.middleware._invoke_middleware",
        lambda _kind, **kwargs: seen.append(kwargs) or [],
    )
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda *_a: "")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "hostile-process-profile"
    )

    apply_tool_request_middleware("read_file", {"path": "README.md"})

    assert seen[0].get("profile_name", "") == ""


def test_tool_execution_middleware_does_not_invent_profile_on_multiplexed_surface(
    monkeypatch,
):
    seen = []
    monkeypatch.setattr(
        "hermes_cli.middleware._get_middleware_callbacks", lambda _kind: [object()]
    )
    monkeypatch.setattr(
        "hermes_cli.middleware._run_execution_chain",
        lambda _kind, _callbacks, next_call, **kwargs: seen.append(kwargs)
        or next_call(kwargs["args"]),
    )
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda *_a: "")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "hostile-process-profile"
    )

    run_tool_execution_middleware(
        "read_file", {"path": "README.md"}, lambda args: args, platform="tui"
    )

    assert seen[0].get("profile_name", "") == ""


def test_tool_middleware_allows_profile_fallback_only_for_explicit_classic_cli(
    monkeypatch,
):
    request_seen = []
    execution_seen = []
    monkeypatch.setattr("hermes_cli.middleware._has_middleware", lambda _kind: True)
    monkeypatch.setattr(
        "hermes_cli.middleware._invoke_middleware",
        lambda _kind, **kwargs: request_seen.append(kwargs) or [],
    )
    monkeypatch.setattr(
        "hermes_cli.middleware._get_middleware_callbacks", lambda _kind: [object()]
    )
    monkeypatch.setattr(
        "hermes_cli.middleware._run_execution_chain",
        lambda _kind, _callbacks, next_call, **kwargs: execution_seen.append(kwargs)
        or next_call(kwargs["args"]),
    )
    monkeypatch.setattr("gateway.session_context.get_session_env", lambda *_a: "")
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "classic-profile"
    )

    apply_tool_request_middleware(
        "read_file", {"path": "README.md"}, classic_cli=True
    )
    run_tool_execution_middleware(
        "read_file",
        {"path": "README.md"},
        lambda args: args,
        classic_cli=True,
    )

    assert request_seen[0]["profile_name"] == "classic-profile"
    assert execution_seen[0]["profile_name"] == "classic-profile"
    assert "classic_cli" not in request_seen[0]
    assert "classic_cli" not in execution_seen[0]
