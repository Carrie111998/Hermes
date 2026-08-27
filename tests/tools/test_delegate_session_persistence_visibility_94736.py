#!/usr/bin/env python3
"""
Regression tests for issue #94736 — "Subagent/cron sessions silently die:
'Session DB append_message failed: ...' goes to a sentry-style backstop
nowhere visible."

The pre-fix behaviour: when a child agent's run_conversation returns
``failed=True``, ``turn_exit_reason='session_persistence_failed'``, and an
empty ``final_response``, ``_run_single_child`` discards the structured
``failure_reason`` and ``turn_exit_reason`` fields. The entry dict it
returns to ``delegate_task`` carries only:

    {"status": "failed", "error": "Subagent did not produce a response.", ...}

— no machine-readable cause, no human-readable explanation of WHAT went
wrong. The parent (a cron job, an orchestrator subagent, or the TUI)
therefore sees "the subagent returned nothing" with no hint that the
session DB write failed, and the delivered Slack summary reports
``last_status: ok`` while every artifact was orphaned.

These tests pin the post-fix contract:

1. ``failure_reason`` (``'session_persistence_failed:<cause>'``) MUST be
   surfaced in the entry dict so callers can branch on the cause
   programmatically.
2. ``turn_exit_reason`` MUST be preserved on the entry dict so audit
   logs and structured error sinks can show the exact exit reason.
3. ``entry['error']`` MUST contain a human-readable explanation of the
   persistence failure (not just "Subagent did not produce a response.")
   — so cron delivery summaries, Slack/email alerts, and parent-model
   context all show the user what actually happened.
4. ``status`` stays ``"failed"`` (we don't change existing semantics).
5. The same visibility contract applies whether the cause is locked /
   disk / turn_lease / corrupt / unknown — all five causes must surface
   the cause tag.

The tests deliberately drive ``_run_single_child`` with mocked children
that return the same dict shape ``run_agent.run_conversation`` would
produce on a persistence failure (see ``agent/turn_finalizer.py``,
``turn_finalizer`` lines 740-755 and ``run_agent._flush_messages_to_session_db_unlocked``
which sets ``_last_persistence_error_cause`` via ``classify_persistence_error``).
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from tools.delegate_tool import _run_single_child


def _make_mock_parent():
    """Lightweight parent double matching _run_single_child's expectations."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "***"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._touch_activity = lambda *_a, **_kw: None
    return parent


def _make_child_with_persistence_failure(cause: str):
    """Build a mock child whose run_conversation returns the
    session_persistence_failed contract shape produced by
    run_agent.run_conversation (see agent/turn_finalizer.py lines 740-755)."""
    child = MagicMock()
    child.model = "claude-sonnet-5"
    child.session_prompt_tokens = 1234
    child.session_completion_tokens = 0
    child._credential_pool = None
    child.run_conversation.return_value = {
        # Empty because the turn was force-ended to protect unpersisted state.
        "final_response": "",
        "completed": False,
        "failed": True,
        "interrupted": False,
        "api_calls": 49,
        "turn_exit_reason": "session_persistence_failed",
        "failure_reason": f"session_persistence_failed:{cause}",
        "error": (
            "session storage could not be written — check the state database "
            "health (`hermes doctor`), then send your message again"
        ),
        "messages": [],
    }
    return child


# ── Regression 1: failure_reason must reach the parent-visible entry ─────


@pytest.mark.parametrize(
    "cause",
    ["locked", "compression", "turn_lease", "corrupt", "disk", "unknown"],
)
def test_run_single_child_surfaces_failure_reason_on_persistence_failure(cause):
    """Issue #94736: silent subagent death drops the structured cause.

    Pre-fix: the entry dict returned by _run_single_child contained no
    ``failure_reason`` field, so the cron deliverer (and any parent
    agent / Slack channel / desktop toast) saw only an empty summary
    and reported ``last_status: ok`` for runs whose every artifact
    had been orphaned by a session DB write failure.
    """
    child = _make_child_with_persistence_failure(cause)
    parent = _make_mock_parent()

    entry = _run_single_child(
        task_index=0,
        goal="Investigate a subagent persistence failure",
        child=child,
        parent_agent=parent,
    )

    assert entry["status"] == "failed", (
        "session_persistence_failed must produce status='failed', "
        f"got status={entry['status']!r}"
    )
    assert entry.get("failure_reason") == (
        f"session_persistence_failed:{cause}"
    ), (
        "failure_reason must reach the entry so the parent can branch on "
        "the cause. Pre-fix this was dropped and the cron deliverer saw "
        "an empty 'last_status: ok' summary."
    )
    assert entry.get("turn_exit_reason") == "session_persistence_failed", (
        "turn_exit_reason must be preserved on the entry for audit logs."
    )


