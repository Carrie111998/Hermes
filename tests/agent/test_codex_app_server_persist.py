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

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.codex_runtime import run_codex_app_server_turn
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
    agent._codex_session.run_turn.return_value = _make_turn()
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent._session_db = session_db
    agent._session_db_created = True
    agent.session_id = session_id
    agent._interrupt_requested = False
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
    # With the agent as sole persister, the gateway must SKIP its DB write.
    assert result["agent_persisted"] is True


def test_codex_initial_exception_counts_attempt(monkeypatch):
    from agent import codex_runtime

    agent = _make_agent(session_db=None)
    agent._codex_session.run_turn.side_effect = RuntimeError("initial turn crashed")
    recorded_turns = []

    def record_usage(_agent, turn):
        recorded_turns.append(getattr(turn, "turn_id", None))
        return {}

    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_usage",
        record_usage,
    )

    result = run_codex_app_server_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[{"role": "user", "content": "hello"}],
        effective_task_id="task-initial-error",
    )

    assert result["api_calls"] == 1
    assert result["error"] == "initial turn crashed"
    assert agent._codex_session is None
    assert recorded_turns == [None]


def test_codex_false_stop_continues_same_app_server_thread(monkeypatch):
    from agent import codex_runtime

    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    request = "Continue implementing the fix in /app until tests pass."
    first = SimpleNamespace(
        interrupted=False,
        native_completed=True,
        error=None,
        thread_id="thread-1",
        turn_id="turn-1",
        projected_messages=[
            {"role": "assistant", "content": "Running tests.", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "25 failed"},
            {
                "role": "assistant",
                "content": "I'm now implementing the remaining workspace fixes and rerunning tests.",
            },
        ],
        tool_iterations=1,
        final_text="I'm now implementing the remaining workspace fixes and rerunning tests.",
        should_retire=False,
        compacted=False,
    )
    second = SimpleNamespace(
        interrupted=False,
        native_completed=True,
        error=None,
        thread_id="thread-1",
        turn_id="turn-2",
        projected_messages=[{"role": "assistant", "content": "Implemented and verified: 122 passed."}],
        tool_iterations=0,
        final_text="Implemented and verified: 122 passed.",
        should_retire=False,
        compacted=False,
    )
    agent._codex_session.run_turn.side_effect = [first, second]
    recorded_turns = []
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_usage",
        lambda _agent, turn: recorded_turns.append(turn.turn_id) or {},
    )
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_compaction",
        lambda _agent, turn: False,
    )

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-1",
    )

    assert agent._codex_session.run_turn.call_count == 2
    assert agent._codex_session.run_turn.call_args_list[0].kwargs["user_input"] == request
    assert "still incomplete" in agent._codex_session.run_turn.call_args_list[1].kwargs["user_input"]
    assert result["final_response"] == second.final_text
    assert result["api_calls"] == 2
    assert recorded_turns == ["turn-1", "turn-2"]
    assert result["messages"][-3:] == first.projected_messages + second.projected_messages
    durable_text = "\n".join(str(message.get("content") or "") for message in result["messages"])
    assert "I'm now implementing the remaining" not in durable_text
    assert "still incomplete" not in durable_text


def _false_stop_turn(turn_id: str, *, native_completed: bool = True):
    text = "I'm now implementing the remaining workspace fixes and rerunning tests."
    return SimpleNamespace(
        interrupted=False,
        native_completed=native_completed,
        error=None,
        thread_id="thread-1",
        turn_id=turn_id,
        projected_messages=[
            {"role": "assistant", "content": "Running tests.", "tool_calls": [{"id": f"tool-{turn_id}"}]},
            {"role": "tool", "tool_call_id": f"tool-{turn_id}", "content": "25 failed"},
            {"role": "assistant", "content": text},
        ],
        tool_iterations=1,
        final_text=text,
        should_retire=False,
        compacted=False,
    )


def test_codex_recovery_error_stops_and_preserves_real_tool_events():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    first = _false_stop_turn("turn-1")
    failed = SimpleNamespace(
        interrupted=False,
        native_completed=False,
        error="transport failed",
        thread_id="thread-1",
        turn_id="turn-2",
        projected_messages=[],
        tool_iterations=0,
        final_text="",
        should_retire=True,
        compacted=False,
    )
    agent._codex_session.run_turn.side_effect = [first, failed]
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-error",
    )

    assert result["api_calls"] == 2
    assert result["error"] == "transport failed"
    assert agent._codex_session is None
    durable_text = "\n".join(
        str(message.get("content") or "") for message in result["messages"]
    )
    assert first.final_text in result["final_response"]
    assert "PAUSED" in result["final_response"]
    assert "Running tests." in durable_text
    assert "25 failed" in durable_text
    assert "I'm now implementing the remaining" in durable_text
    assert "still incomplete" not in durable_text
    agent._sync_external_memory_for_turn.assert_not_called()
    agent._spawn_background_review.assert_not_called()


