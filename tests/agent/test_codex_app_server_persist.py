"""Regression for #49225 — codex app-server turns must reach the session DB
exactly once.

The codex app-server runtime (``run_codex_app_server_turn``) is an early-return
path that bypasses ``conversation_loop`` and therefore never runs the loop's
per-step ``_persist_session()`` flushes. Before the fix, the projected
assistant/tool messages were persisted *nowhere* (state.db got only
session_meta rows), leaving ``session_search`` (FTS) and conversation-distill
blind to real gateway conversations.

The fix has the codex runtime flush its own projected messages via
``_flush_messages_to_session_db()`` (idempotent through the intrinsic
``_DB_PERSISTED_MARKER``) and return ``agent_persisted=True`` so the gateway
skips its own ``append_to_transcript`` DB write. This is critical: the inbound
user turn is already flushed at turn start (``turn_context._persist_session``),
and ``append_message`` is a raw INSERT with no dedup — a gateway re-write would
duplicate the user turn (#860 / #42039). This test locks in:

1. ``run_codex_app_server_turn`` flushes projected messages and returns
   ``agent_persisted=True``.
2. Exactly-once persistence: the already-flushed user turn is NOT re-written,
   and the new projected assistant message lands once.
3. The gateway resolution expression preserves standard-runtime behaviour.
"""

from copy import deepcopy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.codex_runtime import (
    _normalize_codex_projected_messages,
    run_codex_app_server_turn,
)
from hermes_state import SessionDB
from run_agent import AIAgent


def _make_turn():
    return SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-1",
        projected_messages=[{"role": "assistant", "content": "CODEX_ASSISTANT"}],
        tool_iterations=0,
        final_text="CODEX_ASSISTANT",
        should_retire=False,
    )


def _make_agent(session_db=None, session_id="sess-codex"):
    agent = MagicMock()
    # Pre-seed the session so run_codex_app_server_turn skips the spawn block.
    agent._codex_session = MagicMock()
    agent._codex_session.ensure_started.return_value = "thread-1"
    agent._codex_session.run_turn.return_value = _make_turn()
    agent.session_cwd = str(Path.cwd().resolve())
    agent.codex_app_server_require_explicit_cwd = False
    agent.codex_app_server_workspace_roots = [agent.session_cwd]
    agent._codex_session_cwd = agent.session_cwd
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = session_db
    agent._session_db_created = True
    agent.session_id = session_id
    return agent


def test_codex_success_flushes_and_reports_persisted():
    """Codex success turn must self-persist and return agent_persisted=True."""
    agent = _make_agent(session_db=None)  # no DB -> flush is a no-op, still True
    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )
    assert result["completed"] is True
    assert isinstance(result["messages"][-1]["timestamp"], float)
    # With the agent as sole persister, the gateway must SKIP its DB write.
    assert result["agent_persisted"] is True


def test_codex_failed_projection_flush_reports_not_persisted():
    """Gateway must be allowed to recover an unpersisted projected suffix."""
    agent = _make_agent(session_db=MagicMock())
    agent._flush_messages_to_session_db.return_value = False

    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-flush-failure",
    )

    assert result["agent_persisted"] is False


def test_codex_failed_flush_without_projections_reports_not_persisted():
    """A failed turn still has an inbound user row that gateway may need to save."""
    agent = _make_agent(session_db=MagicMock())
    turn = _make_turn()
    turn.projected_messages = []
    turn.final_text = ""
    turn.error = "startup failed"
    agent._codex_session.run_turn.return_value = turn
    agent._flush_messages_to_session_db.return_value = False

    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-zero-projection-flush-failure",
    )

    agent._flush_messages_to_session_db.assert_called_once()
    assert result["agent_persisted"] is False


def test_codex_initial_user_echo_is_skipped_but_steer_is_retained():
    """Only turn/start's echo is redundant; accepted turn/steer text is durable."""
    agent = _make_agent(session_db=None)
    authoritative = (
        "handoff [HERMES_RUNTIME_CWD="
        f"{agent.session_cwd}] continue the task"
    )
    codex_echo = "handoff  continue the task"
    turn = _make_turn()
    turn.projected_messages = [
        {
            "role": "user",
            "content": codex_echo,
            "_codex_initial_user_echo": True,
        },
        {"role": "user", "content": "do not modify migrations"},
        # A steer may intentionally repeat turn/start's exact text. It is real
        # user input and must remain durable once the explicit echo was skipped.
        {"role": "user", "content": codex_echo},
        {"role": "assistant", "content": "CODEX_ASSISTANT"},
    ]
    agent._codex_session.run_turn.return_value = turn
    messages = [{"role": "user", "content": authoritative}]

    result = run_codex_app_server_turn(
        agent,
        user_message=authoritative,
        original_user_message=authoritative,
        messages=messages,
        effective_task_id="task-echo",
    )

    user_rows = [m for m in result["messages"] if m.get("role") == "user"]
    assert [m["content"] for m in user_rows] == [
        authoritative,
        "do not modify migrations",
        codex_echo,
    ]
    assert result["final_response"] == "CODEX_ASSISTANT"


