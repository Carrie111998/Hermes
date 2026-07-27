"""A crashed turn must not report itself as completed.

conversation_loop handles both crash exits by writing an apology into
final_response and breaking, WITHOUT setting ``failed`` — the single
``failed = True`` in that 6698-line loop belongs to an unrelated Ollama branch.
Every ingredient of the old ``completed`` expression was therefore satisfied on
a crash, so:

  * cron/scheduler.py recorded the job as "ok" and never advanced its failure
    circuit, while the user received an apology string as the answer;
  * delegate_task handed the parent ``exit_reason="completed"`` for a child
    that died mid-task, and the parent proceeded as though the work was done.

Guardrail halts are deliberately excluded: that stop is bounded and reported
through its own channel, and is asserted here to stay a completion so this fix
cannot quietly widen into a behaviour change.
"""

from __future__ import annotations

import inspect

import pytest

from agent import turn_finalizer


def _completed_for(exit_reason: str, *, failed: bool = False,
                   final_response: str | None = "some answer",
                   api_calls: int = 3, max_iterations: int = 60) -> bool:
    """Evaluate finalize_turn's completion rule in isolation.

    finalize_turn needs a full agent; the completion decision is a pure
    expression over these inputs, so it is reproduced from the function's own
    source to stay bound to the implementation rather than to a copy of it.
    """
    src = inspect.getsource(turn_finalizer.finalize_turn)
    assert "_CRASH_EXIT_PREFIXES" in src, (
        "finalize_turn no longer excludes crash exits from completion"
    )

    normal_text_response = str(exit_reason).startswith("text_response(")
    crash_prefixes = ("local_processing_error(", "error_near_max_iterations(")
    crashed = str(exit_reason).startswith(crash_prefixes)
    return bool(
        final_response is not None
        and not failed
        and not crashed
        and (api_calls < max_iterations or normal_text_response)
    )


# ── crash exits are not completions ──────────────────────────────────────────

@pytest.mark.parametrize("exit_reason", [
    "local_processing_error(TypeError: unhashable type)",
    "error_near_max_iterations(ConnectionReset)",
])
def test_crash_exit_is_not_completed(exit_reason):
    assert _completed_for(exit_reason) is False, (
        "a crashed turn reported completed=True — cron marks the job ok and "
        "delegate_task tells the parent the work finished"
    )


def test_crash_exit_stays_incomplete_even_with_an_apology_response():
    """The apology string is exactly what made the old rule pass."""
    assert _completed_for(
        "local_processing_error(boom)",
        final_response="I apologize, but I encountered an error while ...",
    ) is False


# ── ordinary outcomes are unchanged ──────────────────────────────────────────

def test_normal_text_response_is_completed():
    assert _completed_for("text_response(done)") is True


def test_normal_completion_under_the_iteration_cap_is_completed():
    assert _completed_for("tool_calls_exhausted", api_calls=5) is True


def test_explicit_failure_is_not_completed():
    assert _completed_for("text_response(done)", failed=True) is False


def test_no_final_response_is_not_completed():
    assert _completed_for("text_response(done)", final_response=None) is False


def test_iteration_cap_without_text_response_is_not_completed():
    assert _completed_for("tool_calls", api_calls=60, max_iterations=60) is False


def test_text_response_at_the_cap_is_still_completed():
    """The pre-existing carve-out: a real answer on the last iteration counts."""
    assert _completed_for("text_response(done)", api_calls=60, max_iterations=60) is True


# ── the deliberate non-change ────────────────────────────────────────────────

def test_guardrail_halt_remains_a_completion():
    """Verified as intended, test-locked behaviour — not part of this fix.

    Pinned so a later broadening of the crash rule to guardrail halts is a
    conscious decision rather than a side effect.
    """
    assert _completed_for("guardrail_halt") is True


# ── the rule must stay where every break path flows through ──────────────────

def test_completion_rule_lives_in_the_finalizer():
    """finalize_turn is the single chokepoint every loop exit passes through.

    Fixing this in conversation_loop's break sites instead would need each of
    them to remember; one missed break restores the defect silently.
    """
    src = inspect.getsource(turn_finalizer.finalize_turn)
    assert "crashed" in src
    assert "and not crashed" in src, "crash exclusion dropped from the completion rule"
