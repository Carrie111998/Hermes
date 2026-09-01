"""MoA one-shot model override must be restored on both success and failure.

These exercise the real ``GatewayRunner._install_moa_one_shot`` helper (which
parks the prior per-session override in the same ``conversation.one_turn_restore``
slot that ``/model --once`` uses) and the real
``GatewayRunner._restore_pending_one_turn_model_override`` helper that the
message-handling ``finally`` block calls, so they prove the production logic —
not a re-implementation of it. The bug
being guarded: the restore used to live in the ``try`` block, so a turn that
raised skipped it and the MoA override leaked permanently (every later message
silently fanned out through MoA).
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.run import GatewayRunner


def _make_runner():
    """Minimal GatewayRunner with only the fields the restore helper reads."""
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._evict_cached_agent = MagicMock()
    runner._rehydrate_session_runtime_options = lambda key: None
    return runner


def _install_moa(runner, key, prior_override):
    """Run the real post-claim install: snapshot into the slot, then swap."""
    runner._session_state(key).conversation.model_override = prior_override
    event = SimpleNamespace(_moa_pending_preset="default")
    assert runner._install_moa_one_shot(event, key) is True
    assert event._moa_pending_preset is None
    runner._evict_cached_agent.reset_mock()
    conv = runner._session_state(key).conversation
    assert conv.model_override["provider"] == "moa"
    assert conv.one_turn_restore == {
        "had_override": prior_override is not None,
        "override": prior_override,
    }


def test_restore_runs_from_finally_even_when_turn_raises():
    """The whole point of the fix: a raising turn still reverts the override.

    Mirrors the real call site — the restore is invoked from a ``finally`` block,
    so it fires after an exception propagates out of the turn body.
    """
    runner = _make_runner()
    key = "agent:main:telegram:dm:999"
    _install_moa(runner, key, {"provider": "openrouter", "model": "gpt-4"})

    with __import__("pytest").raises(RuntimeError):
        try:
            raise RuntimeError("provider error mid-turn")
        finally:
            runner._restore_pending_one_turn_model_override(key)

    assert runner._session_model_overrides[key] == {
        "provider": "openrouter",
        "model": "gpt-4",
    }
    assert runner._session_state(key).conversation.one_turn_restore is None
    runner._evict_cached_agent.assert_called_once_with(key)


def test_restore_clears_override_when_user_had_none():
    """No prior override: the MoA override is cleared outright, not kept."""
    runner = _make_runner()
    key = "agent:main:telegram:dm:1000"
    _install_moa(runner, key, None)
    assert runner._session_model_overrides[key]["provider"] == "moa"

    runner._restore_pending_one_turn_model_override(key)

    assert runner._session_model_overrides.get(key) is None
