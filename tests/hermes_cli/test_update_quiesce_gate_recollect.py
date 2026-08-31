"""The gate must quiesce the fleet as it is NOW (#99450 R2-7).

``collect_runtime_inventory()`` ran once, at the top of ``_cmd_update_impl``,
to print the fleet banner. The quiesce gate fires much later — after the
fetch, the branch inspection and the up-to-date check — and it re-used that
same stale plan. Anything that started in between (a Desktop launching its
backend, a watcher restarting a gateway, an operator running ``hermes
serve``) was never in the plan, so it was never stopped, and it kept
importing from the checkout the update then mutated.

The gate now re-collects, quiesces what it finds, and then sweeps again:
a runtime that appeared during the stop loop is stopped on the next pass,
and one that will not stay down aborts the update with nothing mutated.
Runtimes already proven gone are never signalled twice.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd, update_quiesce
from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan


@pytest.fixture(autouse=True)
def _reset():
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.reset_mutation_authorization()
    update_quiesce.clear_restart_pending_state()


def _runtime(pid, *, start_time=1.0, profile="default"):
    return RuntimeRecord(
        kind="gateway",
        profile=profile,
        pid=pid,
        supervisor="systemd",
        restart_via="systemd",
        unit=f"hermes-gateway-{pid}.service",
        unit_scope="user",
        detail={"start_time": start_time},
    )


def _plan(*runtimes, errors=()):
    plan = UpdatePlan()
    plan.expected_sha = "a" * 40
    plan.runtimes = list(runtimes)
    plan.discovery_errors = list(errors)
    return plan


def _quiesce(recollect, stopped, **kw):
    alive: set = kw.pop("alive", set())

    def _stop(runtime):
        stopped.append(runtime.pid)
        alive.discard(runtime.pid)
        return True

    return update_quiesce.run_pre_mutation_quiesce(
        _plan(),
        stop_runtime=kw.pop("stop_runtime", _stop),
        pid_alive=lambda pid: pid in alive,
        assess_isolation=lambda plan: update_quiesce.IsolationResult(
            isolated=True, reason="test"
        ),
        exit_timeout=1.0,
        poll_interval=0.01,
        persist_state=False,
        recollect=recollect,
        **kw,
    )


class TestTheGateReCollects:
    def test_a_runtime_that_appeared_after_the_banner_is_stopped(self):
        """The stale plan is empty; the fleet is not."""
        seen: list = []
        plans = [_plan(_runtime(100)), _plan()]

        report = _quiesce(lambda: plans.pop(0), seen, alive={100})

        assert seen == [100]
        assert report.quiesced_pids == [100]
        assert update_quiesce.authorized_report() is report

    def test_a_runtime_that_appeared_during_the_stop_loop_is_stopped_too(self):
        """The post-stop sweep is what stops the escape hatch being open for
        the whole duration of the stop loop."""
        seen: list = []
        alive = {100, 200}
        plans = [
            _plan(_runtime(100)),               # gate
            _plan(_runtime(200)),               # sweep: something new appeared
            _plan(),                            # sweep: clean
        ]

        report = _quiesce(lambda: plans.pop(0), seen, alive=alive)

        assert seen == [100, 200]
        assert sorted(report.quiesced_pids) == [100, 200]

    def test_an_already_quiesced_runtime_is_never_signalled_twice(self):
        """A collector that keeps reporting a row we already proved gone must
        not turn into an endless re-stop."""
        seen: list = []
        # The same identity every time — pid AND start time.
        plans = [_plan(_runtime(100)), _plan(_runtime(100)), _plan(_runtime(100))]

        report = _quiesce(lambda: plans.pop(0), seen, alive={100})

        assert seen == [100], "one stop per proven identity"
        assert report.quiesced_pids == [100]

    def test_a_recycled_pid_is_a_different_runtime_and_is_stopped(self):
        """Same PID, different start time — the kernel handed the number to
        something new, and that something is still live."""
        seen: list = []
        alive = {100}
        plans = [
            _plan(_runtime(100, start_time=1.0)),
            _plan(_runtime(100, start_time=999.0)),
            _plan(),
        ]

        report = _quiesce(lambda: plans.pop(0), seen, alive=alive)

        assert seen == [100, 100]
        assert report.quiesced_pids == [100, 100]


class TestTheGateFailsClosed:
    def test_a_runtime_that_will_not_stay_down_aborts_the_update(self):
        """Bounded: a respawning runtime must abort, not spin forever."""
        seen: list = []
        counter = {"n": 0}

        def _recollect():
            counter["n"] += 1
            return _plan(_runtime(100 + counter["n"], start_time=float(counter["n"])))

        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            _quiesce(_recollect, seen, alive=set())

        assert "still running" in str(excinfo.value) or "did not" in str(excinfo.value)
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.assert_mutation_authorized("git")

    def test_a_gate_collection_that_raises_aborts_the_update(self):
        def _boom():
            raise RuntimeError("probe exploded")

        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            _quiesce(_boom, [])

        assert "probe exploded" in str(excinfo.value)

    def test_a_gate_collection_that_returns_nothing_aborts_the_update(self):
        with pytest.raises(update_quiesce.QuiesceAbort):
            _quiesce(lambda: None, [])

    def test_an_incomplete_gate_inventory_aborts_the_update(self):
        """Discovery errors at the GATE matter, not just at the banner."""
        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            _quiesce(lambda: _plan(errors=["ledger probe: boom"]), [])

        assert "ledger probe" in str(excinfo.value)

    def test_an_incomplete_sweep_inventory_aborts_the_update(self):
        seen: list = []
        plans = [_plan(_runtime(100)), _plan(errors=["ledger probe: boom"])]

        with pytest.raises(update_quiesce.QuiesceAbort):
            _quiesce(lambda: plans.pop(0), seen, alive={100})

        assert seen == [100]
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.assert_mutation_authorized("git")

    def test_isolation_is_assessed_against_the_fresh_fleet(self):
        """The updater's own ownership question is about the runtimes it is
        actually going to stop."""
        assessed: list = []
        fresh = _plan(_runtime(100))

        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                _plan(),
                stop_runtime=lambda r: True,
                pid_alive=lambda pid: False,
                assess_isolation=lambda plan: (
                    assessed.append(plan)
                    or update_quiesce.IsolationResult(isolated=False, reason="nope")
                ),
                recollect=lambda: fresh,
                persist_state=False,
            )

        assert assessed == [fresh]


# ---------------------------------------------------------------------------
# End-to-end wiring through `_cmd_update_impl`
# ---------------------------------------------------------------------------


def test_a_runtime_that_appears_before_the_gate_stops_before_any_git_mutation(
    monkeypatch, tmp_path
):
    """The scenario the stale plan could not see: nothing is running when the
    banner prints, a gateway is running by the time the update writes."""
    from tests.hermes_cli import test_update_quiesce_integration as harness

    events: list[str] = []
    harness._patch_update_deps(monkeypatch, tmp_path, events)

    calls = {"n": 0}

    def _collect():
        calls["n"] += 1
        if calls["n"] == 1:
            events.append("inventory:banner")
            return _plan()  # the machine looks idle
        events.append(f"inventory:{calls['n']}")
        # A gateway started between the banner and the gate; once stopped it
        # stops appearing, exactly as the real collectors behave.
        return _plan(_runtime(4242)) if calls["n"] == 2 else _plan()

    monkeypatch.setattr(
        "hermes_cli.update_inventory.collect_runtime_inventory", _collect
    )

    alive = {4242}

    def _stop(runtime):
        events.append(f"stop:{runtime.pid}")
        alive.discard(runtime.pid)
        return True

    for module in (update_cmd, harness.hermes_main):
        monkeypatch.setattr(module, "_stop_runtime_for_quiesce", _stop)
        monkeypatch.setattr(module, "_runtime_pid_alive", lambda pid: pid in alive)
        monkeypatch.setattr(
            module, "_probe_relaunched_runtime_sha", lambda *a, **k: "b" * 40
        )
    monkeypatch.setattr(
        update_quiesce,
        "assess_updater_isolation",
        lambda plan, **kw: update_quiesce.IsolationResult(isolated=True, reason="t"),
    )
    monkeypatch.setattr(update_cmd, "_run_supervisor_command", lambda argv: True)

    try:
        update_cmd._cmd_update_impl(
            SimpleNamespace(branch=None, yes=True, force=False, force_venv=False),
            gateway_mode=False,
        )
    except SystemExit:
        pass

    assert "stop:4242" in events, events
    merges = [i for i, e in enumerate(events) if e.startswith("git:") and " merge" in e]
    assert merges, "the harness must observe the git merge"
    assert events.index("stop:4242") < merges[0], events
