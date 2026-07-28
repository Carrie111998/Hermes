"""Tests for agent.acp_runtime — persistence + external memory contracts.

Mirrors the codex_runtime persistence contract:
- projected assistant messages are flushed to SessionDB (best-effort)
- flush exceptions do not break the turn return
- agent_persisted=True on all return paths (exactly-once)
- _sync_external_memory_for_turn receives messages= kwarg
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agent.acp_runtime import run_acp_client_turn
from agent.transports.acp_client_session import TurnResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    *,
    session_db=None,
    has_flush=True,
    _iters_since_skill: int = 0,
    _skill_nudge_interval: int = 10,
    valid_tool_names: set | None = None,
) -> SimpleNamespace:
    agent = SimpleNamespace(
        api_mode="acp_client",
        acp_command="fake-acp",
        acp_args=[],
        session_cwd="/tmp",
        _acp_session=None,
        _iters_since_skill=_iters_since_skill,
        _skill_nudge_interval=_skill_nudge_interval,
        valid_tool_names=valid_tool_names or set(),
        _fire_stream_delta=None,
        _sync_external_memory_for_turn=MagicMock(),
        _spawn_background_review=MagicMock(),
        model=None,
        acp_mcp_servers=[],
    )
    agent._session_db = session_db
    if has_flush:
        agent._flush_messages_to_session_db = MagicMock()
    else:
        # Agent without _flush_messages_to_session_db (older class shape)
        pass
    return agent


def _mock_session(turn_result: TurnResult) -> MagicMock:
    mock = MagicMock()
    mock.run_turn.return_value = turn_result
    mock.close.return_value = None
    return mock


def _inject_session(agent, session_mock: MagicMock) -> None:
    agent._acp_session = session_mock


# ---------------------------------------------------------------------------
# Tests: projected messages flushed to session DB
# ---------------------------------------------------------------------------


class TestProjectedMessagesFlush:
    def test_projected_messages_flushed_to_session_db(self):
        """When projected messages exist and _session_db is set, flush is called."""
        session_db = MagicMock()  # truthy
        agent = _make_agent(session_db=session_db)
        turn = TurnResult(
            final_text="hello",
            projected_messages=[{"role": "assistant", "content": "hello"}],
        )
        _inject_session(agent, _mock_session(turn))
        messages = [{"role": "user", "content": "hi"}]

        run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=messages,
            effective_task_id="t1",
        )

        agent._flush_messages_to_session_db.assert_called_once_with(messages)

    def test_flush_not_called_when_no_projected_messages(self):
        """When turn has no projected_messages, flush is not called."""
        session_db = MagicMock()
        agent = _make_agent(session_db=session_db)
        turn = TurnResult(final_text="", projected_messages=[])
        _inject_session(agent, _mock_session(turn))

        run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[],
            effective_task_id="t1",
        )

        agent._flush_messages_to_session_db.assert_not_called()

    def test_flush_exception_does_not_break_return(self):
        """If _flush_messages_to_session_db raises, the return is still valid."""
        session_db = MagicMock()
        agent = _make_agent(session_db=session_db)
        agent._flush_messages_to_session_db.side_effect = RuntimeError("DB locked")
        turn = TurnResult(
            final_text="ok",
            projected_messages=[{"role": "assistant", "content": "ok"}],
        )
        _inject_session(agent, _mock_session(turn))

        result = run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[],
            effective_task_id="t1",
        )

        # The exception was caught; result is normal
        assert result["final_response"] == "ok"
        assert result["completed"] is True

    def test_flush_not_called_when_session_db_is_none(self):
        """When _session_db is None, flush is not called."""
        agent = _make_agent(session_db=None)
        turn = TurnResult(
            final_text="ok",
            projected_messages=[{"role": "assistant", "content": "ok"}],
        )
        _inject_session(agent, _mock_session(turn))

        run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[],
            effective_task_id="t1",
        )

        agent._flush_messages_to_session_db.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: agent_persisted=True on all return paths
# ---------------------------------------------------------------------------


class TestAgentPersisted:
    def test_happy_path_returns_agent_persisted_true(self):
        """Successful turn return must include agent_persisted=True."""
        agent = _make_agent(session_db=MagicMock())
        turn = TurnResult(final_text="hello")
        _inject_session(agent, _mock_session(turn))

        result = run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[],
            effective_task_id="t1",
        )

        assert result.get("agent_persisted") is True

    def test_no_db_still_agent_persisted_true(self):
        """Even without _session_db, agent_persisted must be True.

        The early-return persistence contract means the agent is the sole
        persister; the gateway must NOT do its own DB write. When there is no
        DB there is nothing to persist, but the gateway still needs to know it
        should not write — so we report agent_persisted=True unconditionally.
        """
        agent = _make_agent(session_db=None)
        turn = TurnResult(final_text="hello")
        _inject_session(agent, _mock_session(turn))

        result = run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[],
            effective_task_id="t1",
        )

        assert result.get("agent_persisted") is True

    def test_error_turn_returns_agent_persisted_true(self):
        """Error turn return must also include agent_persisted=True.

        Mirrors codex_runtime: even on crash, the early-return path already
        happened (the user message was flushed at turn start), so the gateway
        must not re-write it.
        """
        agent = _make_agent(session_db=MagicMock())
        session_mock = MagicMock()
        session_mock.run_turn.side_effect = RuntimeError("crashed")
        _inject_session(agent, session_mock)

        result = run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=[],
            effective_task_id="t1",
        )

        assert result.get("agent_persisted") is True
        assert result["completed"] is False


# ---------------------------------------------------------------------------
# Tests: external memory receives messages= kwarg
# ---------------------------------------------------------------------------


class TestExternalMemoryMessagesContract:
    def test_external_memory_receives_messages_kwarg(self):
        """_sync_external_memory_for_turn must be called with messages= kwarg."""
        agent = _make_agent(session_db=MagicMock())
        turn = TurnResult(final_text="answer", interrupted=False, error=None)
        _inject_session(agent, _mock_session(turn))
        messages = [{"role": "user", "content": "hi"}]

        run_acp_client_turn(
            agent,
            user_message="hi",
            original_user_message="hi",
            messages=messages,
            effective_task_id="t1",
        )

        agent._sync_external_memory_for_turn.assert_called_once()
        call_kwargs = agent._sync_external_memory_for_turn.call_args[1]
        assert "messages" in call_kwargs
        # messages must be the SAME list object, not a copy
        assert call_kwargs["messages"] is messages

    def test_external_memory_receives_projected_messages_in_list(self):
        """The messages list passed to external memory must contain the projected
        assistant message (proving it was extended before the sync call)."""
        agent = _make_agent(session_db=MagicMock())
        turn = TurnResult(
            final_text="reply",
            projected_messages=[{"role": "assistant", "content": "reply"}],
            interrupted=False,
            error=None,
        )
        _inject_session(agent, _mock_session(turn))
        messages = [{"role": "user", "content": "q"}]

        run_acp_client_turn(
            agent,
            user_message="q",
            original_user_message="q",
            messages=messages,
            effective_task_id="t1",
        )

        call_kwargs = agent._sync_external_memory_for_turn.call_args[1]
        sync_messages = call_kwargs["messages"]
        # Must contain both user and projected assistant
        assert any(m.get("role") == "user" for m in sync_messages)
        assert any(m.get("role") == "assistant" for m in sync_messages)


# ---------------------------------------------------------------------------
# Tests: real SessionDB exactly-once persistence (no flush mock)
# ---------------------------------------------------------------------------


def test_acp_turn_persists_each_message_exactly_once():
    """The ACP client turn must persist user and assistant messages exactly
    once to a real SessionDB, proving no #860/#42039 duplicate-write regression.

    Mirrors ``test_codex_app_server_persist.py::test_codex_turn_persists_each_message_exactly_once``:
    uses a real SessionDB + real AIAgent with real
    ``_flush_messages_to_session_db`` (NOT mocked), a real turn-start flush on
    the user message, and a mock ACP session that returns a single projected
    assistant message. The dedup marker (``_DB_PERSISTED_MARKER``) stamped on
    the user dict by the turn-start flush must prevent the ACP-runtime flush
    from re-inserting it.
    """
    import tempfile
    import shutil
    from pathlib import Path

    from hermes_state import SessionDB
    from run_agent import AIAgent

    tmp = tempfile.mkdtemp(prefix="acp_persist_")
    try:
        db = SessionDB(Path(tmp) / "state.db")
        sid = "sess-acp-once"
        db.create_session(session_id=sid, source="telegram", model="acp")

        # Real agent bound to this DB/session, minimal construction.
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=db,
            session_id=sid,
        )
        agent._session_db_created = True
        agent.tool_progress_callback = None

        # Mock the ACP session to return a single projected assistant message.
        # This is the ONLY mock — _flush_messages_to_session_db is real.
        agent._acp_session = _mock_session(
            TurnResult(
                final_text="ACP_ASSISTANT",
                projected_messages=[
                    {"role": "assistant", "content": "ACP_ASSISTANT"}
                ],
            )
        )

        # Model the real flow: the inbound user turn is flushed at turn start
        # (turn_context._persist_session) on the SAME `messages` list the ACP
        # path later reuses. That flush stamps _DB_PERSISTED_MARKER on the user
        # dict, so the ACP-path flush skips it — no duplicate.
        user_msg = {"role": "user", "content": "USER_TURN"}
        messages = [user_msg]
        agent._flush_messages_to_session_db(messages)  # turn-start flush

        result = run_acp_client_turn(
            agent,
            user_message="USER_TURN",
            original_user_message="USER_TURN",
            messages=messages,
            effective_task_id="task-1",
        )
        assert result["agent_persisted"] is True

        rows = db.get_messages(sid, include_inactive=True)
        contents = [r["content"] for r in rows]
        # Exactly one user turn, exactly one assistant turn — no duplicates.
        assert contents.count("USER_TURN") == 1, contents
        assert contents.count("ACP_ASSISTANT") == 1, contents
        # session_search can now see the ACP conversation.
        hits = {r["session_id"] for r in db.search_messages("ACP_ASSISTANT")}
        assert sid in hits

        db.close()
    finally:
        shutil.rmtree(tmp)
