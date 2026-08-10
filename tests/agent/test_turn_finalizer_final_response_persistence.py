from types import SimpleNamespace
from typing import Any

import pytest

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
        return False

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


def test_nonterminal_retry_exit_restores_latest_continuation_checkpoint(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._response_was_previewed = False
    checkpoint = "I'm now fixing the remaining tests."
    messages = [
        {"role": "user", "content": "finish the work"},
        {
            "role": "assistant",
            "content": checkpoint,
            "_terminal_continuation_scaffold": True,
        },
        {
            "role": "user",
            "content": "continue",
            "_terminal_continuation_scaffold": True,
        },
    ]

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=2,
        interrupted=False,
        failed=True,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="finish the work",
        original_user_message="finish the work",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert result["completed"] is False
    assert result["response_previewed"] is False
    assert result["final_response"] == checkpoint
    assert result["messages"][-1] == {"role": "assistant", "content": checkpoint}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {"role": "assistant", "content": checkpoint}
    assert not any(
        message.get("_terminal_continuation_scaffold")
        for message in result["messages"]
        if isinstance(message, dict)
    )


def test_restored_checkpoint_preserves_proven_interim_delivery(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._response_was_previewed = False
    checkpoint = "I'm now fixing the remaining tests."
    messages = [
        {"role": "user", "content": "finish the work"},
        {
            "role": "assistant",
            "content": checkpoint,
            "_terminal_continuation_scaffold": True,
            "_terminal_continuation_previewed": True,
        },
        {
            "role": "user",
            "content": "continue",
            "_terminal_continuation_scaffold": True,
        },
    ]

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=2,
        interrupted=False,
        failed=True,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="finish the work",
        original_user_message="finish the work",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert result["final_response"] == checkpoint
    assert result["response_previewed"] is True
    assert result["messages"][-1] == {"role": "assistant", "content": checkpoint}


@pytest.mark.parametrize(
    "sentinel",
    [
        "(empty)",
        (
            "⚠️ The model produced only internal reasoning and no final answer, "
            "despite retries. Its last reasoning, which may contain the answer:\n\n"
            "I should continue."
        ),
    ],
)
def test_no_answer_sentinel_restores_latest_continuation_checkpoint(
    monkeypatch, sentinel
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._response_was_previewed = False
    checkpoint = "I'm now fixing the remaining tests."
    messages = [
        {"role": "user", "content": "finish the work"},
        {
            "role": "assistant",
            "content": checkpoint,
            "_terminal_continuation_scaffold": True,
        },
        {
            "role": "user",
            "content": "continue",
            "_terminal_continuation_scaffold": True,
        },
    ]

    result = finalize_turn(
        agent,
        final_response=sentinel,
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="finish the work",
        original_user_message="finish the work",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert result["final_response"] == checkpoint
    assert result["response_previewed"] is False
    assert result["messages"][-1] == {"role": "assistant", "content": checkpoint}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {
        "role": "assistant",
        "content": checkpoint,
    }


def test_restored_checkpoint_follows_trailing_terminal_sentinel_cleanup(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._response_was_previewed = False

    def drop_trailing_scaffolding(messages):
        while messages and messages[-1].get("_empty_terminal_sentinel"):
            messages.pop()

    agent._drop_trailing_empty_response_scaffolding = drop_trailing_scaffolding
    checkpoint = "I'm now fixing the remaining tests."
    messages = [
        {"role": "user", "content": "finish the work"},
        {
            "role": "assistant",
            "content": checkpoint,
            "_terminal_continuation_scaffold": True,
        },
        {
            "role": "user",
            "content": "continue",
            "_terminal_continuation_scaffold": True,
        },
        {
            "role": "assistant",
            "content": "(empty)",
            "_empty_terminal_sentinel": True,
        },
    ]

    result = finalize_turn(
        agent,
        final_response="(empty)",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="finish the work",
        original_user_message="finish the work",
        _should_review_memory=False,
        _turn_exit_reason="empty_response_exhausted",
    )

    assert result["final_response"] == checkpoint
    assert result["messages"] == [
        {"role": "user", "content": "finish the work"},
        {"role": "assistant", "content": checkpoint},
    ]
    assert agent.persisted_messages == result["messages"]
    assert not any(
        message.get("_empty_terminal_sentinel")
        for message in result["messages"]
    )


def test_interrupted_restored_checkpoint_closes_user_tail_after_cleanup(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._response_was_previewed = False
    checkpoint = "I'm now fixing the remaining tests."
    messages = [
        {"role": "user", "content": "finish the work"},
        {
            "role": "assistant",
            "content": checkpoint,
            "_terminal_continuation_scaffold": True,
        },
        {
            "role": "user",
            "content": "continue",
            "_terminal_continuation_scaffold": True,
        },
    ]

    result = finalize_turn(
        agent,
        final_response=None,
        api_call_count=2,
        interrupted=True,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="finish the work",
        original_user_message="finish the work",
        _should_review_memory=False,
        _turn_exit_reason="interrupted",
    )

    assert result["final_response"] == checkpoint
    assert result["messages"] == [
        {"role": "user", "content": "finish the work"},
        {"role": "assistant", "content": checkpoint},
    ]
    assert agent.persisted_messages == result["messages"]


@pytest.mark.parametrize(
    ("failed", "interrupted"),
    [(True, False), (False, True)],
)
def test_newer_recovery_text_wins_over_stale_checkpoint(
    monkeypatch, failed, interrupted
):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._response_was_previewed = False
    checkpoint = "I'm now fixing the remaining tests."
    newer = "The recovery completed a newer partial result."
    messages = [
        {"role": "user", "content": "finish the work"},
        {
            "role": "assistant",
            "content": checkpoint,
            "_terminal_continuation_scaffold": True,
            "_terminal_continuation_previewed": True,
        },
        {
            "role": "user",
            "content": "continue",
            "_terminal_continuation_scaffold": True,
        },
        {"role": "assistant", "content": newer},
    ]

    result = finalize_turn(
        agent,
        final_response=newer,
        api_call_count=2,
        interrupted=interrupted,
        failed=failed,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="finish the work",
        original_user_message="finish the work",
        _should_review_memory=False,
        _turn_exit_reason="interrupted" if interrupted else "recovery_failed",
    )

    assert result["final_response"] == newer
    assert result["response_previewed"] is False
    assert result["messages"][-1] == {"role": "assistant", "content": newer}
    assert not any(
        message.get("_terminal_continuation_scaffold")
        for message in result["messages"]
        if isinstance(message, dict)
    )
    assert all(message.get("content") != checkpoint for message in result["messages"])


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
