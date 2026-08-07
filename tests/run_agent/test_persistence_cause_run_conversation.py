"""Integration contract: when ``_flush_messages_to_session_db`` raises an
exception, the agent must preserve the originating exception on
``_last_persistence_failure`` so the user-facing message can name the
real cause (compression lock / DB lock / disk full / permission) instead
of always blaming disk space (#81227).

The legacy boolean flag ``_incremental_persistence_failed`` must also
be set so existing guards keep working unchanged.
"""

from __future__ import annotations

import errno
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionCompressionInProgressError
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent():
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-cause-test-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _mock_tool_call(*, call_id: str, name: str = "web_search") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def _mock_response(*, content: str, finish_reason: str, tool_calls=None):
    choice = SimpleNamespace(
        index=0,
        message=SimpleNamespace(
            role="assistant",
            content=content,
            tool_calls=tool_calls or [],
        ),
        finish_reason=finish_reason,
    )
    usage = SimpleNamespace(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        reasoning_tokens=0,
    )
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model="test-model",
        id="chatcmpl-mock",
    )


def test_run_conversation_records_compression_lock_failure_for_turn_finalizer():
    """The exact #81227 scenario: a live foreign compression lock causes
    ``_flush_messages_to_session_db`` to raise. The run_conversation tail
    must record the originating exception on ``_last_persistence_failure``
    so the turn-finalizer surfaces the right cause instead of "often a
    full disk"."""
    agent = _make_agent()
    tool_call = _mock_tool_call(call_id="must-not-run")
    agent.client.chat.completions.create.return_value = _mock_response(
        content="I'll inspect the repository now.",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    compression_exc = SessionCompressionInProgressError(
        "Session 'sess-1' is being compressed by another writer"
    )
    agent._flush_messages_to_session_db = MagicMock(side_effect=compression_exc)
    agent.interim_assistant_callback = MagicMock()
    agent._execute_tool_calls = MagicMock()

    result = agent.run_conversation("inspect the repository")

    # The boolean flag keeps working for legacy guards.
    assert agent._incremental_persistence_failed is True
    # The new attribute carries the originating exception object.
    assert agent._last_persistence_failure is compression_exc
    # The turn terminator produced the cause-specific message instead of the
    # legacy "often a full disk" wording.
    assert result["failed"] is True
    assert result["turn_exit_reason"] == "session_persistence_failed"
    error_text = result.get("error", "")
    assert "compression" in error_text.lower() or "compress" in error_text.lower()
    assert "often a full disk" not in error_text


def test_run_conversation_records_disk_full_failure():
    """A genuine ENOSPC append must still surface the disk-full hint,
    proving the new path does not regress the real disk-full case."""
    agent = _make_agent()
    tool_call = _mock_tool_call(call_id="enospc-call")
    agent.client.chat.completions.create.return_value = _mock_response(
        content="I'll inspect the repository now.",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    disk_exc = OSError(errno.ENOSPC, "No space left on device")
    agent._flush_messages_to_session_db = MagicMock(side_effect=disk_exc)
    agent._execute_tool_calls = MagicMock()

    result = agent.run_conversation("inspect the repository")

    assert agent._last_persistence_failure is disk_exc
    error_text = result.get("error", "")
    assert "disk" in error_text.lower()


def test_run_conversation_records_database_locked_failure():
    """A held write-lock from a competing VACUUM must surface a
    maintenance hint, not a disk-full hint."""
    agent = _make_agent()
    tool_call = _mock_tool_call(call_id="locked-call")
    agent.client.chat.completions.create.return_value = _mock_response(
        content="I'll inspect the repository now.",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    locked_exc = sqlite3.OperationalError("database is locked")
    agent._flush_messages_to_session_db = MagicMock(side_effect=locked_exc)
    agent._execute_tool_calls = MagicMock()

    result = agent.run_conversation("inspect the repository")

    assert agent._last_persistence_failure is locked_exc
    error_text = result.get("error", "")
    assert "lock" in error_text.lower() or "vacuum" in error_text.lower() or "maintenance" in error_text.lower()
    assert "often a full disk" not in error_text


def test_run_conversation_clears_last_persistence_failure_on_each_turn():
    """A fresh turn must not leak the previous turn's failure cause —
    the attribute is re-initialized to ``None`` at the start of every
    run_conversation so cached gateway agents recover correctly."""
    agent = _make_agent()
    # Seed a stale failure from a prior turn.
    agent._last_persistence_failure = OSError(errno.ENOSPC, "stale")
    tool_call = _mock_tool_call(call_id="clean-call")
    agent.client.chat.completions.create.return_value = _mock_response(
        content="I'll inspect the repository now.",
        finish_reason="tool_calls",
        tool_calls=[tool_call],
    )
    # This turn succeeds — the legacy guard was never tripped.
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    agent.interim_assistant_callback = MagicMock()
    agent._execute_tool_calls = MagicMock()
    agent.tool_complete_callback = MagicMock()
    agent.tool_start_callback = MagicMock()

    with patch("run_agent.handle_function_call", return_value="repository result"):
        agent.run_conversation("inspect the repository")

    # The new failure list starts cleared; only the legitimate
    # flag-true path (failed flush) sets it.
    assert agent._last_persistence_failure is None