# ── Regression 2: error string must explain the persistence failure ─────


def test_run_single_child_error_message_explains_persistence_failure():
    """Issue #94736: empty 'Subagent did not produce a response' string
    hides the real cause from the cron deliverer.

    Pre-fix: entry['error'] was 'Subagent did not produce a response.' —
    identical wording for empty-LLM-response, transient provider errors,
    AND session DB write failures. Post-fix the persistence case must
    surface a cause-specific message so the delivered Slack summary can
    tell the operator what to investigate.
    """
    child = _make_child_with_persistence_failure("locked")
    parent = _make_mock_parent()

    entry = _run_single_child(
        task_index=0,
        goal="trigger a session_persistence_failed:locked",
        child=child,
        parent_agent=parent,
    )

    err = entry.get("error") or ""
    assert isinstance(err, str) and err.strip(), (
        "error message must be a non-empty string"
    )
    assert "Subagent did not produce a response" not in err, (
        "Generic 'Subagent did not produce a response.' is exactly the "
        "silent-death wording the issue complains about."
    )
    # The cause tag must appear in the message so it survives any
    # downstream templating that only shows the error field.
    assert "session_persistence_failed" in err or "session storage" in err.lower(), (
        "Error string must mention the session storage / persistence failure "
        f"so the operator can act. Got: {err!r}"
    )


# ── Regression 3: delegate_task's JSON output must carry the cause ─────


def test_delegate_task_json_entry_carries_failure_reason():
    """Issue #94736 end-to-end: the cause must reach the parent's tool
    result, not just the in-memory entry.

    ``delegate_task`` returns a JSON ``results`` array; that JSON is what
    the parent agent sees as the tool result and what cron delivers as
    the job's outcome. If the cause is stripped here, the silent-death
    is complete — even with a hook fix upstream, the operator gets no
    signal.
    """
    from tools.delegate_tool import delegate_task

    parent = _make_mock_parent()
    with patch("run_agent.AIAgent") as MockAgent:
        MockAgent.return_value = _make_child_with_persistence_failure("disk")
        raw = delegate_task(
            goal="do something that dies on a persistence failure",
            parent_agent=parent,
        )

    payload = json.loads(raw)
    entry = payload["results"][0]
    assert entry["status"] == "failed"
    assert entry.get("failure_reason") == "session_persistence_failed:disk", (
        "failure_reason MUST survive into the JSON returned to the parent. "
        "If it doesn't, cron reports last_status=ok with no mention of the "
        "underlying session DB write failure."
    )
    assert entry.get("turn_exit_reason") == "session_persistence_failed"
    err = entry.get("error") or ""
    assert "disk" in err.lower() or "session" in err.lower(), (
        f"Error string must mention the persistence cause. Got: {err!r}"
    )


# ── Regression 4: pre-existing observability fields stay intact ────────


def test_run_single_child_preserves_observability_metadata_on_persistence_failure():
    """Issue #94736 fix must NOT regress existing observability.

    The entry dict must still carry model/tokens/duration/tool_trace so
    audit logs and dashboards keep working — the only changes are the
    *added* failure_reason / turn_exit_reason / richer error.
    """
    child = _make_child_with_persistence_failure("turn_lease")
    parent = _make_mock_parent()

    entry = _run_single_child(
        task_index=0,
        goal="observability smoke test",
        child=child,
        parent_agent=parent,
    )

    # Existing fields from issue #81267 and earlier must still be present.
    assert entry["model"] == "claude-sonnet-5"
    assert entry["tokens"]["input"] == 1234
    assert entry["duration_seconds"] >= 0
    assert "tool_trace" in entry
    # task_index is required for batch dispatch accounting.
    assert entry["task_index"] == 0
