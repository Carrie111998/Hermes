from types import SimpleNamespace
from typing import Any

from agent.turn_finalizer import finalize_turn


class FakeAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = True
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages: list[dict[str, Any]] | None = None
        self._persist_user_message_idx: int | None = None
        self._persist_user_message_override: Any = None
        self._persist_user_message_timestamp: float | None = None
        self._turn_completion_explainer = False

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        # Capture the durable write before finalization restores API-local
        # guidance to the returned/live transcript.
        self.persisted_messages = [dict(message) for message in messages]

    def _apply_persist_user_message_override(self, messages):
        idx = self._persist_user_message_idx
        override = self._persist_user_message_override
        if idx is not None and override is not None:
            messages[idx]["content"] = override

    def _file_mutation_verifier_enabled(self):
        return False

    def _turn_completion_explainer_enabled(self):
        return self._turn_completion_explainer

    def _format_turn_completion_explanation(self, reason):
        return f"⚠️ No reply: explained ({reason})"

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass






def test_final_response_closes_tool_tail_before_persistence(monkeypatch):
    """A recovered/previewed final response must be durable in session history.

    Regression for turns where the caller receives a non-empty final_response,
    but the message transcript still ends at a tool result. If persisted that
    way, the next turn reloads a stale/malformed history and can appear to loop
    because the assistant's visible final answer is missing from durable state.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "I'll check.",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert result["messages"][-1] == {"role": "assistant", "content": "Done."}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {"role": "assistant", "content": "Done."}


def test_empty_final_response_explainer_closes_tool_tail_before_persist(monkeypatch):
    """Explainer-synthesized replies must hit the #43849 persist chokepoint.

    When inbound ``final_response`` is falsy (``""`` / ``None``), the append
    gate used to skip, persist left a tool-tailed transcript, and only then
    did the turn-completion explainer replace the delivered text — so the
    caller saw an explanation while durable history still ended on ``tool``,
    recreating #48879 ``tool → user`` risk on the next turn.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._turn_completion_explainer = True
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert "No reply" in (result["final_response"] or "")
    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == result["final_response"]
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1]["role"] == "assistant"
    assert agent.persisted_messages[-1]["content"] == result["final_response"]


