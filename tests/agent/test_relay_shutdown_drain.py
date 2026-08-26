"""Tests for draining active turns when a Relay session shuts down."""

from __future__ import annotations

import contextvars
from types import SimpleNamespace

import pytest

from agent import relay_runtime


class _FakeNemoRelay:
    def __init__(self):
        self.events: list[tuple] = []
        self._scope_serial = 0
        self._scope_context = contextvars.ContextVar("fake_nemo_relay_scope", default=None)
        self.ScopeType = SimpleNamespace(Agent="agent", Function="function")
        self.scope = SimpleNamespace(
            push=self._scope_push,
            pop=self._scope_pop,
        )
        self.subscribers = SimpleNamespace(
            flush=lambda: self.events.append(("subscribers.flush",)),
        )
        self.get_scope_stack = self._get_scope_stack

    def _get_scope_stack(self):
        current = self._scope_context.get()
        self.events.append(("scope.sync", current))
        return current

    def _scope_push(self, name, scope_type, **kwargs):
        self._scope_serial += 1
        handle = ("scope", name, self._scope_serial)
        self._scope_context.set(handle)
        self.events.append(("scope.push", name, scope_type, kwargs))
        return handle

    def _scope_pop(self, handle, **kwargs):
        self.events.append(("scope.pop", handle, kwargs))


def test_close_active_turns_for_session_ends_open_turns(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    fake_relay = _FakeNemoRelay()
    monkeypatch.setattr(relay_runtime, "_load_nemo_relay", lambda: fake_relay)

    coordinator = relay_runtime.SESSION_COORDINATOR
    lease = coordinator.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-drain-1",
        platform="cli",
    )
    turn = coordinator.begin_turn(
        lease,
        turn_id="turn-drain-1",
        task_id="task-drain-1",
    )

    assert coordinator.has_active_turn(
        profile_key=lease.host.profile_key, session_id="session-drain-1"
    )

    # Draining the turns should end the turn with outcome="interrupted_shutdown"
    failures = coordinator.close_active_turns_for_session(
        profile_key=lease.host.profile_key, session_id="session-drain-1"
    )
    assert failures == []
    assert not coordinator.has_active_turn(
        profile_key=lease.host.profile_key, session_id="session-drain-1"
    )
    assert turn.closed is True

    # Check that turn scope pop occurred with interrupted_shutdown outcome
    pop_events = [e for e in fake_relay.events if e[0] == "scope.pop"]
    assert len(pop_events) == 1
    assert pop_events[0][2].get("output") == {"outcome": "interrupted_shutdown"}

    coordinator.release_conversation(lease)
    relay_runtime._reset_for_tests()


def test_close_session_drains_active_turns_before_session_scope_pop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    relay_runtime._reset_for_tests()
    fake_relay = _FakeNemoRelay()
    monkeypatch.setattr(relay_runtime, "_load_nemo_relay", lambda: fake_relay)

    coordinator = relay_runtime.SESSION_COORDINATOR
    lease = coordinator.acquire_conversation(
        profile_key=relay_runtime.current_profile_key(),
        session_id="session-drain-2",
        platform="cli",
    )
    turn = coordinator.begin_turn(
        lease,
        turn_id="turn-drain-2",
        task_id="task-drain-2",
    )

    # Session is closed while turn is still mid-flight
    lease.host.close_session({"session_id": "session-drain-2"})

    # Check order of pop events: turn scope must be popped BEFORE session scope
    pop_events = [e for e in fake_relay.events if e[0] == "scope.pop"]
    assert len(pop_events) == 2
    # Turn pop is first
    assert pop_events[0][1][1] == relay_runtime.TURN_SCOPE
    assert pop_events[0][2].get("output") == {"outcome": "interrupted_shutdown"}
    # Session pop is second
    assert pop_events[1][1][1] == relay_runtime.SESSION_SCOPE

    assert not coordinator.has_active_turn(
        profile_key=lease.host.profile_key, session_id="session-drain-2"
    )
    coordinator.release_conversation(lease)
    relay_runtime._reset_for_tests()
