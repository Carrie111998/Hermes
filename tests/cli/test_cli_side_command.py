from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
from tests.cli.test_cli_new_session import _make_cli


class _ManualFirstStarter:
    def __init__(self):
        self.started = []
        self.auto_after_first = False

    def __call__(self, target, *, name):
        self.started.append((target, name))
        if self.auto_after_first:
            target()

    def run_first(self):
        self.auto_after_first = True
        self.started[0][0]()


class _RecordingSideAgent:
    calls: list[dict] = []
    instances: list[_RecordingSideAgent] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._persist_disabled = False
        self._session_db = object()
        self._session_json_enabled = True
        type(self).instances.append(self)

    def run_conversation(self, user_message, conversation_history=None, task_id=None, **kwargs):
        call = {
            "user_message": user_message,
            "conversation_history": list(conversation_history or []),
            "task_id": task_id,
            "kwargs": kwargs,
            "agent_kwargs": self.kwargs,
        }
        type(self).calls.append(call)
        messages = list(conversation_history or []) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": f"side answer {len(type(self).calls)}"},
        ]
        return {"final_response": messages[-1]["content"], "messages": messages}


def _reset_recording_agent():
    _RecordingSideAgent.calls = []
    _RecordingSideAgent.instances = []


def _prepare_cli():
    cli = _make_cli()
    cli.console = MagicMock()
    cli._invalidate = MagicMock()
    cli._ensure_runtime_credentials = MagicMock(return_value=True)
    cli._resolve_turn_agent_config = MagicMock(
        return_value={
            "model": "gpt-5.4",
            "runtime": {
                "api_key": "token",
                "base_url": "https://example.test/v1",
                "provider": "openai",
                "api_mode": None,
                "command": None,
                "args": [],
            },
            "request_overrides": None,
        }
    )
    cli.conversation_history = [
        {"role": "user", "content": "main question"},
        {"role": "assistant", "content": "main answer"},
    ]
    return cli


def _roles(history):
    return [m.get("role") for m in history]


def test_side_is_registered_cli_only():
    side = resolve_command("side")

    assert side is not None
    assert side.name == "side"
    assert side.cli_only is True
    assert any(cmd.name == "side" for cmd in COMMAND_REGISTRY)


def test_first_side_question_combines_boundary_and_question_in_one_user_turn(monkeypatch):
    _reset_recording_agent()
    cli = _prepare_cli()
    parent_history = [dict(m) for m in cli.conversation_history]
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)
    monkeypatch.setitem(cli._run_side_turn.__func__.__globals__, "AIAgent", _RecordingSideAgent)
    cli._start_side_thread = lambda target, **_kwargs: target()
    monkeypatch.setattr("cli.ChatConsole", lambda: MagicMock(print=MagicMock()))

    assert cli.process_command("/side explain the parser") is True

    assert len(_RecordingSideAgent.calls) == 1
    call = _RecordingSideAgent.calls[0]
    assert call["conversation_history"] == parent_history
    assert _roles(call["conversation_history"]) == ["user", "assistant"]
    assert isinstance(call["user_message"], str)
    assert call["user_message"].startswith("Side conversation boundary.")
    assert "explain the parser" in call["user_message"]
    assert cli.conversation_history == parent_history


def test_repeated_side_turns_use_side_history_without_mutating_parent(monkeypatch):
    _reset_recording_agent()
    cli = _prepare_cli()
    parent_history = [dict(m) for m in cli.conversation_history]
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)
    monkeypatch.setitem(cli._run_side_turn.__func__.__globals__, "AIAgent", _RecordingSideAgent)
    cli._start_side_thread = lambda target, **_kwargs: target()
    monkeypatch.setattr("cli.ChatConsole", lambda: MagicMock(print=MagicMock()))

    cli.process_command("/side first")
    cli._submit_side_message("second")

    assert len(_RecordingSideAgent.calls) == 2
    second = _RecordingSideAgent.calls[1]
    assert second["conversation_history"][:2] == parent_history
    assert _roles(second["conversation_history"]) == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert second["user_message"] == "second"
    assert cli.conversation_history == parent_history


