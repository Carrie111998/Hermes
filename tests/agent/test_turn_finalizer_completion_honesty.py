"""H-01 — a crashed turn must not report ``completed=True``.

``finalize_turn`` derived completion as "there is response text AND not failed
AND under the iteration cap".  The outer-loop exception handler
(agent/conversation_loop.py:6659-6672) satisfies all three: it writes an
apology string into ``final_response`` and ``break``s **without** setting
``failed``.  ``failed = True`` is set at exactly one place in that 6,700-line
loop (the Ollama runtime-context branch), so a genuine crash was indistinguishable
from a successful answer.

Consequences that made this worth fixing rather than documenting:
  * cron/scheduler.py only pattern-matches ``max_iterations_reached(``, so a
    crashed job's ``last_status`` became "ok" and the failure circuit never
    advanced — while the apology text was delivered as the agent's reply;
  * tools/delegate_tool.py handed the parent ``status="completed"``, so an
    orchestrator proceeded as though delegated work was done.

Scope is deliberately narrow.  ``guardrail_halt`` also reaches the finalizer
without ``failed``, and the ledger's proposed fix bundled it in — but that path
is test-locked, intentional design (a hard stop after N identical tool failures
*is* a completed turn that stopped early, and the operator enables it on
purpose).  It is asserted here as-is so a later "fix" cannot quietly change it.
"""

import pytest

from agent.turn_finalizer import finalize_turn


class _StubBudget:
    used = 1
    max_total = 10
    remaining = 9


class _StubCompressor:
    last_prompt_tokens = 0


