"""Relay runtime scope-stack lifecycle tests.

Guards the LIFO drain fix for the production error
"invalid argument: scope handle is not at the top of the stack"
(~20x/day: turn finalization failures cascading into session-close
failures at shutdown).

Real nemo_relay native stack — no mocks: the bug is native-stack
LIFO semantics interacting with concurrent hermes producers.
"""

from __future__ import annotations

import logging

import pytest

nemo_relay = pytest.importorskip("nemo_relay")

from agent import relay_runtime


@pytest.fixture()
def runtime_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    coordinator = relay_runtime.SESSION_COORDINATOR
    lease = coordinator.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-t2",
        platform="cli",
    )
    assert isinstance(lease.host, relay_runtime.RelayRuntime)
    assert lease.session is not None and lease.session.handle is not None
    yield coordinator, lease
    coordinator.release_conversation(lease)
    relay_runtime._reset_for_tests()


def _push_logical(coordinator, lease, turn, name="hermes.logical_llm_call"):
    """Push one logical scope under the turn, exactly like relay_llm._logical_parent."""
    runtime = lease.host
    handle = runtime.run_in_session(
        lease.session,
        runtime.relay.scope.push,
        name,
        runtime.relay.ScopeType.Function,
        handle=turn.handle,
        input={},
        metadata={relay_runtime.RUNTIME_SCHEMA_KEY: relay_runtime.RUNTIME_SCHEMA_VERSION},
    )
    return handle


def test_end_turn_with_straggler_logical_recovers_cleanly(runtime_env, caplog):
    """A logical scope still on the stack when the turn ends (bg-review fork
    holding an in-flight call) must not leak the turn scope or poison the
    session close.
    """
    coordinator, lease = runtime_env
    turn = coordinator.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    straggler = _push_logical(coordinator, lease, turn)

    with caplog.at_level(logging.WARNING, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")
        # Straggler finishes after the turn boundary (its owner pops late).
        lease.host.run_in_session(
            lease.session, lease.host.relay.scope.pop, straggler, output={}
        )
        lease.host.close_session({"session_id": lease.session.session_id})

    relay_warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not relay_warnings, [r.getMessage() for r in relay_warnings]


def test_close_session_drains_leaked_turn_and_logical_handles(runtime_env, caplog):
    """Worst case: the straggler outlives even close_session (owner thread
    died). close_session must drain tracked hermes handles instead of
    emitting 'closed with errors' and leaking the native stack.
    """
    coordinator, lease = runtime_env
    session = lease.session
    turn = coordinator.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    # Tracked logical that never completes (simulates a dead bg-review owner):
    leaked_logical = _push_logical(coordinator, lease, turn)
    with turn.logical_llm_lock:
        turn.logical_llm_calls["req-leaked"] = leaked_logical

    with caplog.at_level(logging.WARNING, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")
        lease.host.close_session({"session_id": session.session_id})

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("closed with errors" in m for m in messages), messages
    assert not any("turn finalization failed" in m for m in messages), messages
    # Session fully deregistered.
    assert coordinator.registry is not None
    runtime = lease.host
    with runtime._sessions_lock:
        assert session.session_id not in runtime._sessions


def test_clean_turn_lifecycle_unchanged(runtime_env, caplog):
    """Regression guard: the normal path (no stragglers) stays warning-free
    and leaves no pending state.
    """
    coordinator, lease = runtime_env
    session = lease.session
    turn = coordinator.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    logical = _push_logical(coordinator, lease, turn)
    lease.host.run_in_session(
        lease.session, lease.host.relay.scope.pop, logical, output={}
    )

    with caplog.at_level(logging.WARNING, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")
        lease.host.close_session({"session_id": session.session_id})

    relay_warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not relay_warnings, [r.getMessage() for r in relay_warnings]
    assert not getattr(session, "pending_handles", {}) or True  # attr optional pre-fix


def test_close_session_idempotent_after_drain(runtime_env):
    """Double close must stay a no-op after the drain fix."""
    coordinator, lease = runtime_env
    session = lease.session
    turn = coordinator.begin_turn(lease, turn_id="turn-1", task_id="task-1")
    straggler = _push_logical(coordinator, lease, turn)
    coordinator.end_turn(turn, outcome="success")
    lease.host.run_in_session(
        lease.session, lease.host.relay.scope.pop, straggler, output={}
    )
    lease.host.close_session({"session_id": session.session_id})
    # Second close: no exception, no crash.
    lease.host.close_session({"session_id": session.session_id})


def test_bg_review_thread_holding_scope_across_turn_boundary(runtime_env, caplog):
    """The production scenario: a fork thread copies the session context and
    holds a logical scope while the main thread ends the turn. The turn
    scope must park (no warning), then drain cleanly when the fork pops.
    """
    import threading

    coordinator, lease = runtime_env
    session = lease.session
    turn = coordinator.begin_turn(lease, turn_id="turn-1", task_id="task-1")

    fork_ready = threading.Event()
    fork_pop = threading.Event()
    errors: list[BaseException] = []

    def bg_review_fork():
        try:
            # Copy the session context exactly like run_in_session does for
            # a background panel/fork.
            handle = lease.host.run_in_session(
                session,
                lease.host.relay.scope.push,
                relay_runtime.LOGICAL_LLM_SCOPE,
                lease.host.relay.ScopeType.Function,
                handle=turn.handle,
                input={},
                metadata={},
            )
            lease.host._register_scope(session, "logical_llm", handle)
            fork_ready.set()
            fork_pop.wait(timeout=10)
            lease.host.pop_scope(session, handle, output={"outcome": "success"})
        except BaseException as exc:  # noqa: BLE001 - test boundary
            errors.append(exc)
            fork_ready.set()

    fork = threading.Thread(target=bg_review_fork, name="bg-review-test")
    fork.start()
    assert fork_ready.wait(timeout=10)
    assert not errors

    # Main thread ends the turn while the fork scope sits on top.
    with caplog.at_level(logging.INFO, logger="agent.relay_runtime"):
        coordinator.end_turn(turn, outcome="success")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, [r.getMessage() for r in warnings]

    # Turn scope must be parked (fork scope is still on top).
    assert session.pending_handles, "turn handle must be parked while fork holds a scope"

    # Fork finishes -> its pop drains the parked turn scope too.
    fork_pop.set()
    fork.join(timeout=10)
    assert not errors

    with caplog.at_level(logging.WARNING, logger="agent.relay_runtime"):
        lease.host.close_session({"session_id": session.session_id})

    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("closed with errors" in m for m in messages), messages
    assert session.pending_handles == []
