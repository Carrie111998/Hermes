"""Fail-closed regressions for the pre-mutation quiesce phase.

Every test here pins a way the update could previously have mutated the
shared checkout while a live runtime kept importing from it:

1. a swallowed collector failure that turns undiscovered runtimes into
   "fewer/zero rows" — an inventory that fails OPEN;
2. a Desktop-supervised backend stopped by PID, which its supervisor
   respawns onto pre-update code inside the mutation window;
3. a durable restart-pending write that failed, leaving no recovery
   authority for an updater killed mid-phase;
4. a runtime SHA "verification" satisfied by the on-disk checkout SHA
   rather than by the replacement interpreter;
5. a stop/escalation signalling an inventoried PID that the kernel has
   since recycled onto an unrelated process.

No live gateway, no network: probes and stops are injected.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import hermes_cli.update_inventory as ui
from hermes_cli import update_quiesce
from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan


def _write_state(home: Path, pid: int, sha: str | None = None):
    record: dict = {"pid": pid}
    if sha:
        record["code_sha"] = sha
    home.mkdir(parents=True, exist_ok=True)
    (home / "gateway_state.json").write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture()
def fleet(monkeypatch, tmp_path):
    """One live gateway on the default profile; every probe answers."""
    default_home = tmp_path / "home"
    _write_state(default_home, 100, sha="a" * 40)

    monkeypatch.setattr(
        "hermes_cli.profiles._get_default_hermes_home", lambda: default_home
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: default_home / "profiles"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._PROFILE_ID_RE",
        re.compile(r"^[a-z0-9][a-z0-9_-]*$"),
        raising=False,
    )
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == 100)
    monkeypatch.setattr(
        "hermes_cli.gateway._get_service_pids", lambda all_profiles=False: set()
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.find_profile_gateway_processes",
        lambda exclude_pids=None: [],
    )
    monkeypatch.setattr(
        "hermes_cli.gateway.find_windows_gateway_services", lambda: []
    )
    monkeypatch.setattr("hermes_cli.process_identity.ledger_entries", lambda **k: [])
    monkeypatch.setattr(
        "hermes_cli.build_info.get_code_identity",
        lambda refresh=False: {"sha": "a" * 40, "version": "1.0"},
    )
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda *a, **k: "git")
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_authorization():
    update_quiesce.reset_mutation_authorization()
    yield
    update_quiesce.reset_mutation_authorization()


def _boom(*args, **kwargs):
    raise RuntimeError("probe exploded")


# ---------------------------------------------------------------------------
# 1. Inventory must fail CLOSED on a collector failure
# ---------------------------------------------------------------------------


class TestInventoryFailsClosed:
    def test_clean_collection_records_no_discovery_errors(self, fleet):
        plan = ui.collect_runtime_inventory()
        assert [r.pid for r in plan.runtimes] == [100]
        assert plan.discovery_errors == []

    def test_pid_file_collector_failure_is_recorded(self, fleet, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.gateway.find_profile_gateway_processes", _boom
        )
        plan = ui.collect_runtime_inventory()
        assert plan.discovery_errors, "a failed collector must be recorded"
        assert any("pid-file" in e.lower() for e in plan.discovery_errors)

    def test_ledger_collector_failure_is_recorded(self, fleet, monkeypatch):
        monkeypatch.setattr("hermes_cli.process_identity.ledger_entries", _boom)
        plan = ui.collect_runtime_inventory()
        assert any("ledger" in e.lower() for e in plan.discovery_errors)

    def test_profile_enumeration_failure_is_recorded(self, fleet, monkeypatch):
        monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", _boom)
        plan = ui.collect_runtime_inventory()
        assert any("profile" in e.lower() for e in plan.discovery_errors)

    def test_service_pid_probe_failure_is_recorded(self, fleet, monkeypatch):
        monkeypatch.setattr("hermes_cli.gateway._get_service_pids", _boom)
        plan = ui.collect_runtime_inventory()
        assert any("service-pid" in e.lower() for e in plan.discovery_errors)

    def test_verify_rejects_a_plan_with_discovery_errors(self):
        plan = UpdatePlan()
        plan.discovery_errors = ["pid-file gateway inventory: probe exploded"]
        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            update_quiesce.verify_inventory_complete(plan)
        assert "probe exploded" in str(excinfo.value)

    def test_verify_still_accepts_a_positively_empty_fleet(self):
        """An empty fleet is legitimate — but only with every probe answered."""
        assert update_quiesce.verify_inventory_complete(UpdatePlan()) == []

    def test_quiesce_aborts_before_any_stop_when_a_collector_failed(self):
        plan = UpdatePlan()
        plan.runtimes = [
            RuntimeRecord(
                kind="gateway",
                profile="default",
                pid=100,
                supervisor="systemd",
                unit="hermes-gateway.service",
                unit_scope="user",
            )
        ]
        plan.discovery_errors = ["serve/dashboard ledger inventory: probe exploded"]
        stopped: list = []
        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                plan,
                stop_runtime=lambda r: stopped.append(r.pid) or True,
                pid_alive=lambda pid: False,
                assess_isolation=lambda _plan: update_quiesce.IsolationResult(
                    isolated=True
                ),
                persist_state=False,
            )
        assert stopped == [], "no runtime may be stopped on an unproven inventory"
        assert update_quiesce.authorized_report() is None


# ---------------------------------------------------------------------------
# 2. A Desktop-supervised backend must not be respawnable during mutation
# ---------------------------------------------------------------------------


def _desktop_runtime(pid=4242, spawner_pid=999, spawner_create=111.0):
    detail = {"argv": "hermes serve --host 0.0.0.0"}
    if spawner_pid is not None:
        detail["spawner_pid"] = spawner_pid
        detail["spawner_create"] = spawner_create
    return RuntimeRecord(
        kind="serve",
        profile="default",
        pid=pid,
        supervisor="desktop",
        restart_via="desktop",
        detail=detail,
    )


class TestDesktopSupervisedStop:
    def test_inventory_records_the_desktop_spawner_identity(self, fleet, monkeypatch):
        """Without the spawner's identity the stop phase cannot revalidate it."""
        entry = {
            "purpose": "serve",
            "pid": 777,
            "profile": "default",
            "argv": "hermes serve",
            "spawner_pid": 999,
            "spawner_create": 111.0,
        }
        monkeypatch.setattr(
            "hermes_cli.process_identity.ledger_entries", lambda **k: [entry]
        )
        monkeypatch.setattr(
            "hermes_cli.process_identity.spawner_is_dead", lambda e: False
        )
        plan = ui.collect_runtime_inventory()
        serve = next(r for r in plan.runtimes if r.kind == "serve")
        assert serve.supervisor == "desktop"
        assert serve.detail["spawner_pid"] == 999
        assert serve.detail["spawner_create"] == 111.0

    def test_relaunch_authority_for_desktop_is_unchanged(self):
        """Closing the respawn window must not move the relaunch authority."""
        with_argv = _desktop_runtime()
        assert update_quiesce.relaunch_authority(with_argv) == "argv"
        without_argv = _desktop_runtime()
        without_argv.detail.pop("argv")
        assert update_quiesce.relaunch_authority(without_argv) == "desktop"

    def test_a_live_desktop_supervisor_blocks_the_stop(self, monkeypatch):
        from hermes_cli import update_cmd

        terminated: list = []
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: terminated.append(pid),
        )
        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: True,  # the Desktop app is still running
        )

        with pytest.raises(Exception) as excinfo:
            update_cmd._stop_runtime_for_quiesce(_desktop_runtime())
        assert "desktop" in str(excinfo.value).lower()
        assert terminated == [], (
            "killing a Desktop-supervised backend lets Desktop respawn it "
            "onto pre-update code inside the mutation window"
        )

    def test_an_unprovable_desktop_supervisor_blocks_the_stop(self, monkeypatch):
        from hermes_cli import update_cmd

        terminated: list = []
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: terminated.append(pid),
        )
        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: None,  # psutil cannot say
        )

        with pytest.raises(Exception):
            update_cmd._stop_runtime_for_quiesce(_desktop_runtime())
        assert terminated == []

    def test_a_dead_desktop_supervisor_permits_the_pid_stop(self, monkeypatch):
        from hermes_cli import update_cmd

        terminated: list = []
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: terminated.append(pid),
        )
        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: False,  # Desktop is gone; nothing will respawn
        )
        # The PID-reuse guard is exercised separately; neutralise it here so
        # this test isolates the Desktop-supervisor question.
        monkeypatch.setattr(
            update_cmd,
            "_runtime_identity_still_matches",
            lambda runtime: True,
            raising=False,
        )

        assert update_cmd._stop_runtime_for_quiesce(_desktop_runtime()) is True
        assert terminated == [4242]

    def test_quiesce_aborts_rather_than_mutating_past_a_live_desktop(
        self, monkeypatch
    ):
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            "gateway.status.terminate_pid", lambda pid, force=False: None
        )
        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: True,
        )
        plan = UpdatePlan()
        plan.runtimes = [_desktop_runtime()]

        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                plan,
                stop_runtime=update_cmd._stop_runtime_for_quiesce,
                pid_alive=lambda pid: False,
                assess_isolation=lambda _p: update_quiesce.IsolationResult(
                    isolated=True
                ),
                persist_state=False,
            )
        assert update_quiesce.authorized_report() is None


