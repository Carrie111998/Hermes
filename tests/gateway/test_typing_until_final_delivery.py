import inspect
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner


def _stream_config():
    return SimpleNamespace(
        edit_interval=0.1,
        buffer_threshold=1,
        cursor=" ▉",
        fresh_final_after_seconds=5.0,
        transport="edit",
    )


def test_telegram_stream_finalize_does_not_pause_typing_before_final_delivery():
    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="-1001", chat_type="group")
    adapter = SimpleNamespace(
        SUPPORTS_MESSAGE_EDITING=True,
        pause_typing_for_chat=lambda _chat_id: None,
    )

    _cfg, before_finalize = runner._build_stream_consumer_config(
        source,
        _stream_config(),
        adapter,
        on_missing_cursor="raise",
    )

    assert before_finalize is None


def test_agent_completion_path_does_not_stop_typing_before_response_delivery():
    source = inspect.getsource(GatewayRunner._handle_message_with_agent)
    after_agent = source.split("agent_result = await self._run_agent", 1)[1]
    before_response = after_agent.split("response = agent_result.get", 1)[0]

    assert "stop_typing" not in before_response
    assert "_stop_typing_with_metadata" not in before_response
    assert ".stop_typing(" not in before_response


def test_agent_error_path_keeps_typing_for_outer_safe_error_delivery():
    source = inspect.getsource(GatewayRunner._handle_message_with_agent)
    error_tail = source.split("except Exception as e:", 1)[1]
    error_prefix = error_tail.split("logger.exception", 1)[0]
    assert "stop_typing" not in error_prefix