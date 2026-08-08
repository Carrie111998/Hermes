"""Response-normalization helpers for the gateway. (#54962, slice 16)

Pure dict/str helpers: normalize empty agent responses into user-facing
messages, detect retry-exhausted hidden-reasoning turns, decide when a
gateway turn really completed (resume-pending clearing), and carry the
outer history offset through queued follow-up drains. Extracted verbatim
from gateway/run.py; run.py re-imports these names so
gateway.run.<name> references stay green.
"""

from __future__ import annotations


def _normalize_empty_agent_response(
    agent_result: dict,
    response: str,
    *,
    history_len: int = 0,
) -> str:
    """Normalize empty/None agent responses into user-facing messages.

    Consolidates the existing ``failed`` handler and adds a catch-all for
    the case where the agent did work (api_calls > 0) but returned no text.
    Fix for #18765.

    Also surfaces a retry hint when the agent never ran at all
    (api_calls == 0) for a non-interrupted, non-failed turn -- this is the
    silent-drop pattern observed after ``/stop`` where the next user
    message hits a stale generation token and returns an empty result,
    leaving the platform with nothing to send. (#31884)
    """
    if response:
        return response

    if agent_result.get("failed"):
        error_detail = agent_result.get("error", "unknown error")
        error_str = str(error_detail).lower()
        is_context_failure = any(
            p in error_str
            for p in ("context", "token", "too large", "too long", "exceed", "payload")
        ) or ("400" in error_str and history_len > 50)
        if is_context_failure:
            return (
                "⚠️ Session too large for the model's context window.\n"
                "Use /compact to compress the conversation, or "
                "/reset to start fresh."
            )
        return (
            f"The request failed: {str(error_detail)[:300]}\n"
            "Try again or use /reset to start a fresh session."
        )

    api_calls = int(agent_result.get("api_calls", 0) or 0)
    if agent_result.get("interrupted"):
        # An interrupted run that did work (api_calls > 0) is the drain of a
        # run the user deliberately stopped or steered — its silence is
        # intentional, and any queued/interrupting message is delivered by
        # the recursive drain inside _run_agent before this result is seen.
        # An interrupted run with ZERO api_calls never processed the user's
        # message at all: it was killed at the top of the tool loop by an
        # interrupt flag left over from a recent /stop (#44212).  Pure
        # silence there swallows a real user message, so surface it.
        if api_calls == 0:
            return (
                "⚠️ Your message was interrupted before processing started "
                "(likely by a recent /stop). Please send it again."
            )
        return response
    if api_calls > 0:
        if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
            return ""
        if agent_result.get("partial"):
            err = agent_result.get("error", "processing incomplete")
            return f"⚠️ Processing stopped: {str(err)[:200]}. Try again."
        return (
            "⚠️ Processing completed but no response was generated. "
            "This may be a transient error — try sending your message again."
        )

    # api_calls == 0, not failed, not interrupted: the agent never ran for
    # this turn. This is the post-/stop generation-race pattern where the
    # gateway would otherwise silently drop the turn (response=0 chars) and
    # the user sees no reply at all. Surface a short retry hint so the
    # message isn't lost in silence. (#31884)
    if (
        api_calls == 0
        and not agent_result.get("interrupted")
        and not agent_result.get("failed")
        and not agent_result.get("partial")
    ):
        return (
            "⚠️ Your message wasn't processed (the previous turn was still "
            "being cleaned up). Please send it again."
        )

    return response


def _is_gateway_hidden_reasoning_incomplete_turn(agent_result: dict) -> bool:
    """Detect retry-exhausted turns with hidden reasoning but no visible answer.

    The conversation loop returns the retry-exhaustion sentinel as BOTH
    ``final_response`` and ``error`` ("Codex response remained incomplete
    after 3 continuation attempts"), so ``final_response`` being non-empty
    does not mean the model produced a visible answer. Treat the turn as
    hidden when the error sentinel is present and ``final_response`` is
    either empty or merely echoes that sentinel — any genuinely different
    final text means the model DID answer and must be delivered.
    """
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("failed") or agent_result.get("interrupted"):
        return False
    if not agent_result.get("partial"):
        return False
    error_text = str(agent_result.get("error", "") or "").strip()
    if "remained incomplete after" not in error_text.lower():
        return False
    final_response = str(agent_result.get("final_response") or "").strip()
    return not final_response or final_response == error_text


def _should_clear_resume_pending_after_turn(agent_result: dict) -> bool:
    """Return True only when a gateway turn really completed successfully.

    Restart recovery uses ``resume_pending`` as a durable marker for sessions
    interrupted during gateway drain.  A soft interrupt can still bubble out as
    a syntactically normal agent result with an empty final response; clearing
    the marker in that case loses the recovery signal and startup auto-resume
    has nothing to schedule.
    """
    if not isinstance(agent_result, dict):
        return False
    if agent_result.get("interrupted"):
        return False
    if agent_result.get("failed") or agent_result.get("partial") or agent_result.get("error"):
        return False
    if agent_result.get("completed") is False:
        return False
    return True


def _preserve_queued_followup_history_offset(
    current_result: dict,
    followup_result: dict,
) -> dict:
    """Carry the outer history offset through queued follow-up drains.

    ``_process_message_background()`` persists transcript rows only once, after the
    entire in-band queued-follow-up chain returns.  Each recursive ``_run_agent()``
    call advances ``history_offset`` to the history it received, so without
    correction the outermost persistence step sees only the *last* queued turn as
    "new" and silently drops earlier turns from the same drain chain.

    Preserve the earliest (outermost) history offset so the final transcript slice
    still includes every queued turn that ran during the chain.
    """
    if not isinstance(followup_result, dict):
        return followup_result
    if not isinstance(current_result, dict):
        return followup_result

    current_offset = current_result.get("history_offset")
    followup_offset = followup_result.get("history_offset")
    if not isinstance(current_offset, int):
        return followup_result
    if isinstance(followup_offset, int) and followup_offset <= current_offset:
        return followup_result

    merged = dict(followup_result)
    merged["history_offset"] = current_offset
    return merged
