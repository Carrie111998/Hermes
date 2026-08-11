"""Intent-ack continuation gate + detector behavior.

Covers the config-driven generalization of the codex intent-ack continuation
(issue #27881): the historical ``codex_responses``-only path is byte-stable
under the default ``"auto"`` mode, while an explicit ``true``/model-list opt-in
extends the "you announced an action but called no tool — keep going" nudge to
every api_mode and relaxes the codebase/workspace requirement so general
autonomous workflows ("I'll run a health check on the server") are caught.

These are invariant assertions about how the mode string and the detector
gates relate, not snapshots of the marker lists.
"""

from types import SimpleNamespace
from typing import Union

import pytest

from agent.agent_runtime_helpers import (
    classify_codex_terminal,
    intent_ack_continuation_enabled,
    intent_ack_continuation_mode,
    looks_like_codex_intermediate_ack,
)
from agent.terminal_continuation import CONTINUATION_NUDGE, ContinuationReason


def _agent(
    mode: Union[str, bool, list] = "auto",
    api_mode="chat_completions",
    model="anthropic/claude-sonnet-4",
):
    # _strip_think_blocks is a no-op for these plain-text fixtures.
    return SimpleNamespace(
        _intent_ack_continuation=mode,
        api_mode=api_mode,
        model=model,
        _strip_think_blocks=lambda c: c,
    )


# The reporter's exact repro (#27881): server-ops task, no filesystem reference.
REPRO_USER = (
    "check the current status of the server, grab the latest error logs, "
    "and let me know if there's anything critical"
)
REPRO_ACK = "I will start by running a health check command on the server to see its current status."

# The codex-coding case the detector was originally built for.
CODE_USER = "review the codebase in /app"
CODE_ACK = "Let me inspect the repository files first."


# ── mode resolution ────────────────────────────────────────────────────────




def test_true_is_all_api_modes():
    for am in ("chat_completions", "anthropic", "codex_responses"):
        assert intent_ack_continuation_mode(_agent(True, am)) == "all"
    for s in ("true", "always", "yes", "on", "ON"):
        assert intent_ack_continuation_mode(_agent(s, "chat_completions")) == "all"








def test_missing_attr_defaults_to_auto():
    bare = SimpleNamespace(api_mode="chat_completions", model="x", _strip_think_blocks=lambda c: c)
    assert intent_ack_continuation_mode(bare) == "off"
    bare_codex = SimpleNamespace(api_mode="codex_responses", model="x", _strip_think_blocks=lambda c: c)
    assert intent_ack_continuation_mode(bare_codex) == "codex_only"


def test_enabled_is_mode_not_off():
    assert intent_ack_continuation_enabled(_agent(True, "chat_completions")) is True
    assert intent_ack_continuation_enabled(_agent("auto", "codex_responses")) is True
    assert intent_ack_continuation_enabled(_agent("auto", "codex_app_server")) is True
    assert intent_ack_continuation_enabled(_agent("auto", "chat_completions")) is False
    assert intent_ack_continuation_enabled(_agent(False, "codex_responses")) is False


# ── detector: workspace requirement ─────────────────────────────────────────




def test_multipart_user_message_does_not_crash_on_workspace_path():
    """#9562: vision requests forward ``user_message`` as a multi-part list.

    The OpenAI-compat API server passes the raw ``content`` field straight
    through for vision turns, so ``user_message`` reaches the detector as
    ``[{type:"text",...}, {type:"image_url",...}]``. The ``require_workspace``
    path flattened it with ``(user_message or "").strip()`` — a truthy list
    survived and ``.strip()`` raised ``AttributeError``, killing the turn.
    The text part still has to drive workspace detection.
    """
    a = _agent("auto", "codex_responses")
    multipart = [
        {"type": "text", "text": CODE_USER},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    msgs = [{"role": "user", "content": multipart}]
    # No crash, and the text part ("review the codebase in /app") still
    # satisfies the workspace requirement so the ack fires.
    assert looks_like_codex_intermediate_ack(
        a, multipart, CODE_ACK, msgs, require_workspace=True
    )


def test_all_path_drops_workspace_requirement():
    """The #27881 fix: opted-in turns catch non-codebase intent acks."""
    a = _agent(True, "chat_completions")
    msgs = [{"role": "user", "content": REPRO_USER}]
    assert looks_like_codex_intermediate_ack(
        a, REPRO_USER, REPRO_ACK, msgs, require_workspace=False
    )


def test_historical_tool_does_not_disable_current_turn_ack():
    """Old tool history must not permanently disable the per-turn guard."""
    a = _agent("auto", "codex_responses")
    msgs = [
        {"role": "user", "content": "Check /tmp/old.txt"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "old"}]},
        {"role": "tool", "tool_call_id": "old", "content": "done"},
        {"role": "assistant", "content": "The old check is complete."},
        {"role": "user", "content": CODE_USER},
    ]

    assert looks_like_codex_intermediate_ack(
        a, CODE_USER, CODE_ACK, msgs, require_workspace=True
    )


