"""Regression test for #10710 — stale context summary leak after auto-reset.

The gateway agent cache is keyed on the stable chat ``session_key``, which does
NOT change when a session is auto-reset (daily schedule / idle timeout /
suspended). So unless the cached agent is explicitly evicted on auto-reset, the
NEXT message reuses the old ``AIAgent`` instance — carrying its
``context_compressor._previous_summary`` — and prior-conversation content leaks
into the new session's compaction summaries.

Manual ``/reset`` and the compression-exhausted path (#9893) already evict the
cached agent. This pins the matching eviction onto the auto-reset cleanup block
in ``_handle_message_with_agent``.

These are AST invariants — load-bearing pins that fail if the eviction is
removed from the cleanup block (mirrors
test_48031_model_switch_after_auto_reset.py's approach).
"""
from __future__ import annotations

import ast
import inspect

from gateway import run as gateway_run


def _calls(node: ast.AST) -> set[str]:
    """Method-call attribute names invoked anywhere under ``node``."""
    return {
        n.func.attr
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }


def _assigns_false(node: ast.AST, attr: str) -> bool:
    """True if ``node`` contains an assignment ``<something>.<attr> = False``."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assign):
            for tgt in sub.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == attr
                    and isinstance(sub.value, ast.Constant)
                    and sub.value.value is False
                ):
                    return True
    return False


def test_auto_reset_cleanup_evicts_cached_agent():
    """The auto-reset cleanup block in gateway/run.py must call
    ``_evict_cached_agent`` so the fresh session does not reuse the previous
    conversation's cached agent (and its leaked
    ``context_compressor._previous_summary``) — the cache is keyed on the
    stable ``session_key`` (#10710)."""
    tree = ast.parse(inspect.getsource(gateway_run))

    # Fingerprint the cleanup branch: the `if <was_auto_reset>:` block that
    # drops transient session state (delegates to the shared boundary-reset
    # helper, #60312, AND consumes the flag by setting was_auto_reset =
    # False). The eviction must live in that same block.
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = _calls(node)
        if (
            "_apply_auto_reset_conversation_boundary" in calls
            and _assigns_false(node, "was_auto_reset")
        ):
            assert "_evict_cached_agent" in calls, (
                "gateway/run.py auto-reset cleanup block must call "
                "`_evict_cached_agent(session_key)` so the auto-reset session "
                "does not reuse the previous cached agent and leak its "
                "context_compressor._previous_summary into new compaction "
                "summaries (#10710)."
            )
            found = True
            break
    assert found, (
        "could not locate the auto-reset transient-state cleanup block in "
        "gateway/run.py (fingerprint: _apply_auto_reset_conversation_boundary "
        "+ was_auto_reset = False)."
    )


def test_evict_cached_agent_method_exists():
    """The eviction helper the cleanup relies on must exist on the runner."""
    assert hasattr(gateway_run.GatewayRunner, "_evict_cached_agent"), (
        "GatewayRunner._evict_cached_agent is the helper the auto-reset "
        "cleanup depends on (#10710)."
    )


def _references_name(node: ast.AST, literal: str) -> bool:
    """True if a string constant equal to ``literal`` appears anywhere under ``node``."""
    return any(
        isinstance(n, ast.Constant) and n.value == literal for n in ast.walk(node)
    )


def _make_runner_for_boundary_reset():
    """Minimal GatewayRunner exposing just what
    _apply_auto_reset_conversation_boundary touches."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._last_resolved_model = {}
    runner._pending_skills_reload_notes = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    return runner


def test_auto_reset_cleanup_clears_last_resolved_model():
    """Regression test for #58403 — behavioral, not AST (#60312 follow-up).

    _apply_auto_reset_conversation_boundary() (the shared helper the
    daily/idle/suspended and compression-exhausted auto-resets both call)
    must pop the session's entry from `_last_resolved_model`, mirroring the
    existing `_session_model_overrides`/`_pending_model_notes` pops it
    performs. Without it, the fresh auto-reset session could serve a model
    cached before the reset on a transient config-cache miss.
    """
    runner = _make_runner_for_boundary_reset()
    key = "telegram:1:chat-1"
    runner._session_model_overrides[key] = {"model": "gpt-5"}
    runner._pending_model_notes[key] = "switched to gpt-5"
    runner._last_resolved_model[key] = "gpt-5"

    runner._apply_auto_reset_conversation_boundary(key)

    assert key not in runner._session_model_overrides
    assert key not in runner._pending_model_notes
    assert key not in runner._last_resolved_model


def test_auto_reset_cleanup_clears_approval_yolo_state():
    """Regression test for #60312 — the same helper must also clear
    approval/YOLO security state (#54878-class boundary), or a /yolo or
    "/approve session" grant from before the reset silently survives into
    the fresh conversation under the same session_key."""
    from tools import approval as approval_mod

    runner = _make_runner_for_boundary_reset()
    target_key = "telegram:1:chat-target"
    other_key = "telegram:1:chat-other"
    approval_mod.enable_session_yolo(target_key)
    approval_mod.enable_session_yolo(other_key)
    try:
        assert approval_mod.is_session_yolo_enabled(target_key) is True

        runner._apply_auto_reset_conversation_boundary(target_key)

        assert approval_mod.is_session_yolo_enabled(target_key) is False
        # Unrelated session's grant must survive — this is a per-session
        # boundary clear, not a global reset.
        assert approval_mod.is_session_yolo_enabled(other_key) is True
    finally:
        approval_mod.disable_session_yolo(target_key)
        approval_mod.disable_session_yolo(other_key)


def test_auto_reset_cleanup_does_not_touch_unrelated_session():
    """Only the target session_key's model-related state is cleared."""
    runner = _make_runner_for_boundary_reset()
    target_key, other_key = "chat-target", "chat-other"
    runner._session_model_overrides[other_key] = {"model": "claude"}
    runner._last_resolved_model[other_key] = "claude"

    runner._apply_auto_reset_conversation_boundary(target_key)

    assert runner._session_model_overrides[other_key] == {"model": "claude"}
    assert runner._last_resolved_model[other_key] == "claude"