# ---------------------------------------------------------------------------
# 3. No mutation without durable recovery authority
# ---------------------------------------------------------------------------


class TestRestartPendingStateIsMandatory:
    @pytest.fixture(autouse=True)
    def _clean_state(self):
        update_quiesce.clear_restart_pending_state()
        yield
        update_quiesce.clear_restart_pending_state()

    def _plan(self):
        plan = UpdatePlan()
        plan.runtimes = [
            RuntimeRecord(
                kind="gateway",
                profile="default",
                pid=4242,
                supervisor="systemd",
                restart_via="systemd",
                unit="hermes-gateway.service",
                unit_scope="user",
            )
        ]
        return plan

    def test_a_failed_durable_write_aborts_before_the_first_stop(
        self, monkeypatch
    ):
        """Stopping the fleet with no record on disk is unrecoverable."""
        monkeypatch.setattr(
            update_quiesce,
            "write_restart_pending_state",
            lambda runtimes, expected_sha="", merge=True: False,
        )
        stopped: list = []

        with pytest.raises(update_quiesce.QuiesceAbort) as excinfo:
            update_quiesce.run_pre_mutation_quiesce(
                self._plan(),
                stop_runtime=lambda r: stopped.append(r.pid) or True,
                pid_alive=lambda pid: False,
                assess_isolation=lambda _p: update_quiesce.IsolationResult(
                    isolated=True
                ),
                expected_sha="a" * 40,
            )

        message = str(excinfo.value).lower()
        assert "restart" in message and "record" in message
        assert stopped == [], (
            "a runtime stopped without a durable record can never be restored"
        )
        assert update_quiesce.authorized_report() is None

    def test_a_durable_write_that_raises_also_aborts(self, monkeypatch):
        monkeypatch.setattr(
            update_quiesce, "write_restart_pending_state", _boom
        )
        stopped: list = []

        with pytest.raises(update_quiesce.QuiesceAbort):
            update_quiesce.run_pre_mutation_quiesce(
                self._plan(),
                stop_runtime=lambda r: stopped.append(r.pid) or True,
                pid_alive=lambda pid: False,
                assess_isolation=lambda _p: update_quiesce.IsolationResult(
                    isolated=True
                ),
            )
        assert stopped == []
        assert update_quiesce.authorized_report() is None

    def test_a_successful_durable_write_authorizes_mutation(self):
        report = update_quiesce.run_pre_mutation_quiesce(
            self._plan(),
            stop_runtime=lambda r: True,
            pid_alive=lambda pid: False,
            assess_isolation=lambda _p: update_quiesce.IsolationResult(isolated=True),
            expected_sha="a" * 40,
        )
        assert report.quiesced_pids == [4242]
        state = update_quiesce.read_restart_pending_state()
        assert state is not None
        assert state["runtimes"][0]["unit"] == "hermes-gateway.service"

    def test_a_failed_discharge_write_is_reported_and_keeps_the_record(
        self, monkeypatch
    ):
        """A shrink that did not land must not be mistaken for a discharge."""
        update_quiesce.write_restart_pending_state(
            self._plan().runtimes, expected_sha="a" * 40
        )
        monkeypatch.setattr(
            update_quiesce,
            "write_restart_pending_state",
            lambda runtimes, expected_sha="", merge=True: False,
        )
        state = update_quiesce.read_restart_pending_state()

        assert update_quiesce.discharge_relaunched_records(state, []) is False
        assert update_quiesce.read_restart_pending_state() is not None


