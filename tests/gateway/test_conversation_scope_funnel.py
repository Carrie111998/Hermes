"""Behavior tests for _clear_conversation_scope — the single conversation-
boundary funnel (#64934 follow-up).

Boundaries (/new, /resume, auto-reset, expiry finalization,
compression-exhausted reset) used to each carry a hand-copied pop-list of the
per-session dicts, and the lists drifted whenever a new dict was added
(#48031, #58403, #10702, #35809 were all "boundary X forgot dict Y" bugs).
The funnel clears every dict registered in _CONVERSATION_SCOPED_STATE plus
the boundary security state, in one call.
"""

from gateway.run import _CONVERSATION_SCOPED_STATE, GatewayRunner

KEY = "agent:main:telegram:dm:777"
OTHER = "agent:main:discord:dm:888"


def _bare_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    for attr in _CONVERSATION_SCOPED_STATE:
        setattr(runner, attr, {KEY: object(), OTHER: object()})
    # Turn-scoped state that the funnel must NOT touch.
    runner._running_agents = {KEY: object()}
    runner._running_agents_ts = {KEY: 1.0}
    runner._session_run_generation = {KEY: 7}
    return runner


def test_funnel_leaves_turn_scoped_and_generation_state_alone():
    runner = _bare_runner()
    runner._clear_conversation_scope(KEY, reason="test")
    # Turn-scoped: owned by _release_running_agent_state / dispatch finally.
    assert KEY in runner._running_agents
    assert KEY in runner._running_agents_ts
    # Generation counter is monotonic by design (#28686) — never reset.
    assert runner._session_run_generation[KEY] == 7


def test_funnel_is_bare_runner_safe_and_empty_key_noop():
    runner = object.__new__(GatewayRunner)
    # No dicts initialized at all — must not raise (pitfall #17).
    runner._clear_conversation_scope(KEY, reason="test")
    runner._clear_conversation_scope("", reason="test")


def test_funnel_also_clears_boundary_security_state():
    runner = _bare_runner()
    runner._pending_approvals = {KEY: {"cmd": "rm -rf"}, OTHER: {}}
    runner._update_prompt_pending = {KEY: True}
    runner._pending_skills_reload_notes = {KEY: "note"}
    runner._clear_conversation_scope(KEY, reason="test")
    assert KEY not in runner._pending_approvals
    assert OTHER in runner._pending_approvals
    assert KEY not in runner._update_prompt_pending
    assert KEY not in runner._pending_skills_reload_notes


def test_context_segment_boundary_preserves_conversation_state():
    runner = _bare_runner()
    runner._set_pending_turn_sidecar_notes(KEY, ["stale segment note"])
    runner._pending_approvals = {KEY: {"cmd": "danger"}, OTHER: {}}
    runner._update_prompt_pending = {KEY: True}

    runner._clear_context_segment_scope(KEY)

    for attr in _CONVERSATION_SCOPED_STATE:
        store = getattr(runner, attr)
        if attr == "_pending_turn_sidecar_notes":
            assert KEY not in store
        else:
            assert KEY in store, f"{attr} lost across context rollover"
        assert OTHER in store
    state = runner._peek_session_state(KEY)
    assert state is not None
    assert state.conversation.sidecar_notes == []
    assert KEY not in runner._pending_approvals
    assert KEY not in runner._update_prompt_pending
