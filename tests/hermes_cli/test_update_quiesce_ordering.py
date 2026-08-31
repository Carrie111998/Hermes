"""Fail-closed pre-mutation quiescing for live self-updates.

A shared checkout whose HEAD moves while gateway/dashboard/serve runtimes
still run the old module graph produces torn imports: a long-lived
interpreter lazily imports a *new* module into an *old* graph minutes
before the restart phase runs.

The contract these tests pin:

1. Updater ownership is established OUTSIDE every affected runtime's
   supervisor cgroup/process tree before anything is touched.
2. Every inventoried runtime is stopped and its old PID observed gone
   BEFORE any git or dependency mutation.
3. Isolation failure, an incomplete inventory, or any failed stop aborts
   the update before mutation — never after.

No live gateway, no network: runtimes are fake records and stop/probe are
injected.
"""

from __future__ import annotations

import pytest

from hermes_cli import update_quiesce
from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan


def _plan(*runtimes) -> UpdatePlan:
    plan = UpdatePlan()
    plan.runtimes = list(runtimes)
    return plan


def _gateway(pid=4242, profile="default", unit="hermes-gateway.service"):
    return RuntimeRecord(
        kind="gateway",
        profile=profile,
        pid=pid,
        supervisor="systemd",
        restart_via="systemd",
        unit=unit,
        unit_scope="user",
    )


class _Fleet:
    """Fake runtime fleet recording the exact order of operations."""

    def __init__(self, *, alive, stop_fails=(), never_exits=()):
        self.alive = set(alive)
        self.stop_fails = set(stop_fails)
        self.never_exits = set(never_exits)
        self.events: list[str] = []

    def stop(self, runtime):
        pid = runtime.pid
        self.events.append(f"stop:{pid}")
        if pid in self.stop_fails:
            return False
        if pid not in self.never_exits:
            self.alive.discard(pid)
        return True

    def pid_alive(self, pid):
        return pid in self.alive

    def mutate(self):
        self.events.append("mutate")


@pytest.fixture(autouse=True)
def _reset_authorization():
    """Mutation authorization is process-global; never let it leak between tests."""
    update_quiesce.reset_mutation_authorization()
    yield
    update_quiesce.reset_mutation_authorization()


def _isolated(_pid=None):
    """Isolation probe that reports the updater as fully detached."""
    return update_quiesce.IsolationResult(isolated=True, reason="test-isolated")


def test_every_runtime_stops_before_any_mutation():
    fleet = _Fleet(alive={4242, 4243})
    plan = _plan(_gateway(4242), _gateway(4243, profile="zeus"))

    report = update_quiesce.run_pre_mutation_quiesce(
        plan,
        stop_runtime=fleet.stop,
        pid_alive=fleet.pid_alive,
        assess_isolation=_isolated,
        exit_timeout=1.0,
        poll_interval=0.01,
    )
    update_quiesce.assert_mutation_authorized("git")
    fleet.mutate()

    assert report.quiesced_pids == [4242, 4243]
    assert fleet.events == ["stop:4242", "stop:4243", "mutate"]


def test_failed_stop_aborts_before_mutation():
    fleet = _Fleet(alive={4242, 4243}, stop_fails={4243})
    plan = _plan(_gateway(4242), _gateway(4243, profile="zeus"))

    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.run_pre_mutation_quiesce(
            plan,
            stop_runtime=fleet.stop,
            pid_alive=fleet.pid_alive,
            assess_isolation=_isolated,
            exit_timeout=1.0,
            poll_interval=0.01,
        )

    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.assert_mutation_authorized("git")
    assert "mutate" not in fleet.events


def test_pid_that_never_exits_aborts_before_mutation():
    fleet = _Fleet(alive={4242}, never_exits={4242})
    plan = _plan(_gateway(4242))

    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.run_pre_mutation_quiesce(
            plan,
            stop_runtime=fleet.stop,
            pid_alive=fleet.pid_alive,
            assess_isolation=_isolated,
            exit_timeout=0.3,
            poll_interval=0.01,
        )

    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.assert_mutation_authorized("dependencies")
    assert "mutate" not in fleet.events


