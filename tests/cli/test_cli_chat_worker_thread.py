from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import cli as cli_module
from cli import HermesCLI


class _AgentStub:
    def __init__(self):
        self.session_id = "test-session"
        self.max_iterations = 90
        self._active_children = []
        self._interrupt_requested = False
        self.last_kwargs = None

    def run_conversation(self, **kwargs):
        self.last_kwargs = kwargs
        return {
            "final_response": "CHAT_THREAD_OK",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "CHAT_THREAD_OK"},
            ],
            "response_previewed": True,
            "completed": True,
        }

    def interrupt(self, _message):
        return None


def _make_cli_stub() -> HermesCLI:
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._secret_capture_callback = None
    cli_obj._sudo_password_callback = None
    cli_obj._approval_callback = None
    cli_obj._voice_mode = False
    cli_obj._voice_tts = False
    cli_obj._voice_continuous = False
    cli_obj._pending_model_switch_note = None
    cli_obj._pending_skills_reload_note = None
    cli_obj._last_turn_interrupted = False
    cli_obj._active_agent_route_signature = "stable-route"
    cli_obj._resolve_turn_agent_config = lambda _message: {
        "signature": "stable-route",
        "model": None,
        "runtime": None,
        "request_overrides": None,
    }
    cli_obj._ensure_runtime_credentials = lambda: True
    cli_obj._init_agent = lambda **_kwargs: True
    cli_obj._reset_stream_state = lambda: None
    cli_obj._flush_stream = lambda: None
    cli_obj._invalidate = lambda **_kwargs: None
    cli_obj._scrollback_box_width = lambda *args, **kwargs: 80
    cli_obj._transfer_session_yolo = lambda *_args, **_kwargs: None
    cli_obj._session_db = None
    cli_obj.conversation_history = []
    cli_obj.agent = _AgentStub()
    cli_obj.session_id = "test-session"
    cli_obj.show_reasoning = False
    cli_obj.bell_on_complete = False
    cli_obj.final_response_markdown = False
    cli_obj.show_timestamps = False
    cli_obj._stream_started = False
    cli_obj._stream_box_opened = False
    cli_obj.model = "test-model"
    cli_obj.provider = "test-provider"
    cli_obj.base_url = ""
    cli_obj.api_key = ""
    cli_obj.api_mode = "chat_completions"
    cli_obj.session_start = datetime.now()
    return cli_obj


def test_chat_worker_thread_calls_run_conversation_and_returns_response():
    """Regression: HermesCLI.chat()'s worker thread must actually invoke
    self.agent.run_conversation(...). Without that call, chat -q exits with only
    the user message recorded and no assistant reply."""
    cli_obj = _make_cli_stub()

    fake_console = MagicMock()
    fake_console.print = MagicMock()

    with patch.object(cli_module, "set_secret_capture_callback"), patch.object(
        cli_module, "set_sudo_password_callback"
    ), patch.object(cli_module, "set_approval_callback"), patch.object(
        cli_module, "_accent_hex", return_value="#fff"
    ), patch.object(cli_module, "_cprint"), patch.object(
        cli_module, "ChatConsole", return_value=fake_console
    ), patch.object(
        cli_module.time, "sleep", lambda *_args, **_kwargs: None
    ):
        response = cli_obj.chat("hello")

    assert response == "CHAT_THREAD_OK"
    assert cli_obj.agent.last_kwargs is not None
    assert cli_obj.agent.last_kwargs["user_message"] == "hello"
    assert cli_obj.agent.last_kwargs["conversation_history"][0] == {
        "role": "user",
        "content": "hello",
    }
    assert cli_obj.conversation_history == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "CHAT_THREAD_OK"},
    ]