class _StubAgent:
    """Minimal agent surface ``finalize_turn`` reads from."""

    def __init__(self):
        self.max_iterations = 10
        self.iteration_budget = _StubBudget()
        self.context_compressor = _StubCompressor()
        self.model = "stub/model"
        self.provider = "stub"
        self.base_url = "http://stub"
        self.session_id = "sess-1"
        self.quiet_mode = True
        self.platform = "cli"
        self._interrupt_requested = False
        self._interrupt_message = None
        self._tool_guardrail_halt_decision = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        for attr in (
            "session_input_tokens", "session_output_tokens",
            "session_cache_read_tokens", "session_cache_write_tokens",
            "session_reasoning_tokens", "session_prompt_tokens",
            "session_completion_tokens", "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "ok"
        self.session_cost_source = "stub"

    def _save_trajectory(self, *a, **k): pass
    def _cleanup_task_resources(self, *a, **k): pass
    def _drop_trailing_empty_response_scaffolding(self, *a, **k): pass
    def _persist_session(self, *a, **k): pass
    def _emit_status(self, *a, **k): pass
    def _safe_print(self, *a, **k): pass
    def _handle_max_iterations(self, messages, n): return "PARTIAL SUMMARY"
    def _file_mutation_verifier_enabled(self): return False
    def _turn_completion_explainer_enabled(self): return False
    def _drain_pending_steer(self): return None
    def clear_interrupt(self): pass
    def _sync_external_memory_for_turn(self, **k): pass


def _run(*, final_response="an answer", api_call_count=3, failed=False,
         interrupted=False, turn_exit_reason="unknown"):
    messages = [
        {"role": "user", "content": "do a thing"},
        {"role": "assistant", "content": final_response or ""},
    ]
    return finalize_turn(
        _StubAgent(),
        final_response=final_response,
        api_call_count=api_call_count,
        interrupted=interrupted,
        failed=failed,
        messages=messages,
        conversation_history=None,
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="do a thing",
        original_user_message="do a thing",
        _should_review_memory=False,
        _turn_exit_reason=turn_exit_reason,
    )


# ── the defect ───────────────────────────────────────────────────────────────

def test_local_processing_crash_is_not_completed():
    """The exact production repro: a TypeError while post-processing a
    response becomes an apology string, and used to report success."""
    result = _run(
        final_response="I apologize, but I encountered an error while "
                       "processing the model response: TypeError(...)",
        turn_exit_reason="local_processing_error(TypeError: unhashable type)",
    )
    assert result["completed"] is False


def test_repeated_error_near_max_iterations_is_not_completed():
    result = _run(
        final_response="I apologize, but I encountered repeated errors: 502",
        turn_exit_reason="error_near_max_iterations(HTTP 502 from provider)",
    )
    assert result["completed"] is False


# ── behaviour that must NOT change ───────────────────────────────────────────

def test_normal_text_response_is_completed():
    result = _run(turn_exit_reason="text_response(finish_reason=stop)")
    assert result["completed"] is True


def test_guardrail_halt_still_completes_by_design():
    """Intentionally test-locked: a hard stop after N identical tool failures
    is a turn that ended early on purpose, not a crash. The ledger proposed
    bundling it with the crash fix; the verifier refuted that. Locked so the
    distinction survives."""
    result = _run(
        final_response="I stopped retrying read_file after 5 failures.",
        turn_exit_reason="guardrail_halt",
    )
    assert result["completed"] is True


def test_explicit_failure_is_not_completed():
    result = _run(failed=True, turn_exit_reason="ollama_runtime_context_too_small")
    assert result["completed"] is False


def test_no_response_is_not_completed():
    result = _run(final_response=None,
                  turn_exit_reason="all_retries_exhausted_no_response")
    assert result["completed"] is False


def test_iteration_cap_without_text_response_is_not_completed():
    """Hitting the cap is not success unless the model actually answered."""
    result = _run(api_call_count=10, turn_exit_reason="unknown")
    assert result["completed"] is False


def test_partial_tool_execution_with_a_real_answer_still_completes():
    """A turn that ran tools and then answered normally is a success; the fix
    must key on the crash exit reasons, not on 'tools were involved'."""
    result = _run(final_response="Read 3 files; here is the summary.",
                  turn_exit_reason="text_response(finish_reason=stop)")
    assert result["completed"] is True


@pytest.mark.parametrize("reason", [
    "interrupted_by_user",
    "interrupted_during_api_call",
    "budget_exhausted",
    "empty_response_exhausted",
])
def test_other_abnormal_exits_are_not_reported_as_normal_completion(reason):
    """These already avoided completed=True via no-text / failed / cap; assert
    it, so a refactor of any one of them surfaces here rather than in cron."""
    result = _run(final_response=None, turn_exit_reason=reason)
    assert result["completed"] is False


# ── the shared crash predicate (finalizer + delegate_tool must agree) ────────

@pytest.mark.parametrize("reason,expected", [
    ("local_processing_error(TypeError: x)", True),
    ("error_near_max_iterations(HTTP 502)", True),
    ("text_response(finish_reason=stop)", False),
    ("guardrail_halt", False),          # intentional early exit, not a crash
    ("max_iterations_reached(60)", False),
    ("interrupted_by_user", False),
    ("budget_exhausted", False),
    ("unknown", False),
    ("", False),
    (None, False),
])
def test_turn_crashed_predicate(reason, expected):
    from agent.turn_finalizer import turn_crashed
    assert turn_crashed(reason) is expected


def test_delegate_tool_uses_the_shared_predicate():
    """delegate_tool must not re-implement 'crashed'.

    A crashed child still returns a non-empty final_response (the apology), so
    delegate_tool's summary-presence check reported status='completed' and the
    parent orchestrator proceeded on work that never happened. It now consults
    the same predicate as the finalizer; a private copy of the prefix tuple
    would drift the moment a new crash exit reason is added.
    """
    import pathlib

    src = pathlib.Path("tools/delegate_tool.py").read_text(encoding="utf-8")
    assert "from agent.turn_finalizer import turn_crashed" in src
    assert "child_crashed" in src
    # and it must be consulted BEFORE the summary-presence branch
    assert src.index("elif child_crashed:") < src.index("elif summary and not _empty_sentinel:")
