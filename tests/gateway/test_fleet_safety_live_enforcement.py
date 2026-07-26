"""Generation-bound hard-stop integration contracts for the live gateway."""

import asyncio
from types import SimpleNamespace

from gateway.fleet_safety.deadloop_guard import Trip, TripReason
from gateway.fleet_safety.enforcer import GuardEnforcer
from gateway.fleet_safety.integration import _LiveKillActions
from gateway.run import GatewayRunner


class _Agent:
    def __init__(self, session_id: str, generation: int):
        self.session_id = session_id
        self._hermes_run_generation = generation
        self.interrupt_reasons: list[str] = []
        self.on_interrupt = None

    def interrupt(self, reason: str) -> None:
        self.interrupt_reasons.append(reason)
        if self.on_interrupt is not None:
            self.on_interrupt()


def _trip() -> Trip:
    return Trip(
        session_id="session-1",
        reason=TripReason.WALL_CLOCK,
        detail="runtime limit exceeded",
        estimated_tokens=10,
        estimated_calls=2,
        runtime_seconds=100.0,
        provider="subscription-lane",
        model="model-x",
        effort="high",
        last_state="progress:0",
    )


def _runner(agent: _Agent, generation: int):
    runner = object.__new__(GatewayRunner)
    runner._session_run_generation = {"route-key": generation}
    runner._running_agents = {"route-key": agent}
    runner._running_agents_ts = {"route-key": 1.0}
    runner._active_session_leases = {}
    runner._busy_ack_ts = {}
    runner._persist_active_agents = lambda: None
    released: list[tuple[str, int]] = []
    held = {("route-key", 1), ("route-key", 2)}

    def _release_turn_lease(session_key: str, run_generation: int) -> bool:
        released.append((session_key, run_generation))
        key = (session_key, run_generation)
        if key not in held:
            return False
        held.remove(key)
        return True

    runner._release_turn_lease = _release_turn_lease
    runner._get_cached_session_source = lambda _key: None
    evicted: list[str] = []
    runner._evict_cached_agent = evicted.append
    runner._guard_interrupt_drain_timeout_s = 0.0
    return runner, released, held, evicted


def test_live_hard_stop_requests_exact_run_and_leaves_generation_lease_to_unwind():
    agent = _Agent("session-1", 1)
    runner, released, held, evicted = _runner(agent, 1)
    agent.on_interrupt = lambda: runner._running_agents.pop("route-key", None)
    actions = _LiveKillActions(
        runner,
        loop=None,
        mapping={"session-1": ("route-key", agent, 1)},
    )

    result = GuardEnforcer(actions).enforce(_trip())

    assert result.stop_requested is True
    assert result.killed is False
    assert agent.interrupt_reasons == [
        "fleet-safety stop: wall_clock_runtime_exceeded — runtime limit exceeded"
    ]
    assert "route-key" not in runner._running_agents
    assert runner._session_run_generation["route-key"] == 1
    assert released == []
    assert evicted == ["route-key"]
    assert ("route-key", 1) in held
    assert ("route-key", 2) in held
    assert "Interrupt request accepted: yes" in result.report


def test_stale_guard_observation_cannot_interrupt_or_release_newer_run():
    stale_agent = _Agent("session-1", 1)
    current_agent = _Agent("session-1", 2)
    runner, released, held, evicted = _runner(current_agent, 2)
    actions = _LiveKillActions(
        runner,
        loop=None,
        mapping={"session-1": ("route-key", stale_agent, 1)},
    )

    result = GuardEnforcer(actions).enforce(_trip())

    assert result.killed is False
    assert stale_agent.interrupt_reasons == []
    assert current_agent.interrupt_reasons == []
    assert runner._running_agents["route-key"] is current_agent
    assert runner._session_run_generation["route-key"] == 2
    assert released == []
    assert result.lease_released is False
    assert evicted == []
    assert ("route-key", 1) in held
    assert ("route-key", 2) in held
    assert "Interrupt request accepted: no" in result.report


def test_cooperative_interrupt_keeps_slot_and_lease_until_worker_drains():
    agent = _Agent("session-1", 1)
    runner, released, held, evicted = _runner(agent, 1)
    actions = _LiveKillActions(
        runner,
        loop=None,
        mapping={"session-1": ("route-key", agent, 1)},
    )

    result = GuardEnforcer(actions).enforce(_trip())

    assert result.killed is False
    assert result.interrupt_pending is True
    assert result.lease_released is False
    assert runner._running_agents["route-key"] is agent
    assert ("route-key", 1) in held
    assert released == []
    assert evicted == ["route-key"]
    assert "Interrupt request accepted: yes" in result.report
    assert "Lease: retained until generation-safe gateway unwind" in result.report


def test_interrupt_exception_still_targets_origin_and_keeps_lease():
    agent = _Agent("session-1", 1)
    runner, released, held, evicted = _runner(agent, 1)

    def _raise() -> None:
        raise RuntimeError("interrupt failed")

    agent.on_interrupt = _raise
    actions = _LiveKillActions(
        runner,
        loop=None,
        mapping={"session-1": ("route-key", agent, 1)},
    )

    result = GuardEnforcer(actions).enforce(_trip())

    assert actions._notification_target == "session-1"
    assert result.killed is False
    assert result.notified is False
    assert released == []
    assert ("route-key", 1) in held
    assert evicted == []


def test_origin_send_failure_is_reported_as_not_notified(monkeypatch):
    agent = _Agent("session-1", 1)
    runner, _released, _held, _evicted = _runner(agent, 1)
    source = SimpleNamespace(chat_id="owner-chat")
    runner._get_cached_session_source = lambda _key: source
    runner._thread_metadata_for_source = lambda _source: {}

    class _Adapter:
        async def send(self, _chat_id, _text, metadata=None):
            return SimpleNamespace(success=False)

    runner._adapter_for_source = lambda _source: _Adapter()
    loop = asyncio.new_event_loop()

    class _Completed:
        def __init__(self, value):
            self._value = value

        def result(self, timeout=None):
            return self._value

    def _schedule(coro, _loop, **_kwargs):
        return _Completed(loop.run_until_complete(coro))

    monkeypatch.setattr("gateway.run.safe_schedule_threadsafe", _schedule)
    actions = _LiveKillActions(
        runner,
        loop=loop,
        mapping={"session-1": ("route-key", agent, 1)},
    )
    actions._notification_target = "session-1"
    try:
        assert actions.notify("receipt") is False
    finally:
        loop.close()
