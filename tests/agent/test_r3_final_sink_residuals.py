"""Causal tests for the final R3 exception and final-response sinks."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.codex_runtime import (
    _record_codex_app_server_usage,
    make_codex_app_server_event_bridge,
    run_codex_app_server_turn,
)
from tools.tool_result_sanitization import sanitize_exception_for_sink
from tools.tool_result_storage import maybe_persist_tool_result


MARKER = "opaque-r3-final-residual-SECRET-654321"


def test_exception_sanitizer_keeps_diagnostic_class_without_message():
    error = RuntimeError(MARKER)
    safe = sanitize_exception_for_sink(error)
    assert MARKER not in safe
    assert "RuntimeError" in safe


def test_codex_callback_exception_is_contained_without_raw_log(caplog):
    agent = SimpleNamespace(
        tool_progress_callback=MagicMock(),
        _fire_stream_delta=MagicMock(side_effect=RuntimeError(MARKER)),
        _fire_reasoning_delta=None,
        _emit_interim_assistant_message=None,
    )
    bridge = make_codex_app_server_event_bridge(agent)

    with caplog.at_level("DEBUG"):
        bridge({
            "method": "item/agentMessage/delta",
            "params": {"delta": MARKER},
        })

    agent._fire_stream_delta.assert_called_once_with(MARKER)
    assert MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


def test_codex_usage_persistence_exception_is_contained_without_raw_log(caplog):
    agent = SimpleNamespace(
        session_api_calls=0,
        context_compressor=None,
        _session_db=MagicMock(),
        _session_db_created=True,
        session_id="session-r3",
        model="codex",
        provider="openai-codex",
        base_url="https://example.test",
        api_key="test-key",
    )
    agent._session_db.queue_token_counts.side_effect = RuntimeError(MARKER)
    turn = SimpleNamespace(token_usage_last=None)

    with caplog.at_level("DEBUG"):
        result = _record_codex_app_server_usage(agent, turn)

    assert result == {}
    assert MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


def test_remote_storage_failure_has_no_raw_exception_in_return_or_log(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    remote = MagicMock()
    remote.get_temp_dir.return_value = ""
    remote.execute.side_effect = RuntimeError(MARKER)

    with patch(
        "tools.tool_result_storage._sandbox_visible_spillover_path",
        return_value=None,
    ), caplog.at_level("DEBUG"):
        result = maybe_persist_tool_result(
            content="x" * 60_000 + MARKER,
            tool_name="terminal",
            tool_use_id="storage-r3",
            env=remote,
            threshold=30_000,
        )

    assert MARKER not in result
    assert MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("should_retire", [False, True])
def test_codex_error_turn_sanitizes_final_response_and_error(
    should_retire, caplog
):
    turn = SimpleNamespace(
        interrupted=False,
        error={"nested": {"message": MARKER}},
        thread_id="thread-r3",
        turn_id="turn-r3",
        projected_messages=[],
        tool_iterations=0,
        final_text=MARKER,
        should_retire=should_retire,
    )
    agent = MagicMock()
    session = MagicMock()
    agent._codex_session = session
    agent._codex_session.run_turn.return_value = turn
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = None
    agent.session_id = "session-r3"
    agent.context_compressor = None
    agent._interrupt_requested = False

    with caplog.at_level("DEBUG"):
        result = run_codex_app_server_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=[{"role": "user", "content": "hello"}],
            effective_task_id="task-r3",
        )

    assert result["completed"] is False
    assert result["partial"] is True
    assert MARKER not in result["final_response"]
    assert MARKER not in result["error"]
    assert MARKER not in repr(result["messages"])
    assert MARKER not in caplog.text
    if should_retire:
        session.close.assert_called_once_with()
