"""Regression tests for classic-CLI mid-run /background dispatch.

Background
----------
/background (and aliases /bg, /btw) sent while the agent is running used to be
queued through ``self._pending_input`` alongside ordinary user input.
``process_loop`` pulls from that queue and calls ``process_command()`` — but
while the agent is running, ``process_loop`` is blocked inside ``self.chat()``.
By the time the queued /background was pulled, the foreground turn had already
finished, so the independent background task started too late.

The fix dispatches /background inline on the UI thread when the agent is
running — matching the existing pattern for /steer (#13354) — so
``_handle_background_command`` runs immediately without touching the queue.

These tests exercise the detector + inline dispatch without starting a
prompt_toolkit app. See issue #75221.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock, patch


def _make_cli():
    """Create a HermesCLI instance with prompt_toolkit stubbed out."""
    _clean_config = {
        "model": {
            "default": "anthropic/claude-opus-4.6",
            "base_url": "https://openrouter.ai/api/v1",
            "provider": "auto",
        },
        "display": {"compact": False, "tool_progress": "all"},
        "agent": {},
        "terminal": {"env_type": "local"},
    }
    clean_env = {"LLM_MODEL": "", "HERMES_MAX_ITERATIONS": ""}
    prompt_toolkit_stubs = {
        "prompt_toolkit": MagicMock(),
        "prompt_toolkit.history": MagicMock(),
        "prompt_toolkit.styles": MagicMock(),
        "prompt_toolkit.patch_stdout": MagicMock(),
        "prompt_toolkit.application": MagicMock(),
        "prompt_toolkit.layout": MagicMock(),
        "prompt_toolkit.layout.processors": MagicMock(),
        "prompt_toolkit.filters": MagicMock(),
        "prompt_toolkit.layout.dimension": MagicMock(),
        "prompt_toolkit.layout.menus": MagicMock(),
        "prompt_toolkit.widgets": MagicMock(),
        "prompt_toolkit.key_binding": MagicMock(),
        "prompt_toolkit.completion": MagicMock(),
        "prompt_toolkit.formatted_text": MagicMock(),
        "prompt_toolkit.auto_suggest": MagicMock(),
    }
    with patch.dict(sys.modules, prompt_toolkit_stubs), patch.dict(
        "os.environ", clean_env, clear=False
    ):
        import cli as _cli_mod

        _cli_mod = importlib.reload(_cli_mod)
        with patch.object(_cli_mod, "get_tool_definitions", return_value=[]), patch.dict(
            _cli_mod.__dict__, {"CLI_CONFIG": _clean_config}
        ):
            return _cli_mod.HermesCLI()


class TestBackgroundInlineDetector:
    """_should_handle_background_command_inline gates the busy-path fast dispatch."""

    def test_detects_background_when_agent_running(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_background_command_inline(
            "/background inspect the test failures"
        ) is True

    def test_detects_bg_alias_when_agent_running(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_background_command_inline(
            "/bg inspect the test failures"
        ) is True

    def test_detects_btw_alias_when_agent_running(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_background_command_inline(
            "/btw check dependency breaking changes"
        ) is True

    def test_ignores_background_when_agent_idle(self):
        """Idle-path /background should fall through to the normal process_loop
        dispatch rather than the busy-path Enter early-return."""
        cli = _make_cli()
        cli._agent_running = False
        assert cli._should_handle_background_command_inline(
            "/background do something"
        ) is False

    def test_ignores_non_slash_input(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_background_command_inline(
            "background without slash"
        ) is False
        assert cli._should_handle_background_command_inline("") is False

    def test_ignores_other_slash_commands(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_background_command_inline("/steer hello") is False
        assert cli._should_handle_background_command_inline("/queue hello") is False
        assert cli._should_handle_background_command_inline("/stop") is False
        assert cli._should_handle_background_command_inline("/help") is False

    def test_ignores_background_with_attached_images(self):
        """Image payloads take the normal path; background doesn't accept images."""
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_background_command_inline(
            "/bg text", has_images=True
        ) is False


class TestBackgroundBusyPathDispatch:
    """When the detector fires, process_command('/bg ...') must call the
    background handler rather than queueing onto _pending_input."""

    def test_process_command_routes_to_background_handler(self):
        """With _agent_running=True, /bg reaches _handle_background_command,
        NOT _pending_input."""
        cli = _make_cli()
        cli._agent_running = True
        cli._pending_input = MagicMock()
        cli._handle_background_command = MagicMock()

        cli.process_command("/bg inspect the test failures")

        cli._handle_background_command.assert_called_once_with(
            "/bg inspect the test failures"
        )
        cli._pending_input.put.assert_not_called()

    def test_process_command_routes_btw_alias(self):
        cli = _make_cli()
        cli._agent_running = True
        cli._pending_input = MagicMock()
        cli._handle_background_command = MagicMock()

        cli.process_command("/btw check deps")

        cli._handle_background_command.assert_called_once_with("/btw check deps")
        cli._pending_input.put.assert_not_called()

    def test_idle_path_still_invokes_handler(self):
        """Control — when the agent is NOT running, /background still reaches
        the handler via normal process_command (no queue pollution from the
        slash itself)."""
        cli = _make_cli()
        cli._agent_running = False
        cli._pending_input = MagicMock()
        cli._handle_background_command = MagicMock()

        cli.process_command("/background would-run-now")

        cli._handle_background_command.assert_called_once_with(
            "/background would-run-now"
        )
        cli._pending_input.put.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