def test_codex_commentary_merges_into_following_tool_call_envelope():
    """Persist the pre-tool commentary and call as one assistant message."""
    agent = _make_agent(session_db=None)
    call = {
        "id": "codex_exec_call-1",
        "type": "function",
        "function": {"name": "exec_command", "arguments": '{"cmd":"pwd"}'},
    }
    turn = _make_turn()
    turn.projected_messages = [
        {
            "role": "assistant",
            "content": "I’ll inspect the workspace first.",
            "reasoning": "commentary reasoning",
            "display_kind": "commentary",
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [call],
            "reasoning": "tool reasoning",
            "reasoning_content": "tool reasoning content",
            "finish_reason": "tool_calls",
        },
        {"role": "tool", "tool_call_id": call["id"], "content": "/repo"},
    ]
    turn.final_text = ""
    agent._codex_session.run_turn.return_value = turn

    result = run_codex_app_server_turn(
        agent,
        user_message="inspect",
        original_user_message="inspect",
        messages=[{"role": "user", "content": "inspect"}],
        effective_task_id="task-commentary-tool",
    )

    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "tool",
    ]
    assistant, tool = result["messages"][1:]
    assert assistant["content"] == "I’ll inspect the workspace first."
    assert assistant["tool_calls"] == [call]
    assert assistant["reasoning"] == "commentary reasoning\ntool reasoning"
    assert assistant["reasoning_content"] == "tool reasoning content"
    assert assistant["display_kind"] == "commentary"
    assert assistant["finish_reason"] == "tool_calls"
    assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]