def test_synthetic_nudge_does_not_reset_current_turn_tool_evidence():
    a = _agent("auto", "codex_responses")
    request = "Continue implementing the fix in /app until tests pass."
    checkpoint = (
        "The candidate is not promotable. Remaining work: implement the "
        "workspace fix and rerun the failing tests."
    )
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "25 failed"},
        {
            "role": "assistant",
            "content": "I'm now implementing the remaining workspace fix.",
            "_terminal_continuation_scaffold": True,
        },
        {
            "role": "user",
            "content": CONTINUATION_NUDGE,
            "_terminal_continuation_scaffold": True,
        },
    ]

    assert classify_codex_terminal(
        a,
        request,
        checkpoint,
        messages,
        continuation_attempts=1,
    ) is ContinuationReason.POST_TOOL_EXPLICIT_UNFINISHED


def test_legacy_nudge_does_not_reset_current_turn_tool_evidence():
    a = _agent("auto", "codex_responses")
    request = "Continue implementing the fix in /app until tests pass."
    checkpoint = (
        "The candidate is not promotable. Remaining work: implement the "
        "workspace fix and rerun the failing tests."
    )
    legacy_nudge = (
        "[System: Continue now. Execute the required tool calls and only send "
        "your final answer after completing the task.]"
    )
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "25 failed"},
        {"role": "assistant", "content": "I'm now implementing the remaining fix."},
        {"role": "user", "content": legacy_nudge},
    ]

    assert classify_codex_terminal(
        a,
        request,
        checkpoint,
        messages,
        continuation_attempts=1,
    ) is ContinuationReason.POST_TOOL_EXPLICIT_UNFINISHED


def test_other_synthetic_user_rows_do_not_reset_current_turn_tool_evidence():
    a = _agent("auto", "codex_responses")
    request = "Continue implementing the fix in /app until tests pass."
    checkpoint = (
        "The candidate is not promotable. Remaining work: implement the "
        "workspace fix and rerun the failing tests."
    )
    messages = [
        {"role": "user", "content": request},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "25 failed"},
        {
            "role": "user",
            "content": "Run the required verification before finishing.",
            "_verification_stop_synthetic": True,
        },
    ]

    assert classify_codex_terminal(
        a,
        request,
        checkpoint,
        messages,
        continuation_attempts=1,
    ) is ContinuationReason.POST_TOOL_EXPLICIT_UNFINISHED


def test_codex_post_tool_progressive_action_is_not_terminal():
    """A Codex model must not end after announcing its next concrete action."""
    a = _agent("auto", "codex_responses")
    msgs = [
        {"role": "user", "content": "Continue implementing the fix in /app"},
        {"role": "assistant", "content": "Running focused tests.", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "51 passed"},
    ]
    progress = (
        "The focused tests pass. I'm now porting the remaining workspace checks "
        "and rerunning the suite."
    )

    assert looks_like_codex_intermediate_ack(
        a,
        "Continue implementing the fix in /app",
        progress,
        msgs,
        require_workspace=True,
    )


def test_codex_explicit_unfinished_checkpoint_is_not_terminal():
    """A stopped checkpoint with actionable remaining work must continue."""
    a = _agent("auto", "codex_responses")
    request = "Continue implementing the local fix in /app until the tests pass."
    msgs = [
        {"role": "user", "content": request},
        {"role": "assistant", "content": "Running the suite.", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "25 failed, 97 passed"},
    ]
    checkpoint = (
        "I have stopped at that verified local checkpoint; there is no background "
        "process running. The candidate is not promotable. Remaining work: implement "
        "workspace release lifecycle and rerun the failing tests."
    )

    assert looks_like_codex_intermediate_ack(
        a, request, checkpoint, msgs, require_workspace=True
    )