def test_codex_recovery_exception_preserves_completed_turn(monkeypatch):
    from agent import codex_runtime

    agent = _make_agent(session_db=object())
    agent._flush_messages_to_session_db.return_value = True
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    first = _false_stop_turn("turn-1")
    session = agent._codex_session
    session.run_turn.side_effect = [first, RuntimeError("retry crashed")]
    recorded_turns = []

    def record_usage(_agent, turn):
        recorded_turns.append(getattr(turn, "turn_id", None))
        return {}

    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_usage",
        record_usage,
    )
    monkeypatch.setattr(
        codex_runtime,
        "_record_codex_app_server_compaction",
        lambda _agent, turn: False,
    )
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-exception",
    )

    assert session.run_turn.call_count == 2
    assert agent._codex_session is None
    assert result["api_calls"] == 2
    assert result["error"] == "retry crashed"
    assert first.final_text in result["final_response"]
    durable_text = "\n".join(
        str(message.get("content") or "") for message in result["messages"]
    )
    assert "Running tests." in durable_text
    assert "25 failed" in durable_text
    assert first.final_text in durable_text
    agent._flush_messages_to_session_db.assert_called_once()
    flushed_messages = agent._flush_messages_to_session_db.call_args.args[0]
    flushed_text = "\n".join(
        str(message.get("content") or "") for message in flushed_messages
    )
    assert "25 failed" in flushed_text
    assert first.final_text in flushed_text
    assert recorded_turns == ["turn-1", None]


def test_codex_recovery_interruption_stops_without_third_turn():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    interrupted = SimpleNamespace(
        interrupted=True,
        native_completed=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-2",
        projected_messages=[],
        tool_iterations=0,
        final_text="",
        should_retire=False,
        compacted=False,
    )
    first = _false_stop_turn("turn-1")
    agent._codex_session.run_turn.side_effect = [first, interrupted]
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-interrupt",
    )

    assert agent._codex_session.run_turn.call_count == 2
    assert result["completed"] is False
    assert result["partial"] is True
    durable_text = "\n".join(
        str(message.get("content") or "") for message in result["messages"]
    )
    assert first.final_text in result["final_response"]
    assert "PAUSED" in result["final_response"]
    assert "25 failed" in durable_text
    assert "I'm now implementing the remaining" in durable_text
    assert "still incomplete" not in durable_text


def test_codex_user_interrupt_during_empty_recovery_restores_checkpoint():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    first = _false_stop_turn("turn-1")
    interrupted_recovery = SimpleNamespace(
        interrupted=True,
        native_completed=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-2",
        projected_messages=[],
        tool_iterations=0,
        final_text="",
        should_retire=False,
        compacted=False,
    )

    def _run_turn(*, user_input):
        if agent._codex_session.run_turn.call_count == 2:
            agent._interrupt_requested = True
        return first if agent._codex_session.run_turn.call_count == 1 else interrupted_recovery

    agent._codex_session.run_turn.side_effect = _run_turn
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-user-interrupt-recovery",
    )

    assert agent._codex_session.run_turn.call_count == 2
    assert result["interrupted"] is True
    assert result["final_response"] == first.final_text
    durable_text = "\n".join(
        str(message.get("content") or "") for message in result["messages"]
    )
    assert "25 failed" in durable_text
    assert first.final_text in durable_text
    assert "still incomplete" not in durable_text


def test_codex_deadline_accepted_text_does_not_auto_continue():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    turn = _false_stop_turn("turn-deadline", native_completed=False)
    agent._codex_session.run_turn.return_value = turn
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-deadline",
    )

    assert agent._codex_session.run_turn.call_count == 1
    assert result["final_response"] == turn.final_text