# ---------------------------------------------------------------------------
# 4. Runtime SHA verification must come from the REPLACEMENT runtime
# ---------------------------------------------------------------------------


class TestRuntimeShaVerification:
    """The on-disk checkout SHA proves nothing about a live interpreter.

    A replacement that never came up, came up on a stale venv, or died on
    import leaves the checkout at the new SHA regardless — so answering
    the probe with the checkout SHA turns "verified" into "we updated the
    files", which is the claim the verification exists to test.
    """

    @pytest.fixture()
    def profile_home(self, monkeypatch, tmp_path):
        home = tmp_path / "profile"
        home.mkdir()
        monkeypatch.setattr(
            "hermes_cli.profiles.get_profile_dir", lambda profile: home
        )
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            update_cmd, "_current_checkout_sha", lambda: "c" * 40
        )
        return home

    def _probe(self, record, new_pid=None):
        from hermes_cli import update_cmd

        return update_cmd._probe_relaunched_runtime_sha(
            record, new_pid, timeout=0.2, poll_interval=0.02
        )

    def test_a_runtime_publishing_no_stamp_does_not_verify(self, profile_home):
        assert self._probe({"kind": "gateway", "profile": "default"}) is None

    def test_a_failed_probe_does_not_verify(self, profile_home, monkeypatch):
        monkeypatch.setattr("gateway.status.read_runtime_status", _boom)
        assert self._probe({"kind": "gateway", "profile": "default"}) is None

    def test_the_replacements_own_stamp_is_what_verifies(
        self, profile_home, monkeypatch
    ):
        from hermes_cli import update_cmd

        # The stamp has to be the REPLACEMENT's, so the probe checks the PID
        # in it against the process this relaunch started.
        monkeypatch.setattr(update_cmd, "_runtime_pid_alive", lambda pid: True)
        monkeypatch.setattr("hermes_cli.main._runtime_pid_alive", lambda pid: True)
        (profile_home / "gateway_state.json").write_text(
            json.dumps({"pid": 5, "code_sha": "b" * 40}), encoding="utf-8"
        )
        assert (
            self._probe({"kind": "gateway", "profile": "default", "pid": 4}, 5)
            == "b" * 40
        )

    def test_an_unverified_relaunch_is_not_complete(self):
        state = {
            "expected_sha": "b" * 40,
            "runtimes": [
                {
                    "kind": "gateway",
                    "profile": "default",
                    "pid": 4242,
                    "unit": "hermes-gateway.service",
                    "unit_scope": "user",
                }
            ],
        }
        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: None,
            pid_alive=lambda pid: False,
            probe_sha=lambda record, _new_pid=None: None,  # replacement publishes nothing
        )
        assert outcomes[0].relaunched is True
        assert outcomes[0].old_pid_gone is True
        assert outcomes[0].sha_matches is False
        assert update_quiesce.relaunch_is_complete(outcomes) is False


