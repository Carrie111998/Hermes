from unittest.mock import MagicMock, patch
from types import SimpleNamespace
from pathlib import Path
from hermes_cli.plugins import VALID_HOOKS, PluginManager
from cli import HermesCLI


def test_session_hooks_in_valid_hooks():
    """Verify on_session_finalize and on_session_reset are registered as valid hooks."""
    assert "on_session_finalize" in VALID_HOOKS
    assert "on_session_reset" in VALID_HOOKS


def test_classic_cli_constructor_wrapper_mints_trusted_runtime(monkeypatch):
    import cli as cli_mod
    import run_agent

    monkeypatch.setattr(
        run_agent,
        "AIAgent",
        lambda *args, **kwargs: SimpleNamespace(platform=kwargs.get("platform")),
    )

    agent = cli_mod.AIAgent(platform="cli")

    assert agent.platform == "cli"
    assert agent._classic_cli_runtime is True


def test_single_query_finalize_does_not_trust_platform_label_for_classic_authority():
    import cli as cli_mod

    fake_cli = SimpleNamespace(
        session_id="hostile-session",
        agent=SimpleNamespace(
            session_id="hostile-session",
            platform="cli",
            _classic_cli_runtime=False,
        ),
    )
    cli_mod._single_query_finalize_attempted_session_ids.discard("hostile-session")

    with patch("agent.turn_context._plugin_hook_cwd", return_value="") as cwd, patch(
        "agent.turn_context._plugin_hook_profile_name", return_value=""
    ) as profile, patch("hermes_cli.lifecycle.finalize_session"):
        cli_mod._notify_single_query_session_finalize(fake_cli)

    cwd.assert_called_once_with("hostile-session", allow_cli_fallback=False)
    profile.assert_called_once_with(allow_process_fallback=False)


def test_tui_lifecycle_uses_the_single_upstream_resume_scroll_helper():
    root = Path(__file__).resolve().parents[2]
    lifecycle = (root / "ui-tui/src/app/useSessionLifecycle.ts").read_text(
        encoding="utf-8"
    )
    helper = (root / "ui-tui/src/app/sessionResumeView.ts").read_text(
        encoding="utf-8"
    )

    assert "export const scheduleResumeScrollToBottom" not in lifecycle
    assert "scheduleResumeScrollToBottom } from './sessionResumeView.js'" in lifecycle
    assert "refreshSessionView()" in helper


# These tests pin CLI ownership of the finalize request. The end-to-end
# built-in/core/plugin dispatch order is exercised by
# tests/hermes_cli/test_lifecycle.py::test_finalize_session_closes_core_before_plugin_export.
@patch("hermes_cli.lifecycle.invoke_hook")
@patch("hermes_cli.lifecycle.finalize_session")
def test_session_finalize_on_reset(mock_finalize_session, mock_invoke_hook):
    """Verify on_session_finalize fires when /new or /reset is used."""
    cli = HermesCLI()
    cli.agent = MagicMock()
    cli.agent.session_id = "test-session-id"

    # Simulate /new command which triggers on_session_finalize for the old session
    cli.new_session(silent=True)

    # Check if on_session_finalize was called for the old session
    assert any(
        not c.args
        and c.kwargs["session_id"] == "test-session-id"
        and c.kwargs["platform"] == "cli"
        for c in mock_finalize_session.call_args_list
    )
    # Check if on_session_reset was called for the new session
    assert any(
        c.args == ("on_session_reset",)
        and c.kwargs["session_id"] == cli.session_id
        and c.kwargs["platform"] == "cli"
        for c in mock_invoke_hook.call_args_list
    )


@patch("hermes_cli.lifecycle.finalize_session")
def test_session_finalize_on_cleanup(mock_finalize_session):
    """Verify on_session_finalize fires during CLI exit cleanup."""
    import cli as cli_mod

    mock_agent = MagicMock()
    mock_agent.session_id = "cleanup-session-id"
    cli_mod._active_agent_ref = mock_agent
    cli_mod._cleanup_done = False

    cli_mod._run_cleanup()

    assert any(
        not c.args
        and c.kwargs["session_id"] == "cleanup-session-id"
        and c.kwargs["platform"] == "cli"
        and c.kwargs["reason"] == "shutdown"
        for c in mock_finalize_session.call_args_list
    )


@patch("hermes_cli.lifecycle.finalize_session")
def test_classic_shutdown_finalize_uses_configured_cwd_and_concrete_profile(
    mock_finalize_session, monkeypatch, tmp_path
):
    import cli as cli_mod

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "reviewer"
    )

    cli_mod._notify_session_finalize(
        session_id="shutdown-session", classic_cli=True
    )

    assert mock_finalize_session.call_args.kwargs == {
        "session_id": "shutdown-session",
        "platform": "cli",
        "reason": "shutdown",
        "old_session_id": "shutdown-session",
        "cwd": str(tmp_path),
        "profile_name": "reviewer",
    }


@patch("hermes_cli.lifecycle.finalize_session")
def test_single_query_finalize_uses_classic_session_attribution(
    mock_finalize_session, monkeypatch, tmp_path
):
    import cli as cli_mod

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "reviewer"
    )
    cli_mod._single_query_finalize_attempted_session_ids.clear()
    one_shot = SimpleNamespace(
        agent=SimpleNamespace(
            session_id="one-shot",
            platform="cli",
            _classic_cli_runtime=True,
        ),
        session_id="fallback",
    )

    cli_mod._notify_single_query_session_finalize(one_shot)

    assert mock_finalize_session.call_args.kwargs["cwd"] == str(tmp_path)
    assert mock_finalize_session.call_args.kwargs["profile_name"] == "reviewer"


@patch("hermes_cli.lifecycle.invoke_hook")
def test_interrupted_session_end_helper_emits_observer_shape(mock_invoke_hook):
    """Verify quiet single-query interruption emits a correlated session end."""
    import cli as cli_mod

    mock_agent = MagicMock()
    mock_agent.session_id = "agent-session-id"
    mock_agent.model = "test-model"
    mock_agent.platform = "cli"
    mock_agent._current_task_id = "task-1"
    mock_agent._current_turn_id = "turn-1"
    mock_agent._current_api_request_id = "api-1"
    cli = SimpleNamespace(agent=mock_agent, session_id="cli-session-id")

    cli_mod._emit_interrupted_session_end(cli, reason="keyboard_interrupt")

    mock_agent.interrupt.assert_called_once_with("keyboard interrupt")
    assert cli.session_id == "agent-session-id"
    mock_invoke_hook.assert_called_once()
    call = mock_invoke_hook.call_args
    assert call.args == ("on_session_end",)
    assert call.kwargs["session_id"] == "agent-session-id"
    assert call.kwargs["task_id"] == "task-1"
    assert call.kwargs["turn_id"] == "turn-1"
    assert call.kwargs["api_request_id"] == "api-1"
    assert call.kwargs["completed"] is False
    assert call.kwargs["interrupted"] is True
    assert call.kwargs["reason"] == "keyboard_interrupt"


@patch("hermes_cli.plugins.invoke_hook")
def test_hook_errors_are_caught(mock_invoke_hook):
    """Verify hook exceptions are caught and don't crash the agent."""
    mgr = PluginManager()

    # Register a hook that raises
    def bad_callback(**kwargs):
        raise Exception("Hook failed")

    mgr._hooks["on_session_finalize"] = [bad_callback]

    # This should not raise
    results = mgr.invoke_hook("on_session_finalize", session_id="test", platform="cli")
    assert results == []
