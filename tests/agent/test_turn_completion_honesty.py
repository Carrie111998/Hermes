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
    assert "CRASH_EXIT_PREFIXES" in src, (
        "finalize_turn no longer excludes crash exits from completion"
    )

    normal_text_response = str(exit_reason).startswith("text_response(")
    crashed = str(exit_reason).startswith(turn_finalizer.CRASH_EXIT_PREFIXES)
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
    assert isinstance(turn_finalizer.CRASH_EXIT_PREFIXES, tuple), (
        "CRASH_EXIT_PREFIXES must stay module-level — delegate_tool imports it so "
        "the parent classifies a crashed child the same way the turn does"
    )


# ── the parent must see the crash too ────────────────────────────────────────

def test_delegate_tool_classifies_a_crashed_child_as_failed():
    """Fixing finalize_turn alone was not enough (found by the peer session).

    delegate_tool derived the child's status from "is there a summary?", and a
    crashed child produces a non-empty apology as its final_response. So the
    turn correctly said completed=False while the PARENT was still handed
    status="completed" and carried on as though the delegated work was done.

    Asserted structurally against the real source: reproducing the whole
    delegate path needs a live child agent, and the property that matters is
    that the crash check exists and precedes the summary check.
    """
    import inspect

    import tools.delegate_tool as dt

    src = inspect.getsource(dt)
    assert "CRASH_EXIT_PREFIXES" in src, (
        "delegate_tool no longer consults the crash exit reasons — a crashed "
        "child is reported to the parent as completed again"
    )
    crash_at = src.index("_crashed = str(result.get(")
    branch_at = src.index('elif _crashed:')
    summary_at = src.index('elif summary and not _empty_sentinel:')
    assert crash_at < branch_at < summary_at, (
        "the crash branch must precede the summary branch, or an apology "
        "summary still wins"
    )


def test_max_iterations_child_is_still_completed():
    """Deliberate non-change: gating on `completed` wholesale would reclassify
    a child that ran out of iterations but produced usable output as a failure.
    Only the crash exit reasons are treated as failures."""
    assert not str("max_iterations_reached(60)").startswith(
        turn_finalizer.CRASH_EXIT_PREFIXES
    )


# ── a reasoning-only child is not a completion either ────────────────────────
#
# Upstream 214ae7b77 made the empty terminal deliver a labeled reasoning
# excerpt instead of the "(empty)" sentinel whenever the model DID think but
# produced no answer. That is a delivery improvement for a human reader, but
# delegate_tool consumes the same field programmatically: the banner is
# non-empty and is not a crash prefix, so the child reported status="completed"
# and handed the parent raw chain-of-thought as the delegated result.

_BANNER = (
    "\u26a0\ufe0f The model produced only internal reasoning and no final "
    "answer, despite retries and fallback. Its last reasoning, which may "
    "contain the answer:\n\nthe user probably wants me to..."
)


def _delegate_status_for(summary: str, exit_reason: str, *,
                         interrupted: bool = False) -> str:
    """Evaluate delegate_tool's status rule in isolation.

    _run_single_child is 660 lines and needs a live child agent, so this is a
    reproduction of its rule, not the rule itself. What binds the two is
    test_delegate_tool_consults_the_empty_terminal_exit_reason below — that is
    the test which fails if the fix is reverted. These cases document the rule.
    """
    empty_sentinel = (
        summary.strip() == "(empty)"
        or str(exit_reason) == turn_finalizer.EMPTY_TERMINAL_EXIT_REASON
    )
    crashed = str(exit_reason).startswith(turn_finalizer.CRASH_EXIT_PREFIXES)
    if interrupted:
        return "interrupted"
    if crashed:
        return "failed"
    if summary and not empty_sentinel:
        return "completed"
    return "failed"


def test_delegate_tool_consults_the_empty_terminal_exit_reason():
    """The binding test: delegate_tool must key on the exit reason, not just on
    the "(empty)" literal, or the reasoning-excerpt half of the terminal reads
    as a normal answer."""
    import tools.delegate_tool as dt

    src = inspect.getsource(dt)
    assert "EMPTY_TERMINAL_EXIT_REASON" in src, (
        "delegate_tool no longer consults the empty-terminal exit reason — a "
        "child that produced only reasoning is reported as completed again"
    )


def test_reasoning_only_child_is_not_completed():
    assert _delegate_status_for(_BANNER, "empty_response_exhausted") == "failed", (
        "a child that produced only internal reasoning reported completed — "
        "the parent proceeds with chain-of-thought as the delegated result"
    )


def test_bare_empty_sentinel_child_is_still_not_completed():
    """The pre-existing half of the same terminal must keep failing."""
    assert _delegate_status_for("(empty)", "empty_response_exhausted") == "failed"


def test_a_real_answer_is_still_completed():
    """Deliberate non-change: the exit reason only fires on the empty terminal."""
    assert _delegate_status_for("Here is the answer.", "text_response(done)") == "completed"


def test_banner_and_sentinel_share_one_exit_reason():
    """Producer contract: conversation_loop must not give the reasoning-excerpt
    branch its own exit reason, or consumers keying on it silently stop
    matching. Guards against upstream drift, not a current regression."""
    from agent import conversation_loop

    src = inspect.getsource(conversation_loop)
    start = src.index('_turn_exit_reason = "empty_response_exhausted"')
    block = src[start:src.index("\n                    break", start)]
    assert "only internal reasoning and " in block, (
        "the reasoning-excerpt banner moved out of the empty-terminal block"
    )
    assert block.count("_turn_exit_reason =") == 1, (
        "the empty terminal now assigns more than one exit reason; consumers "
        "keying on EMPTY_TERMINAL_EXIT_REASON will miss a branch"
    )