def test_busy_side_queue_drains_fifo(monkeypatch):
    _reset_recording_agent()
    starter = _ManualFirstStarter()
    cli = _prepare_cli()
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)
    monkeypatch.setitem(cli._run_side_turn.__func__.__globals__, "AIAgent", _RecordingSideAgent)
    cli._start_side_thread = starter
    monkeypatch.setattr("cli.ChatConsole", lambda: MagicMock(print=MagicMock()))

    cli.process_command("/side first")
    cli._submit_side_message("second")
    cli._submit_side_message("third")

    assert [item[0] for item in cli._side_queue] == ["second", "third"]
    starter.run_first()

    assert _RecordingSideAgent.calls[0]["user_message"].startswith("Side conversation boundary.")
    assert [call["user_message"] for call in _RecordingSideAgent.calls[1:]] == [
        "second",
        "third",
    ]
    assert cli._side_queue == []


def test_side_cancel_clears_ephemeral_state_and_queue(monkeypatch):
    cli = _prepare_cli()
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)

    cli._side_state = {"session_id": "side_123", "conversation_history": [], "running": True}
    cli._side_queue = [("queued", [])]

    assert cli._close_side_conversation() is True
    assert cli._side_state is None
    assert cli._side_queue == []


def test_side_images_use_native_content_parts_and_are_not_count_markers(monkeypatch, tmp_path):
    _reset_recording_agent()
    cli = _prepare_cli()
    image_path = tmp_path / "one.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)
    monkeypatch.setitem(cli._run_side_turn.__func__.__globals__, "AIAgent", _RecordingSideAgent)
    cli._start_side_thread = lambda target, **_kwargs: target()
    monkeypatch.setattr("cli.ChatConsole", lambda: MagicMock(print=MagicMock()))

    cli.process_command("/side")
    accepted = cli._submit_side_message("what is here?", images=[image_path])

    assert accepted is True
    user_message = _RecordingSideAgent.calls[0]["user_message"]
    assert isinstance(user_message, list)
    assert any(part.get("type") == "image_url" for part in user_message)
    text_parts = [part.get("text", "") for part in user_message if part.get("type") == "text"]
    assert "what is here?" in "\n".join(text_parts)
    assert "User attached 1 image" not in "\n".join(text_parts)


def test_side_rejects_unreadable_images_before_clearing_composer(monkeypatch, tmp_path):
    _reset_recording_agent()
    cli = _prepare_cli()
    missing = tmp_path / "missing.png"
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)
    monkeypatch.setitem(cli._run_side_turn.__func__.__globals__, "AIAgent", _RecordingSideAgent)
    cli._start_side_thread = lambda target, **_kwargs: target()

    cli.process_command("/side")
    accepted = cli._submit_side_message("look", images=[missing])

    assert accepted is False
    assert _RecordingSideAgent.calls == []


def test_side_agent_is_ephemeral_and_isolated(monkeypatch):
    _reset_recording_agent()
    cli = _prepare_cli()
    cli.session_id = "main_session"
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)
    monkeypatch.setitem(cli._run_side_turn.__func__.__globals__, "AIAgent", _RecordingSideAgent)
    cli._start_side_thread = lambda target, **_kwargs: target()
    monkeypatch.setattr("cli.ChatConsole", lambda: MagicMock(print=MagicMock()))

    cli.process_command("/side isolated")

    kwargs = _RecordingSideAgent.calls[0]["agent_kwargs"]
    assert kwargs["session_id"].startswith("side_")
    assert kwargs["session_id"] != cli.session_id
    assert kwargs["session_db"] is None
    assert kwargs["skip_memory"] is True
    assert kwargs["skip_context_files"] is True
    side_agent = _RecordingSideAgent.instances[0]
    assert side_agent._persist_disabled is True
    assert side_agent._session_db is None
    assert side_agent._session_json_enabled is False
    assert cli.session_id == "main_session"


def test_side_history_deep_copy_cannot_mutate_parent_nested_content(monkeypatch):
    cli = _prepare_cli()
    cli.conversation_history[0]["content"] = [
        {"type": "text", "text": "main nested content"}
    ]
    monkeypatch.setattr("cli._cprint", lambda *args, **kwargs: None)

    assert cli.process_command("/side") is True
    cli._side_state["conversation_history"][0]["content"][0]["text"] = "side mutation"

    assert cli.conversation_history[0]["content"][0]["text"] == "main nested content"