def test_none_final_response_explainer_closes_tool_tail_before_persist(monkeypatch):
    """Same invariant as the empty-string case, with inbound ``None``."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._turn_completion_explainer = True
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert "No reply" in (result["final_response"] or "")
    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == result["final_response"]
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1]["content"] == result["final_response"]


def test_final_response_fills_pure_tool_call_tail(monkeypatch):
    """A tail assistant row that is a *pure tool-call turn* carries no answer.

    The role check alone ("tail is assistant ⇒ nothing to do") leaves the
    #43849/#44100 invariant unmet when the tail is ``assistant(tool_calls)``
    with no text of its own: the caller and the gateway already delivered
    ``final_response``, but it never reaches the transcript. The next turn then
    replays the user backlog and the model re-answers it — the exact symptom
    that block exists to prevent.
    """
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
        },
    ]

    result = finalize_turn(
        agent,
        final_response="Here is your answer.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    persisted = agent.persisted_messages
    assert any(
        m.get("role") == "assistant" and m.get("content") == result["final_response"]
        for m in persisted
    ), "delivered final_response never reached the durable transcript"
    # Filled in place — no assistant→assistant pair, tool_calls preserved.
    assert persisted[-1]["content"] == "Here is your answer."
    assert persisted[-1]["tool_calls"]
    assert sum(1 for m in persisted if m.get("role") == "assistant") == 1






def test_final_response_fill_invalidates_flush_scan_cursor():
    """The fill's marker pop must invalidate the bounded flush-scan cursor.

    The cursor (run_agent.py) skips the identity-matched prefix of its
    previous snapshot assuming no live dict loses ``_db_persisted`` in place
    — the fill is the one path that pops it. Without invalidation, the
    turn-end flush skips the filled row as 'already stamped' and the
    delivered answer never reaches state.db (the #43849 class resurfacing).
    """
    agent = FakeAgent()
    agent._db_flush_scan_prefix = ["prior-snapshot"]
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "type": "function",
                 "function": {"name": "f", "arguments": "{}"}}
            ],
            "_db_persisted": True,
        },
    ]

    finalize_turn(
        agent,
        final_response="Here is your answer.",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="t",
        turn_id="tid",
        user_message="q",
        original_user_message="q",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    assert agent._db_flush_scan_prefix is None

    persisted = agent.persisted_messages
    assert persisted is not None
    assert persisted[-1]["content"] == "Here is your answer."
    assert persisted[-1]["tool_calls"]
    assert "_db_persisted" not in persisted[-1], (
        "marker must be popped so the next flush re-writes the filled content"
    )


def test_empty_terminal_sentinel_explainer_survives_sessiondb_reload(
    tmp_path, monkeypatch
):
    """Production ``(empty)`` path must reload the same explainer from SessionDB.

    Empty-response exhaustion appends ``_empty_terminal_sentinel`` and sets
    ``final_response="(empty)"``. The finalizer strips that scaffold (and any
    orphan tool tail) before the #43849 append/persist gate. FakeAgent leaves
    ``_drop_trailing_empty_response_scaffolding`` as a no-op, so only a real
    AIAgent + SessionDB round-trip proves the ``/resume`` transcript ends on
    the delivered explanation rather than a tool/sentinel row.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_TURN_COMPLETION_EXPLAINER", "1")
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-empty-terminal-explainer"
    db.create_session(session_id=session_id, source="cli")

    agent = object.__new__(AIAgent)
    agent.max_iterations = 90
    agent.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
    agent.quiet_mode = True
    agent.model = "test-model"
    agent.provider = "test-provider"
    agent.base_url = ""
    agent.session_id = session_id
    agent.platform = "cli"
    agent.context_compressor = SimpleNamespace(last_prompt_tokens=0)
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_estimated_cost_usd = 0
    agent.session_cost_status = "unknown"
    agent.session_cost_source = "test"
    agent._tool_guardrail_halt_decision = None
    agent._interrupt_message = None
    agent._response_was_previewed = False
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = []
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._pending_cli_user_message = None
    agent._persist_disabled = False
    agent._session_db = db
    agent._session_db_created = True
    agent._session_messages = []
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._session_persist_lock = None
    agent._session_json_enabled = False
    agent._stream_callback = None
    agent._turn_failed_file_mutations = {}
    agent.request_overrides = {}

    # Production methods under test — not FakeAgent no-ops.
    agent._drop_trailing_empty_response_scaffolding = (
        AIAgent._drop_trailing_empty_response_scaffolding.__get__(agent, AIAgent)
    )
    agent._persist_session = AIAgent._persist_session.__get__(agent, AIAgent)
    agent._flush_messages_to_session_db = (
        AIAgent._flush_messages_to_session_db.__get__(agent, AIAgent)
    )
    agent._flush_messages_to_session_db_unlocked = (
        AIAgent._flush_messages_to_session_db_unlocked.__get__(agent, AIAgent)
    )
    agent._save_session_log = AIAgent._save_session_log.__get__(agent, AIAgent)
    agent._apply_persist_user_message_override = (
        AIAgent._apply_persist_user_message_override.__get__(agent, AIAgent)
    )
    agent._turn_completion_explainer_enabled = (
        AIAgent._turn_completion_explainer_enabled.__get__(agent, AIAgent)
    )
    agent._format_turn_completion_explanation = (
        AIAgent._format_turn_completion_explanation
    )
    agent._file_mutation_verifier_enabled = lambda: False
    agent._handle_max_iterations = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("not expected")
    )
    agent._emit_status = lambda *_a, **_k: None
    agent._safe_print = lambda *_a, **_k: None
    agent._save_trajectory = lambda *_a, **_k: None
    agent._cleanup_task_resources = lambda *_a, **_k: None
    agent._drain_pending_steer = lambda: None
    agent.clear_interrupt = lambda: None
    agent._sync_external_memory_for_turn = lambda **_k: None

    # Shape left by conversation_loop empty-exhaustion before finalize_turn:
    # tool work + the ``(empty)`` terminal sentinel, inbound final_response
    # still the bare sentinel string.
    messages = [
        {"role": "user", "content": "run the task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {
            "role": "assistant",
            "content": "(empty)",
            "_empty_terminal_sentinel": True,
        },
    ]

    result = finalize_turn(
        agent,
        final_response="(empty)",
        api_call_count=4,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="run the task",
        original_user_message="run the task",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    delivered = result["final_response"]
    assert delivered and "No reply" in delivered
    assert delivered != "(empty)"
    assert result["messages"][-1]["role"] == "assistant"
    assert result["messages"][-1]["content"] == delivered
    assert all(not m.get("_empty_terminal_sentinel") for m in result["messages"])

    reloaded = db.get_messages_as_conversation(session_id)
    assert reloaded, "expected durable rows in SessionDB"
    assert reloaded[-1]["role"] == "assistant"
    assert reloaded[-1]["content"] == delivered
    assert all(row.get("role") != "tool" for row in reloaded)
    assert all("(empty)" not in (row.get("content") or "") for row in reloaded)