# ---------------------------------------------------------------------------
# 5. Never signal a PID the kernel has recycled
# ---------------------------------------------------------------------------


def _pid_runtime(pid=4242, start_time=111.0):
    detail: dict = {"argv": "hermes gateway run"}
    if start_time is not None:
        detail["start_time"] = start_time
    return RuntimeRecord(
        kind="gateway",
        profile="default",
        pid=pid,
        supervisor="manual",
        restart_via="manual",
        detail=detail,
    )


class TestPidReuseSafety:
    """An inventoried PID is only a name; ``(pid, start_time)`` is identity.

    Between the inventory pass and the stop the kernel may have reaped the
    runtime and handed its PID to something else entirely — a shell, a
    build, the user's editor. Signalling it then kills a bystander and
    still leaves the real fleet running into the mutation.
    """

    @pytest.fixture()
    def terminated(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            "gateway.status.terminate_pid",
            lambda pid, force=False: calls.append((pid, force)),
        )
        return calls

    def test_inventory_records_each_runtimes_start_time(self, fleet, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.update_inventory._process_start_time",
            lambda pid: 222.0,
        )
        plan = ui.collect_runtime_inventory()
        assert plan.runtimes[0].detail["start_time"] == 222.0

    def test_a_matching_identity_is_signalled(self, terminated, monkeypatch):
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: True,
        )
        assert update_cmd._stop_runtime_for_quiesce(_pid_runtime()) is True
        assert terminated == [(4242, False)]

    def test_a_recycled_pid_is_never_signalled(self, terminated, monkeypatch):
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: False,  # different process behind that PID
        )
        with pytest.raises(Exception):
            update_cmd._stop_runtime_for_quiesce(_pid_runtime())
        assert terminated == []

    def test_an_unprovable_identity_is_never_signalled(self, terminated, monkeypatch):
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: None,  # psutil cannot say
        )
        with pytest.raises(Exception):
            update_cmd._stop_runtime_for_quiesce(_pid_runtime())
        assert terminated == []

    def test_a_runtime_with_no_recorded_identity_is_never_signalled(
        self, terminated
    ):
        from hermes_cli import update_cmd

        with pytest.raises(Exception):
            update_cmd._stop_runtime_for_quiesce(_pid_runtime(start_time=None))
        assert terminated == []

    def test_escalation_revalidates_before_force_killing(
        self, terminated, monkeypatch
    ):
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: False,
        )
        update_cmd._escalate_runtime_stop(_pid_runtime())
        assert terminated == [], "SIGKILL on a reused PID kills a bystander"

    def test_escalation_still_force_kills_the_real_runtime(
        self, terminated, monkeypatch
    ):
        from hermes_cli import update_cmd

        monkeypatch.setattr(
            "hermes_cli.process_identity._pid_alive_matches",
            lambda pid, create: True,
        )
        update_cmd._escalate_runtime_stop(_pid_runtime())
        assert terminated == [(4242, True)]

    def test_a_supervised_stop_needs_no_pid_identity(self, terminated, monkeypatch):
        """The unit IS the authority — no PID is signalled, so none is checked."""
        from hermes_cli import update_cmd

        issued: list = []
        monkeypatch.setattr(
            update_cmd,
            "_run_supervisor_command",
            lambda argv: (issued.append(list(argv)) or True),
        )
        runtime = RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=4242,
            supervisor="systemd",
            unit="hermes-gateway.service",
            unit_scope="user",
        )
        assert update_cmd._stop_runtime_for_quiesce(runtime) is True
        assert issued and terminated == []