def test_codex_user_interrupt_between_native_turn_and_nudge_stops_recovery():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    first = _false_stop_turn("turn-interrupt-race")
    second = SimpleNamespace(
        interrupted=False,
        native_completed=True,
        error=None,
        thread_id="thread-1",
        turn_id="turn-should-not-run",
        projected_messages=[{"role": "assistant", "content": "unexpected"}],
        tool_iterations=0,
        final_text="unexpected",
        should_retire=False,
        compacted=False,
    )

    def _run_turn(*, user_input):
        if agent._codex_session.run_turn.call_count == 1:
            agent._interrupt_requested = True
            return first
        return second

    agent._codex_session.run_turn.side_effect = _run_turn
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-interrupt-race",
    )

    assert agent._codex_session.run_turn.call_count == 1
    assert result["interrupted"] is True
    assert result["final_response"] == first.final_text


def test_codex_pause_does_not_overwrite_non_candidate_tool_call_row():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    turn = _false_stop_turn("turn-projection-mismatch")
    candidate_text = turn.final_text
    turn.projected_messages = turn.projected_messages[:-1]
    tool_call_projection = next(
        message for message in turn.projected_messages if message.get("tool_calls")
    )
    tool_call_projection["content"] = candidate_text
    agent._codex_session.run_turn.return_value = turn
    agent.iteration_budget.consume.return_value = False
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-projection-mismatch",
    )

    assert agent._codex_session.run_turn.call_count == 1
    tool_call_row = next(
        message for message in result["messages"] if message.get("tool_calls")
    )
    assert tool_call_row["content"] == candidate_text
    assert tool_call_row.get("tool_calls")
    assert result["messages"][-1]["role"] == "assistant"
    assert not result["messages"][-1].get("tool_calls")
    assert "PAUSED" in result["messages"][-1]["content"]


def test_codex_false_stop_respects_outer_iteration_budget():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    first = _false_stop_turn("turn-budget")
    agent._codex_session.run_turn.return_value = first
    agent.iteration_budget.consume.return_value = False
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-no-headroom",
    )

    assert agent._codex_session.run_turn.call_count == 1
    assert result["api_calls"] == 1
    assert "PAUSED" in result["final_response"]
    assert "PAUSED" in result["messages"][-1]["content"]


def test_codex_false_stop_budget_exhaustion_is_visible():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    agent._codex_session.run_turn.side_effect = [
        _false_stop_turn("turn-1"),
        _false_stop_turn("turn-2"),
        _false_stop_turn("turn-3"),
    ]
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-budget",
    )

    assert agent._codex_session.run_turn.call_count == 3
    assert "PAUSED" in result["final_response"]
    assert "budget exhausted" in result["final_response"]


def test_codex_false_stop_loop_uses_shared_continuation_cap(monkeypatch):
    from agent import codex_runtime

    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    monkeypatch.setattr(codex_runtime, "MAX_TERMINAL_CONTINUATIONS", 1, raising=False)
    session = agent._codex_session
    session.run_turn.side_effect = [
        _false_stop_turn("turn-1"),
        _false_stop_turn("turn-2"),
    ]
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-shared-cap",
    )

    assert session.run_turn.call_count == 2


def test_codex_partial_retry_error_returns_checkpoint_with_pause_notice():
    agent = _make_agent(session_db=None)
    agent.api_mode = "codex_app_server"
    agent.model = "gpt-5.6-terra"
    agent._intent_ack_continuation = "auto"
    agent.valid_tool_names = {"terminal"}
    agent._strip_think_blocks.side_effect = lambda content: content
    first = _false_stop_turn("turn-1")
    failed = SimpleNamespace(
        interrupted=False,
        native_completed=False,
        error="transport failed",
        thread_id="thread-1",
        turn_id="turn-2",
        projected_messages=[{"role": "assistant", "content": "partial retry output"}],
        tool_iterations=0,
        final_text="partial retry output",
        should_retire=True,
        compacted=False,
    )
    agent._codex_session.run_turn.side_effect = [first, failed]
    request = "Continue implementing the fix in /app until tests pass."

    result = run_codex_app_server_turn(
        agent,
        user_message=request,
        original_user_message=request,
        messages=[{"role": "user", "content": request}],
        effective_task_id="task-partial-retry-error",
    )

    assert result["error"] == "transport failed"
    assert first.final_text in result["final_response"]
    assert "PAUSED" in result["final_response"]
    assert "partial retry output" not in result["final_response"]


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
        agent._codex_session = MagicMock()
        agent._codex_session.run_turn.return_value = _make_turn()
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
        # Exactly one user turn, exactly one assistant turn — no duplicates.
        assert contents.count("USER_TURN") == 1, contents
        assert contents.count("CODEX_ASSISTANT") == 1, contents
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