def test_post_tool_checkpoint_waiting_for_approval_is_terminal():
    a = _agent("auto", "codex_responses")
    request = "Continue implementing the local fix in /app."
    msgs = [
        {"role": "user", "content": request},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "ready"},
    ]
    checkpoint = (
        "I have stopped at this checkpoint. Remaining work requires a production "
        "restart, and I need your approval before continuing."
    )

    assert not looks_like_codex_intermediate_ack(
        a, request, checkpoint, msgs, require_workspace=True
    )


def test_review_only_checkpoint_report_is_terminal():
    a = _agent("auto", "codex_responses")
    request = "Audit the candidate in /app and report its readiness."
    msgs = [
        {"role": "user", "content": request},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "25 failed"},
    ]
    report = (
        "I have stopped at the verified checkpoint. The candidate is not promotable. "
        "Remaining work belongs to the implementation phase."
    )

    assert not looks_like_codex_intermediate_ack(
        a, request, report, msgs, require_workspace=True
    )


def test_post_tool_signoff_is_terminal():
    a = _agent("auto", "codex_responses")
    request = "Review the code in /app."
    msgs = [
        {"role": "user", "content": request},
        {"role": "assistant", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "done"},
    ]

    assert not looks_like_codex_intermediate_ack(
        a,
        request,
        "The review is complete. Let me know if you'd like me to implement the fix.",
        msgs,
        require_workspace=True,
    )


def test_unfinished_checkpoint_without_current_tool_is_terminal():
    a = _agent("auto", "codex_responses")
    request = "Continue implementing the fix in /app."
    msgs = [{"role": "user", "content": request}]
    checkpoint = "I have stopped at this checkpoint. Remaining work is not complete."

    assert not looks_like_codex_intermediate_ack(
        a, request, checkpoint, msgs, require_workspace=True
    )


# Adapted from the adversarial false-positive corpus in PR #69779. These are
# terminal conversational/approval/refusal responses, not executable promises.
@pytest.mark.parametrize(
    "content",
    [
        "Let me know if you'd like me to inspect the repository files.",
        "I'll deploy it, but let me know if you'd prefer another approach.",
        "Would you like me to deploy it now?",
        "I'll admit, building rapport with a new team takes time.",
        "I'll never run that command on production.",
        "I will never delete your data without asking.",
        "I'll be brief\nrun the tests when you are ready.",
        "I'll run through my reasoning first.",
        "I'll open with a quick summary.",
        "Let me build on your earlier point.",
        "Let me read you the key line.",
        "Now I'll walk you through the tradeoffs.",
        "I'll check in with you next week.",
        "Now I am done reviewing the code and everything runs.",
        "Now I am unable to run the tests because docker is down.",
    ],
)
def test_conversational_or_waiting_prose_is_terminal(content):
    a = _agent(True, "chat_completions")
    assert not looks_like_codex_intermediate_ack(
        a,
        "Help me plan the repository work.",
        content,
        [{"role": "user", "content": "Help me plan the repository work."}],
        require_workspace=False,
    )


def test_compatibility_wrapper_honors_explicit_recovery_budget():
    agent = _agent("auto", "codex_responses", "gpt-5.6-terra")
    messages = [{"role": "user", "content": CODE_USER}]

    assert looks_like_codex_intermediate_ack(
        agent,
        CODE_USER,
        CODE_ACK,
        messages,
        continuation_attempts=0,
    )
    assert not looks_like_codex_intermediate_ack(
        agent,
        CODE_USER,
        CODE_ACK,
        messages,
        continuation_attempts=2,
    )


def test_agent_compatibility_forwarder_carries_recovery_budget():
    from run_agent import AIAgent

    agent = _agent("auto", "codex_responses", "gpt-5.6-terra")
    messages = [{"role": "user", "content": CODE_USER}]
    assert not AIAgent._looks_like_codex_intermediate_ack(
        agent,
        CODE_USER,
        CODE_ACK,
        messages,
        continuation_attempts=2,
    )


# ── detector: guardrails that hold regardless of workspace ───────────────────







