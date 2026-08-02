"""Regression tests for classic-CLI mid-run /steer dispatch.

Background
----------
/steer sent while the agent is running used to be queued through
``self._pending_input`` alongside ordinary user input.  ``process_loop``
pulls from that queue and calls ``process_command()`` — but while the
agent is running, ``process_loop`` is blocked inside ``self.chat()``.
By the time the queued /steer was pulled, ``_agent_running`` had
already flipped back to False, so ``process_command()`` took the idle
fallback (``"No agent running; queued as next turn"``) and delivered
the steer as an ordinary next-turn message.

The fix dispatches /steer inline on the UI thread when the agent is
running — matching the existing pattern for /model — so the steer
reaches ``agent.steer()`` (thread-safe) without touching the queue.

These tests exercise the detector + inline dispatch without starting a
prompt_toolkit app.
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


class TestSteerInlineDetector:
    """_should_handle_steer_command_inline gates the busy-path fast dispatch."""

    def test_detects_steer_when_agent_running(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_steer_command_inline("/steer focus on error handling") is True

    def test_ignores_steer_when_agent_idle(self):
        """Idle-path /steer should fall through to the normal process_loop
        dispatch so the queue-style fallback message is emitted."""
        cli = _make_cli()
        cli._agent_running = False
        assert cli._should_handle_steer_command_inline("/steer do something") is False

    def test_ignores_non_slash_input(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_steer_command_inline("steer without slash") is False
        assert cli._should_handle_steer_command_inline("") is False

    def test_ignores_other_slash_commands(self):
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_steer_command_inline("/queue hello") is False
        assert cli._should_handle_steer_command_inline("/stop") is False
        assert cli._should_handle_steer_command_inline("/help") is False

    def test_ignores_steer_with_attached_images(self):
        """Image payloads take the normal path; steer doesn't accept images."""
        cli = _make_cli()
        cli._agent_running = True
        assert cli._should_handle_steer_command_inline("/steer text", has_images=True) is False


class TestSteerBusyPathDispatch:
    """When the detector fires, process_command('/steer ...') must call
    agent.steer() directly rather than the idle-path fallback."""

    def test_process_command_routes_to_agent_steer(self, tmp_path, monkeypatch):
        """Busy /steer persists, claims, and waits for a consumption callback."""

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
        cli = _make_cli()

        class AckAgent:
            session_id = "explicit-steer-busy"
            provider = None
            model = None

            def __init__(self):
                self.calls = []

            def get_activity_summary(self):
                return {}

            def get_steer_generation(self):
                return 7

            def supports_steer_consumption_ack(self):
                return True

            def steer(self, payload, **kwargs):
                self.calls.append((payload, kwargs))
                return True

        agent = AckAgent()
        cli.agent = agent
        cli._begin_smart_cli_turn("active work")

        cli.process_command("/steer focus on errors")

        assert len(agent.calls) == 1
        payload, kwargs = agent.calls[0]
        assert payload == "focus on errors"
        assert kwargs["run_generation"] == 7
        assert callable(kwargs["on_consumed"])
        assert callable(kwargs["on_unconsumed"])
        assert callable(kwargs["on_uncertain"])
        jobs = cli._load_smart_cli_durable_jobs_locked(agent.session_id)
        assert len(jobs) == 1
        assert jobs[0]["text"] == payload
        assert jobs[0]["state"] == "transferring"
        assert cli._pending_input.empty()

        kwargs["on_consumed"]()
        assert cli._load_smart_cli_durable_jobs_locked(agent.session_id) == []

    def test_idle_path_queues_as_next_turn(self, tmp_path, monkeypatch):
        """Idle /steer is still persistence-first and stages a typed receipt."""

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
        cli = _make_cli()
        cli._agent_running = False

        cli.process_command("/steer would-be-next-turn")

        queued = cli._pending_input.get_nowait()
        assert queued.payload == "would-be-next-turn"
        assert queued.durable_id
        jobs = cli._load_smart_cli_durable_jobs_locked(
            queued.durable_session_id
        )
        assert jobs == [
            {
                "id": queued.durable_id,
                "text": "would-be-next-turn",
                "state": "accepted",
            }
        ]


if __name__ == "__main__":  # pragma: no cover
    import pytest

    pytest.main([__file__, "-v"])
