"""Regression tests: auto-reset / session-finalization must clear
approval/YOLO security state, the same as /new, /resume, and /branch.

``_clear_session_boundary_security_state()`` (gateway/run.py) is the shared
conversation-boundary cleanup: it pops pending approvals, pending
skill-reload notes, and pending update-prompt state, then clears
session-scoped dangerous-command approvals ("/approve session") and YOLO
bypass via ``tools.approval.clear_session()``. ``/new``, ``/resume``, and
``/branch`` already call it.

Three other reset paths in gateway/run.py reuse the exact same "full
conversation boundary" comment and already mirror it for
``_session_model_overrides`` / ``_set_session_reasoning_override`` /
``_pending_model_notes`` / ``_last_resolved_model`` (see #58403's sibling
fixes), but never called the security-state helper:

- the daily/idle/suspended auto-reset cleanup (``was_auto_reset`` handling)
- the compression-exhausted immediate auto-reset
- ``_session_expiry_watcher``'s permanent session-finalization block (which
  only manually cleared 2 of the helper's 5 sub-clears, missing
  ``_pending_skills_reload_notes`` and the ``tools.approval``/``slash_confirm``
  state)

Without this, a ``/yolo`` or ``/approve session`` grant made before any of
these resets would silently survive into the "fresh" conversation under the
same ``session_key`` — bypassing dangerous-command approval without the user
ever re-granting it.

All three sites now delegate their full boundary-reset behavior (model
override pop, reasoning-override reset, pending-model-notes pop,
last-resolved-model pop, and the security-state clear) to one shared
``_apply_auto_reset_conversation_boundary()`` helper — extracted specifically
so this could be exercised directly instead of via source-shape assertions
(AGENTS.md "never read source code in tests"; the original version of this
file used ``inspect.getsource`` + AST pins, mirroring
test_10710_auto_reset_evicts_cached_agent.py's pre-existing style). Testing
the shared helper thoroughly covers the security-relevant behavior actually
performed by all three call sites; the remaining risk (an accidental
deletion of the one-line call at a site) is a much smaller surface than the
duplicated inline logic this replaced.
"""
from __future__ import annotations

from gateway import run as gateway_run
from tools import approval as approval_mod


def _make_runner():
    """Minimal GatewayRunner exposing just what
    _apply_auto_reset_conversation_boundary / _clear_session_boundary_security_state
    touch — bypasses __init__ (which wires up the full gateway)."""
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_model_overrides = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._last_resolved_model = {}
    runner._pending_skills_reload_notes = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    return runner


class TestClearSessionBoundarySecurityState:
    """Direct coverage of the security-state clear itself."""

    def test_clears_yolo_bypass(self):
        runner = _make_runner()
        key = "telegram:1:chat-1"
        approval_mod.enable_session_yolo(key)
        try:
            assert approval_mod.is_session_yolo_enabled(key) is True
            runner._clear_session_boundary_security_state(key)
            assert approval_mod.is_session_yolo_enabled(key) is False
        finally:
            approval_mod.disable_session_yolo(key)

    def test_clears_session_approved_dangerous_command(self):
        runner = _make_runner()
        key = "telegram:1:chat-2"
        approval_mod.approve_session(key, "rm_rf")
        try:
            assert "rm_rf" in approval_mod._session_approved.get(key, set())
            runner._clear_session_boundary_security_state(key)
            assert "rm_rf" not in approval_mod._session_approved.get(key, set())
        finally:
            approval_mod.clear_session(key)

    def test_clears_pending_skills_reload_notes(self):
        runner = _make_runner()
        key = "telegram:1:chat-3"
        runner._pending_skills_reload_notes[key] = "note"
        runner._clear_session_boundary_security_state(key)
        assert key not in runner._pending_skills_reload_notes

    def test_leaves_unrelated_session_untouched(self):
        runner = _make_runner()
        target, other = "chat-target", "chat-other"
        approval_mod.enable_session_yolo(target)
        approval_mod.enable_session_yolo(other)
        try:
            runner._clear_session_boundary_security_state(target)
            assert approval_mod.is_session_yolo_enabled(target) is False
            assert approval_mod.is_session_yolo_enabled(other) is True
        finally:
            approval_mod.disable_session_yolo(target)
            approval_mod.disable_session_yolo(other)

    def test_noop_on_empty_session_key(self):
        """Must not raise / must not touch global state for a falsy key."""
        runner = _make_runner()
        runner._clear_session_boundary_security_state("")  # no crash


class TestApplyAutoResetConversationBoundary:
    """The shared helper all three sites (daily/idle/suspended auto-reset,
    compression-exhausted auto-reset, session finalization) delegate to."""

    def test_clears_model_and_reasoning_state(self):
        runner = _make_runner()
        key = "telegram:1:chat-4"
        runner._session_model_overrides[key] = {"model": "gpt-5"}
        runner._set_session_reasoning_override(key, {"effort": "high"})
        runner._pending_model_notes[key] = "switched"
        runner._last_resolved_model[key] = "gpt-5"

        runner._apply_auto_reset_conversation_boundary(key)

        assert key not in runner._session_model_overrides
        assert key not in runner._session_reasoning_overrides
        assert key not in runner._pending_model_notes
        assert key not in runner._last_resolved_model

    def test_clears_yolo_and_session_approval(self):
        """The security-relevant half of #60312: a /yolo or '/approve
        session' grant from before ANY of the three boundary resets must
        not survive into the fresh conversation under the same
        session_key."""
        runner = _make_runner()
        key = "telegram:1:chat-5"
        approval_mod.enable_session_yolo(key)
        approval_mod.approve_session(key, "curl_pipe_sh")
        try:
            assert approval_mod.is_session_yolo_enabled(key) is True
            assert "curl_pipe_sh" in approval_mod._session_approved.get(key, set())

            runner._apply_auto_reset_conversation_boundary(key)

            assert approval_mod.is_session_yolo_enabled(key) is False
            assert "curl_pipe_sh" not in approval_mod._session_approved.get(key, set())
        finally:
            approval_mod.clear_session(key)

    def test_unrelated_session_yolo_survives(self):
        """A per-session reset must not bleed into a different session_key
        sharing the same gateway process."""
        runner = _make_runner()
        target, other = "chat-target-2", "chat-other-2"
        approval_mod.enable_session_yolo(target)
        approval_mod.enable_session_yolo(other)
        try:
            runner._apply_auto_reset_conversation_boundary(target)
            assert approval_mod.is_session_yolo_enabled(target) is False
            assert approval_mod.is_session_yolo_enabled(other) is True
        finally:
            approval_mod.disable_session_yolo(target)
            approval_mod.disable_session_yolo(other)
