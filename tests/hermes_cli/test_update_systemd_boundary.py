from __future__ import annotations

import inspect
import subprocess

import pytest

from hermes_cli.update_systemd_boundary import (
    SystemdWorkerBoundary,
    WorkerBoundaryError,
    capture_systemd_worker_boundary,
)


class FakeSystemd:
    def __init__(self, *, stale=(), fail_start=(), failed_on_stop=()):
        self.calls = []
        self.stale = set(stale)
        self.fail_start = set(fail_start)
        self.failed_on_stop = set(failed_on_stop)
        self.after_transition = False
        self.omit = set()
        self.extra = set()
        self.units = {
            "hermes-dashboard.service": self._unit(11, 10, "enabled"),
            "hermes-webui.service": self._unit(12, 11, "enabled"),
            "hermes-gateway.service": self._unit(21, 12, "enabled"),
            "hermes-gateway-work.service": self._unit(22, 13, "enabled"),
            "buzz-health.timer": self._unit(31, 14, "enabled", kind="timer"),
            "buzz-concierge-e2e.timer": self._unit(32, 15, "enabled", kind="timer"),
            "buzz-concierge-e2e.service": self._unit(0, 0, "static", active="inactive"),
        }

    @staticmethod
    def _unit(pid, started, enabled, *, active="active", kind="service"):
        return {
            "pid": pid,
            "started": started,
            "enabled": enabled,
            "active": active,
            "sub": "running" if active == "active" else "dead",
            "kind": kind,
        }

    def _completed(self, cmd, rc=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

    def __call__(self, command):
        cmd = list(command)
        self.calls.append(cmd)
        args = cmd[2:]
        verb = args[0]
        if verb in {"list-unit-files", "list-units"}:
            names = sorted(set(self.units) | self.extra)
            if verb == "list-unit-files":
                out = "".join(
                    f"{name} {self.units.get(name, self._unit(0, 0, 'enabled'))['enabled']}\n"
                    for name in names
                )
            else:
                out = "".join(f"{name} loaded active running description\n" for name in names)
            return self._completed(cmd, stdout=out)
        if verb == "show":
            names = [arg for arg in args[1:] if not arg.startswith("--")]
            blocks = []
            for name in names:
                if name in self.omit:
                    continue
                state = self.units.get(name)
                if state is None and name in self.extra:
                    state = self._unit(90, 100_000_090, "enabled")
                if state is None:
                    continue
                blocks.append(
                    "\n".join(
                        [
                            f"Id={name}",
                            "LoadState=loaded",
                            f"UnitFileState={state['enabled']}",
                            f"ActiveState={state['active']}",
                            f"SubState={state['sub']}",
                            f"MainPID={state['pid']}",
                            f"ExecMainStartTimestampMonotonic={state['started']}",
                            f"ExecStart={{ path=/opt/hermes/venv/bin/hermes ; argv[]=/opt/hermes/venv/bin/hermes {name} ; }}",
                            f"FragmentPath=/home/test/.config/systemd/user/{name}",
                        ]
                    )
                )
            return self._completed(cmd, stdout="\n\n".join(blocks) + "\n")
        name = args[-1]
        if verb == "stop":
            state = self.units[name]
            state["old_pid"] = state["pid"]
            state["old_started"] = state["started"]
            state["pid"] = 0
            state["active"] = "failed" if name in self.failed_on_stop else "inactive"
            state["sub"] = "failed" if name in self.failed_on_stop else "dead"
            return self._completed(cmd)
        if verb == "reset-failed":
            self.units[name]["active"] = "inactive"
            self.units[name]["sub"] = "dead"
            return self._completed(cmd)
        if verb == "start" or (verb == "enable" and "--now" in args):
            if name in self.fail_start:
                return self._completed(cmd, rc=1, stderr="start failed")
            state = self.units[name]
            state["active"] = "active"
            state["sub"] = "running"
            if state["kind"] == "service" and "e2e" not in name:
                if name in self.stale:
                    state["pid"] = state["old_pid"]
                    state["started"] = state["old_started"]
                else:
                    state["pid"] += 1000
                    if state["pid"] == 1000:
                        state["pid"] = 2000
                    state["started"] = 100_000_000 + state["pid"]
            self.after_transition = True
            return self._completed(cmd)
        if verb == "enable":
            return self._completed(cmd)
        raise AssertionError(cmd)


def _transition(fake: FakeSystemd):
    boundary = SystemdWorkerBoundary(fake, monotonic=lambda: 100.0)
    before = boundary.inventory()
    return boundary, before


def test_exact_inventory_captures_required_process_metadata():
    fake = FakeSystemd()
    boundary, before = _transition(fake)

    gateway = before.targets["hermes-gateway.service"]
    assert gateway.enabled_state == "enabled"
    assert gateway.active_state == "active"
    assert gateway.sub_state == "running"
    assert gateway.main_pid == 21
    assert gateway.start_monotonic_usec == 12
    assert "/opt/hermes/venv/bin/hermes" in gateway.exec_start
    assert not any("hermes-gateway*" in part for call in fake.calls for part in call)
    assert before.monitors == ("buzz-concierge-e2e.timer", "buzz-health.timer")
    assert before.concierge_e2e_services == ("buzz-concierge-e2e.service",)


def test_partial_fleet_restart_fails_closed_and_does_not_rearm_monitors():
    fake = FakeSystemd(fail_start={"hermes-gateway-work.service"})
    boundary, before = _transition(fake)

    with pytest.raises(WorkerBoundaryError, match="hermes-gateway-work.service"):
        boundary.transition(before)

    assert not any(call[2:4] == ["enable", "--now"] for call in fake.calls)


@pytest.mark.parametrize("stale", ["hermes-gateway.service", "hermes-gateway-work.service"])
def test_stale_pid_or_start_time_fails_canary_or_fleet(stale):
    fake = FakeSystemd(stale={stale})
    boundary, before = _transition(fake)

    with pytest.raises(WorkerBoundaryError, match="stale (MainPID|start time)"):
        boundary.transition(before)


def test_missing_enabled_unit_is_named():
    fake = FakeSystemd()
    boundary, before = _transition(fake)
    original_inventory = boundary.inventory
    calls = 0

    def inventory_with_missing():
        nonlocal calls
        calls += 1
        if calls == 1:
            fake.units["hermes-gateway-work.service"]["enabled"] = "disabled"
            fake.units["hermes-gateway-work.service"]["active"] = "inactive"
            fake.units["hermes-gateway-work.service"]["pid"] = 0
        return original_inventory()

    boundary.inventory = inventory_with_missing
    with pytest.raises(WorkerBoundaryError, match="missing enabled/running units: hermes-gateway-work.service"):
        boundary.transition(before)


def test_extra_enabled_unit_is_named():
    fake = FakeSystemd()
    boundary, before = _transition(fake)
    original_inventory = boundary.inventory

    def inventory_with_extra():
        fake.extra.add("hermes-gateway-extra.service")
        return original_inventory()

    boundary.inventory = inventory_with_extra
    with pytest.raises(WorkerBoundaryError, match="extra enabled units: hermes-gateway-extra.service"):
        boundary.transition(before)


def test_failed_mainpid_zero_is_quiesced_and_reset_before_start():
    fake = FakeSystemd(failed_on_stop={"hermes-gateway.service"})
    boundary, before = _transition(fake)
    boundary.transition(before)

    reset = fake.calls.index(["systemctl", "--user", "reset-failed", "hermes-gateway.service"])
    start = fake.calls.index(["systemctl", "--user", "start", "hermes-gateway.service"])
    assert reset < start


def test_deactivating_mainpid_zero_is_not_yet_quiesced():
    fake = FakeSystemd()
    boundary, before = _transition(fake)
    original_show = boundary._show
    show_calls = 0

    def show_deactivating(names):
        nonlocal show_calls
        show_calls += 1
        current = original_show(names)
        if show_calls == 1:
            unit = fake.units["hermes-gateway.service"]
            unit["active"] = "deactivating"
            unit["sub"] = "stop-sigterm"
            current = original_show(names)
        return current

    boundary._show = show_deactivating
    with pytest.raises(WorkerBoundaryError, match="workers did not quiesce: hermes-gateway.service"):
        boundary.transition(before)


def test_failed_canary_does_not_start_remaining_gateways_or_monitors():
    fake = FakeSystemd(fail_start={"hermes-gateway.service"})
    boundary, before = _transition(fake)

    with pytest.raises(WorkerBoundaryError, match="hermes-gateway.service"):
        boundary.transition(before)

    assert ["systemctl", "--user", "start", "hermes-gateway-work.service"] not in fake.calls
    assert not any(call[2:4] == ["enable", "--now"] for call in fake.calls)


def test_canary_health_callback_failure_stops_rollout():
    fake = FakeSystemd()
    boundary = SystemdWorkerBoundary(
        fake,
        monotonic=lambda: 100.0,
        canary_verifier=lambda unit: unit.name != "hermes-gateway.service",
    )
    before = boundary.inventory()

    with pytest.raises(WorkerBoundaryError, match="failed the canary health check"):
        boundary.transition(before)

    assert ["systemctl", "--user", "start", "hermes-gateway-work.service"] not in fake.calls
    assert not any(call[2:4] == ["enable", "--now"] for call in fake.calls)


def test_available_runtime_identity_stamp_must_match_installed_generation():
    fake = FakeSystemd()

    def identities():
        return {
            name: {"code_sha": "old-generation"}
            for name in fake.units
            if name.endswith(".service")
        }

    boundary = SystemdWorkerBoundary(
        fake,
        monotonic=lambda: 100.0,
        identity_collector=identities,
    )
    before = boundary.inventory()

    with pytest.raises(WorkerBoundaryError, match="code_sha='old-generation'"):
        boundary.transition(before, expected_identity={"code_sha": "new-generation"})

    assert not any(call[2:4] == ["enable", "--now"] for call in fake.calls)


def test_changed_execstart_fails_intended_runtime_proof():
    fake = FakeSystemd()
    boundary, before = _transition(fake)
    original_show = boundary._show

    def show_changed_runtime(names):
        current = original_show(names)
        if fake.after_transition and "hermes-dashboard.service" in current:
            old = current["hermes-dashboard.service"]
            current["hermes-dashboard.service"] = type(old)(
                **{**old.__dict__, "exec_start": "{ path=/wrong/hermes ; }"}
            )
        return current

    boundary._show = show_changed_runtime
    with pytest.raises(WorkerBoundaryError, match="ExecStart changed"):
        boundary.transition(before)


def test_monitor_and_concierge_rearm_order_after_fleet_proof():
    fake = FakeSystemd()
    boundary, before = _transition(fake)
    boundary.transition(before)

    # transition proves workers but must leave monitors paused until the
    # updater's later code-SHA fleet matrix succeeds.
    assert not any(call[2:4] == ["enable", "--now"] for call in fake.calls)
    boundary.rearm_monitors(before)

    def index(suffix):
        return next(i for i, call in enumerate(fake.calls) if call[2:] == suffix)

    first_stop = index(["stop", "hermes-dashboard.service"])
    assert index(["stop", "buzz-health.timer"]) < first_stop
    assert index(["start", "hermes-dashboard.service"]) < index(["start", "hermes-gateway.service"])
    assert index(["start", "hermes-gateway.service"]) < index(["start", "hermes-gateway-work.service"])
    timer = index(["enable", "--now", "buzz-concierge-e2e.timer"])
    concierge = index(["start", "buzz-concierge-e2e.service"])
    assert index(["start", "hermes-gateway-work.service"]) < timer < concierge


def test_monitor_rearm_failure_is_fatal():
    fake = FakeSystemd(fail_start={"buzz-concierge-e2e.timer"})
    boundary, before = _transition(fake)
    boundary.transition(before)

    with pytest.raises(WorkerBoundaryError, match="buzz-concierge-e2e.timer"):
        boundary.rearm_monitors(before)


def test_probe_failure_is_fatal_when_systemd_is_relevant():
    def failed(command):
        return subprocess.CompletedProcess(command, 1, "", "user bus unavailable")

    with pytest.raises(WorkerBoundaryError, match="user bus unavailable"):
        capture_systemd_worker_boundary(relevant=True, runner=failed)


def test_non_systemd_platform_path_is_noop():
    def unused(command):
        raise AssertionError(command)

    assert capture_systemd_worker_boundary(relevant=False, runner=unused) is None


def test_supported_update_entrypoint_orders_boundary_and_authoritative_proof():
    """Pin the owning `hermes update` wiring, not only the pure helper."""
    from hermes_cli import update_cmd

    source = inspect.getsource(update_cmd._cmd_update_impl)
    capture = source.index("capture_systemd_worker_boundary(")
    source_mutation = source.index("# Resolve the target branch up front")
    transition = source.index("_systemd_worker_boundary.transition(")
    generic_restart = source.index("# Auto-restart ALL gateways after update.")
    fleet_matrix = source.index("# Phase 1 (#91277): post-update fleet version verification")
    rearm = source.index("_systemd_worker_boundary.rearm_monitors(")
    receipt = source.index("from hermes_cli.update_receipt import finalize_update_receipt", rearm)

    assert capture < source_mutation < transition < generic_restart
    assert generic_restart < fleet_matrix < rearm < receipt
    assert "if _systemd_boundary_units:" in source
    assert "if svc_name in _systemd_boundary_units:" in source