def test_codex_final_assistant_content_stays_after_tool_result():
    """A final assistant item is not consumed by the pre-tool normalization."""
    agent = _make_agent(session_db=None)
    turn = _make_turn()
    turn.projected_messages = [
        {"role": "assistant", "content": "Checking now."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {"role": "assistant", "content": "The check passed."},
    ]
    turn.final_text = "The check passed."
    agent._codex_session.run_turn.return_value = turn

    result = run_codex_app_server_turn(
        agent,
        user_message="check",
        original_user_message="check",
        messages=[{"role": "user", "content": "check"}],
        effective_task_id="task-final-content",
    )

    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert result["messages"][-1]["content"] == "The check passed."
    assert result["final_response"] == "The check passed."


def test_codex_projection_normalization_does_not_mutate_or_alias_inputs():
    projections = [
        {
            "role": "assistant",
            "content": "Working.",
            "reasoning_details": [{"step": "commentary"}],
        },
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"arguments": "{}"}}],
            "codex_message_items": [{"id": "tool-item"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    before = deepcopy(projections)

    normalized = _normalize_codex_projected_messages(projections)

    assert projections == before
    assert normalized == [
        {
            "role": "assistant",
            "content": "Working.",
            "reasoning_details": [{"step": "commentary"}],
            "tool_calls": [{"id": "call-1", "function": {"arguments": "{}"}}],
            "codex_message_items": [{"id": "tool-item"}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
    ]
    assert normalized[0] is not projections[0]
    assert normalized[0]["tool_calls"] is not projections[1]["tool_calls"]
    assert normalized[1] is not projections[2]


def test_codex_user_interrupt_is_reported_and_cleared():
    agent = _make_agent(session_db=None)
    turn = _make_turn()
    turn.interrupted = True
    turn.final_text = ""
    agent._codex_session.run_turn.return_value = turn
    agent._interrupt_requested = True
    agent._interrupt_message = "new correction"

    def clear_interrupt():
        agent._interrupt_requested = False
        agent._interrupt_message = None

    agent.clear_interrupt.side_effect = clear_interrupt
    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-1",
    )

    assert result["interrupted"] is True
    assert result["interrupt_message"] == "new correction"
    agent.clear_interrupt.assert_called_once_with()
    assert agent._interrupt_requested is False


def test_codex_turn_persists_each_message_exactly_once():
    """The user turn (flushed at turn start) must not be duplicated; the
    projected assistant message must land once.  Uses a real SessionDB and the
    real AIAgent._flush_messages_to_session_db to prove no #860/#42039
    duplicate-write regression on the codex path."""
    tmp = tempfile.mkdtemp(prefix="codex_persist_")
    try:
        db = SessionDB(Path(tmp) / "state.db")
        sid = "sess-codex-once"
        db.create_session(session_id=sid, source="telegram", model="codex")

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
        codex_session = MagicMock()
        codex_session.ensure_started.return_value = "thread-1"
        turn = _make_turn()
        persisted_call = {
            "id": "codex_exec_persisted",
            "type": "function",
            "function": {
                "name": "exec_command",
                "arguments": '{"cmd":"pwd"}',
            },
        }
        turn.projected_messages = [
            {
                "role": "assistant",
                "content": "PERSISTED_COMMENTARY",
                "display_kind": "commentary",
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [persisted_call],
                "finish_reason": "tool_calls",
            },
            {
                "role": "tool",
                "tool_call_id": persisted_call["id"],
                "content": "PERSISTED_TOOL_RESULT",
            },
            {"role": "assistant", "content": "CODEX_ASSISTANT"},
        ]
        codex_session.run_turn.return_value = turn
        session_cwd = str(Path.cwd().resolve())
        setattr(agent, "_codex_session", codex_session)
        setattr(agent, "session_cwd", session_cwd)
        setattr(agent, "_codex_session_cwd", session_cwd)
        agent.tool_progress_callback = None

        # Model the real flow: the inbound user turn is flushed at turn start
        # (turn_context._persist_session) on the SAME `messages` list the codex
        # path later reuses. That flush stamps _DB_PERSISTED_MARKER on the user
        # dict, so the codex-path flush skips it — no duplicate.
        user_msg = {"role": "user", "content": "USER_TURN"}
        messages = [user_msg]
        agent._flush_messages_to_session_db(messages)  # turn-start flush

        result = run_codex_app_server_turn(
            agent,
            user_message="USER_TURN",
            original_user_message="USER_TURN",
            messages=messages,
            effective_task_id="task-1",
        )
        assert result["agent_persisted"] is True

        rows = db.get_messages(sid, include_inactive=True)
        contents = [r["content"] for r in rows]
        # Exactly one of each projected row, with no assistant→assistant split
        # at the commentary/tool-call boundary.
        assert contents.count("USER_TURN") == 1, contents
        assert contents.count("PERSISTED_COMMENTARY") == 1, contents
        assert contents.count("PERSISTED_TOOL_RESULT") == 1, contents
        assert contents.count("CODEX_ASSISTANT") == 1, contents
        assert [row["role"] for row in rows] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
        commentary_row = rows[1]
        assert commentary_row["tool_calls"] == [persisted_call]
        assert commentary_row["finish_reason"] == "tool_calls"
        assert commentary_row["display_kind"] == "commentary"
        assert rows[2]["tool_call_id"] == persisted_call["id"]
        assistant_row = next(
            row for row in rows if row["content"] == "CODEX_ASSISTANT"
        )
        assert isinstance(assistant_row["timestamp"], float)
        # session_search can now see the codex conversation.
        hits = {r["session_id"] for r in db.search_messages("CODEX_ASSISTANT")}
        assert sid in hits
    finally:
        import shutil

        shutil.rmtree(tmp)


class TestGatewayPersistedResolution:
    """The gateway default must preserve standard-runtime skip-db behaviour."""

    @staticmethod
    def _resolve_persistence_block(agent_result, session_db_present):
        # gateway/run.py persistence block:
        #   agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)
        return agent_result.get("agent_persisted", session_db_present)

    @staticmethod
    def _resolve_passthrough(result_holder0):
        # gateway/run.py result_holder passthrough:
        #   result_holder[0].get("agent_persisted", True) if result_holder[0] else True
        return result_holder0.get("agent_persisted", True) if result_holder0 else True

    def test_codex_result_keeps_gateway_skip(self):
        # Codex now self-persists → gateway must SKIP (agent_persisted True).
        codex = {"agent_persisted": True}
        assert self._resolve_persistence_block(codex, True) is True
        assert self._resolve_persistence_block(codex, False) is True
        assert self._resolve_passthrough(codex) is True

    def test_standard_runtime_preserves_skip_db(self):
        # Standard runtime omits the key → old behaviour: skip iff DB present.
        standard = {"final_response": "ok"}
        assert self._resolve_persistence_block(standard, True) is True
        assert self._resolve_persistence_block(standard, False) is False
        assert self._resolve_passthrough(standard) is True

    def test_missing_result_holder_defaults_persisted(self):
        assert self._resolve_passthrough(None) is True
