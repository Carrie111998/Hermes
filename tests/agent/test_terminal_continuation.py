"""Pure terminal-continuation policy tests.

The classifier owns *whether* a completed model turn should continue. Runtime
loops own *how* to retry and how to keep retry scaffolding ephemeral.
"""

from agent.terminal_continuation import (
    ContinuationFacts,
    ContinuationReason,
    classify_terminal_continuation,
    count_substantive_tools,
)


def _facts(**overrides):
    values = {
        "runtime": "codex_responses",
        "terminal_completed": True,
        "finish_reason": "stop",
        "substantive_tool_count": 0,
        "assistant_text": "I'll inspect the repository now.",
        "user_text": "Inspect the repository in /app and fix the issue.",
        "workspace_scoped": True,
        "continuation_attempts": 0,
        "interrupted": False,
        "transport_error": False,
        "pending_background": False,
    }
    values.update(overrides)
    return ContinuationFacts(**values)


def test_initial_intent_ack_is_reasoned():
    assert classify_terminal_continuation(_facts()) is ContinuationReason.INITIAL_INTENT_ACK


def test_post_tool_immediate_action_is_reasoned():
    facts = _facts(
        substantive_tool_count=2,
        assistant_text=(
            "The focused tests pass. I'm now porting the remaining workspace "
            "checks and rerunning the suite."
        ),
    )
    assert (
        classify_terminal_continuation(facts)
        is ContinuationReason.POST_TOOL_IMMEDIATE_ACTION
    )


def test_long_post_tool_unfinished_tail_is_reasoned():
    summary = "Completed checks. " + ("Evidence and analysis. " * 150)
    tail = (
        "Tests are still failing and this is not promotable. Remaining work: "
        "fix workspace release handling and rerun the suite."
    )
    facts = _facts(
        substantive_tool_count=3,
        assistant_text=summary + tail,
        user_text="Continue implementing the fix in /app until it is complete.",
    )
    assert (
        classify_terminal_continuation(facts)
        is ContinuationReason.POST_TOOL_EXPLICIT_UNFINISHED
    )


def test_audit_report_with_remaining_work_is_terminal():
    facts = _facts(
        substantive_tool_count=3,
        assistant_text=(
            "The candidate is not promotable. Remaining work: fix workspace "
            "release handling and rerun the suite."
        ),
        user_text="Audit the candidate in /app and report what remains.",
    )
    assert classify_terminal_continuation(facts) is ContinuationReason.NONE


def test_unfinished_text_without_substantive_tool_is_terminal():
    facts = _facts(
        substantive_tool_count=0,
        assistant_text="Remaining work: fix the release path and rerun tests.",
    )
    assert classify_terminal_continuation(facts) is ContinuationReason.NONE


def test_housekeeping_tools_do_not_count_as_substantive():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "todo-1", "function": {"name": "todo", "arguments": "{}"}},
                {"id": "mem-1", "function": {"name": "memory", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "todo-1", "content": "updated"},
        {"role": "tool", "tool_call_id": "mem-1", "content": "saved"},
    ]
    assert count_substantive_tools(messages) == 0


def test_unknown_and_execution_tools_count_as_substantive():
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "term-1",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "term-1", "content": "tests passed"},
        {"role": "tool", "tool_call_id": "unknown-1", "content": "result"},
    ]
    assert count_substantive_tools(messages) == 2


def test_historical_tools_are_excluded_by_current_turn_slice():
    history = [
        {"role": "assistant", "tool_calls": [{"id": "old", "function": {"name": "terminal"}}]},
        {"role": "tool", "tool_call_id": "old", "content": "old result"},
        {"role": "user", "content": "New task in /app"},
    ]
    current_turn = history[3:]
    assert count_substantive_tools(current_turn) == 0


def test_budget_exhaustion_has_explicit_reason():
    facts = _facts(continuation_attempts=2)
    assert classify_terminal_continuation(facts) is ContinuationReason.BUDGET_EXHAUSTED


def test_terminal_state_guards_precede_lexical_detection():
    assert (
        classify_terminal_continuation(_facts(terminal_completed=False))
        is ContinuationReason.NONE
    )
    assert (
        classify_terminal_continuation(_facts(interrupted=True))
        is ContinuationReason.NONE
    )
    assert (
        classify_terminal_continuation(_facts(transport_error=True))
        is ContinuationReason.NONE
    )
    assert (
        classify_terminal_continuation(_facts(finish_reason="length"))
        is ContinuationReason.NONE
    )
    assert (
        classify_terminal_continuation(_facts(finish_reason="content_filter"))
        is ContinuationReason.NONE
    )
    assert (
        classify_terminal_continuation(_facts(pending_background=True))
        is ContinuationReason.NONE
    )


def test_blockers_approval_credentials_questions_and_waits_are_terminal():
    endings = [
        "I need your approval before I update production.",
        "I need your deployment token before I can continue.",
        "The process is still running; waiting for completion.",
        "Waiting for the build to finish. I'm now deploying the remaining changes.",
        "The deploy is in flight; next I'll run the remaining checks.",
        "The background job hasn't returned yet. I'm now applying the next patch.",
        "Applied the change. Next, I'll wait for your review before deploying.",
        "I am blocked by an unavailable external dependency.",
        "Would you like me to update the README?",
        "Let me know if you want me to run more tests.",
    ]
    for text in endings:
        assert (
            classify_terminal_continuation(
                _facts(substantive_tool_count=1, assistant_text=text)
            )
            is ContinuationReason.NONE
        )


def test_early_safety_boundary_is_not_hidden_by_long_actionable_tail():
    assistant_text = (
        "I need your approval and deployment token before touching production. "
        + ("Evidence and analysis. " * 160)
        + "Next, I'll run the migration and finish the remaining work."
    )
    facts = _facts(
        substantive_tool_count=3,
        assistant_text=assistant_text,
        user_text="Continue implementing the migration in /app until complete.",
    )
    assert classify_terminal_continuation(facts) is ContinuationReason.NONE


def test_genuine_completion_and_optional_next_steps_are_terminal():
    endings = [
        "Done; all tests pass and the implementation is complete.",
        "The requested audit is complete. Next steps could include refactoring.",
        "I will not delete production data.",
    ]
    for text in endings:
        assert (
            classify_terminal_continuation(
                _facts(substantive_tool_count=1, assistant_text=text)
            )
            is ContinuationReason.NONE
        )


def test_non_codex_or_non_workspace_auto_scope_is_terminal():
    assert (
        classify_terminal_continuation(_facts(runtime="chat_completions"))
        is ContinuationReason.NONE
    )
    assert (
        classify_terminal_continuation(_facts(workspace_scoped=False))
        is ContinuationReason.NONE
    )


def test_app_server_uses_same_policy_only_after_native_completion():
    facts = _facts(runtime="codex_app_server")
    assert classify_terminal_continuation(facts) is ContinuationReason.INITIAL_INTENT_ACK
    assert (
        classify_terminal_continuation(
            _facts(runtime="codex_app_server", terminal_completed=False)
        )
        is ContinuationReason.NONE
    )