def test_isolation_failure_aborts_before_any_stop():
    fleet = _Fleet(alive={4242})
    plan = _plan(_gateway(4242))

    def not_isolated(_pid=None):
        return update_quiesce.IsolationResult(
            isolated=False, reason="updater shares hermes-gateway.service cgroup"
        )

    with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
        update_quiesce.run_pre_mutation_quiesce(
            plan,
            stop_runtime=fleet.stop,
            pid_alive=fleet.pid_alive,
            assess_isolation=not_isolated,
            exit_timeout=1.0,
            poll_interval=0.01,
        )

    assert "cgroup" in str(excinfo.value)
    assert fleet.events == []
    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.assert_mutation_authorized("git")


def test_incomplete_inventory_aborts_before_any_stop():
    fleet = _Fleet(alive={4242})
    # A runtime the inventory saw but could not attribute a PID to: the
    # update cannot prove it stopped, so it must not mutate the checkout.
    plan = _plan(_gateway(4242), RuntimeRecord(kind="dashboard", profile="default"))

    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.run_pre_mutation_quiesce(
            plan,
            stop_runtime=fleet.stop,
            pid_alive=fleet.pid_alive,
            assess_isolation=_isolated,
            exit_timeout=1.0,
            poll_interval=0.01,
        )

    assert fleet.events == []


def test_missing_plan_aborts():
    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.run_pre_mutation_quiesce(
            None,
            stop_runtime=lambda r: True,
            pid_alive=lambda p: False,
            assess_isolation=_isolated,
        )


def test_empty_fleet_authorizes_mutation():
    report = update_quiesce.run_pre_mutation_quiesce(
        _plan(),
        stop_runtime=lambda r: True,
        pid_alive=lambda p: False,
        assess_isolation=_isolated,
    )
    assert report.quiesced_pids == []
    update_quiesce.assert_mutation_authorized("git")


class _StubbornFleet(_Fleet):
    """Ignores the graceful stop; only a forced escalation gets it out."""

    def __init__(self, *, alive, escalation_works=True):
        super().__init__(alive=alive, never_exits=set(alive))
        self.escalation_works = escalation_works

    def escalate(self, runtime):
        self.events.append(f"escalate:{runtime.pid}")
        if self.escalation_works:
            self.alive.discard(runtime.pid)
            self.never_exits.discard(runtime.pid)


def test_a_runtime_that_ignores_the_graceful_stop_is_escalated():
    """A wedged gateway must not abort the whole update: escalate, then
    confirm it is really gone."""
    fleet = _StubbornFleet(alive={4242})
    plan = _plan(_gateway(4242))

    report = update_quiesce.run_pre_mutation_quiesce(
        plan,
        stop_runtime=fleet.stop,
        pid_alive=fleet.pid_alive,
        assess_isolation=_isolated,
        escalate=fleet.escalate,
        exit_timeout=0.2,
        escalated_exit_timeout=1.0,
        poll_interval=0.01,
    )

    assert report.quiesced_pids == [4242]
    assert fleet.events == ["stop:4242", "escalate:4242"]
    update_quiesce.assert_mutation_authorized("git")


def test_escalation_that_does_not_work_still_aborts_before_mutation():
    fleet = _StubbornFleet(alive={4242}, escalation_works=False)
    plan = _plan(_gateway(4242))

    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.run_pre_mutation_quiesce(
            plan,
            stop_runtime=fleet.stop,
            pid_alive=fleet.pid_alive,
            assess_isolation=_isolated,
            escalate=fleet.escalate,
            exit_timeout=0.2,
            escalated_exit_timeout=0.2,
            poll_interval=0.01,
        )

    assert fleet.events == ["stop:4242", "escalate:4242"]
    with pytest.raises(update_quiesce.QuiesceAbort):
        update_quiesce.assert_mutation_authorized("git")
