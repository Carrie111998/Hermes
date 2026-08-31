"""Durable restart-pending state: an interrupted update must be resumable.

Once the fleet is quiesced, the update owes every recorded runtime a
relaunch.  If the updater dies between quiesce and relaunch (SIGKILL,
power loss, a supervisor reaping its cgroup), the ONLY way a retry can
restore the fleet is a record on disk written BEFORE the mutation — the
processes it describes are already gone, so nothing can be re-derived.

The record has to carry the exact relaunch authority (unit/label or
argv), not just a profile name.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import update_quiesce
from hermes_cli.update_inventory import RuntimeRecord
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _clean_state():
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.clear_restart_pending_state()


def _records():
    return [
        RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=4242,
            supervisor="systemd",
            restart_via="systemd",
            unit="hermes-gateway.service",
            unit_scope="user",
        ),
        RuntimeRecord(
            kind="dashboard",
            profile="zeus",
            pid=4300,
            supervisor="manual-serve",
            restart_via="respawn-argv",
            detail={"argv": "hermes -p zeus dashboard --port 8765"},
        ),
    ]


class TestDurableState:
    def test_state_round_trips_every_relaunch_authority(self):
        update_quiesce.write_restart_pending_state(
            _records(), expected_sha="deadbeef"
        )
        state = update_quiesce.read_restart_pending_state()

        assert state is not None
        assert state["expected_sha"] == "deadbeef"
        by_pid = {r["pid"]: r for r in state["runtimes"]}
        assert by_pid[4242]["unit"] == "hermes-gateway.service"
        assert by_pid[4242]["unit_scope"] == "user"
        assert by_pid[4300]["detail"]["argv"] == (
            "hermes -p zeus dashboard --port 8765"
        )

    def test_state_is_written_before_it_is_readable(self):
        """Atomic publish: a reader never sees a half-written record."""
        update_quiesce.write_restart_pending_state(_records(), expected_sha="abc")
        path = update_quiesce.restart_pending_state_path()
        assert path.is_file()
        json.loads(path.read_text(encoding="utf-8"))

    def test_state_lives_under_hermes_home(self):
        update_quiesce.write_restart_pending_state(_records(), expected_sha="abc")
        assert update_quiesce.restart_pending_state_path().parent == get_hermes_home()

    def test_clear_removes_the_state(self):
        update_quiesce.write_restart_pending_state(_records(), expected_sha="abc")
        update_quiesce.clear_restart_pending_state()
        assert update_quiesce.read_restart_pending_state() is None

    def test_corrupt_state_reads_as_absent(self):
        update_quiesce.write_restart_pending_state(_records(), expected_sha="abc")
        update_quiesce.restart_pending_state_path().write_text(
            "{not json", encoding="utf-8"
        )
        assert update_quiesce.read_restart_pending_state() is None

    def test_quiesce_persists_state_before_authorizing_mutation(self):
        """The record must exist by the time mutation is allowed."""
        update_quiesce.reset_mutation_authorization()
        plan_runtimes = _records()

        class _Plan:
            runtimes = plan_runtimes

        seen_state_at_stop: list = []

        def _stop(runtime):
            seen_state_at_stop.append(update_quiesce.read_restart_pending_state())
            return True

        update_quiesce.run_pre_mutation_quiesce(
            _Plan(),
            stop_runtime=_stop,
            pid_alive=lambda pid: False,
            assess_isolation=lambda plan: update_quiesce.IsolationResult(
                isolated=True, reason="test"
            ),
            expected_sha="cafe",
        )
        update_quiesce.assert_mutation_authorized("git")

        # Written before the FIRST stop: an interrupt anywhere from the
        # first SIGTERM onward is recoverable.
        assert seen_state_at_stop[0] is not None
        assert seen_state_at_stop[0]["expected_sha"] == "cafe"
        update_quiesce.reset_mutation_authorization()


class TestRelaunchFromRecordedState:
    def test_every_recorded_runtime_is_relaunched_exactly_once(self):
        state = {"expected_sha": "newsha", "runtimes": [
            r.to_dict() for r in _records()
        ]}
        restarted_units: list[str] = []
        respawned: list[str] = []

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: (restarted_units.append(unit) or True),
            respawn_argv=lambda argv, record: (respawned.append(argv) or 5555),
            pid_alive=lambda pid: False,
            probe_sha=lambda record, _new_pid=None: "newsha",
        )

        assert restarted_units == ["hermes-gateway.service"]
        assert respawned == ["hermes -p zeus dashboard --port 8765"]
        assert [o.relaunched for o in outcomes] == [True, True]
        assert all(o.old_pid_gone for o in outcomes)
        assert {o.code_sha for o in outcomes} == {"newsha"}
        assert all(o.sha_matches for o in outcomes)

    def test_a_surviving_old_pid_is_reported_not_gone(self):
        state = {"expected_sha": "newsha", "runtimes": [
            _records()[0].to_dict()
        ]}
        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: None,
            pid_alive=lambda pid: pid == 4242,
            probe_sha=lambda record, _new_pid=None: "newsha",
        )
        assert outcomes[0].old_pid_gone is False
        assert update_quiesce.relaunch_is_complete(outcomes) is False

    def test_a_stale_sha_is_reported(self):
        state = {"expected_sha": "newsha", "runtimes": [
            _records()[0].to_dict()
        ]}
        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: None,
            pid_alive=lambda pid: False,
            probe_sha=lambda record, _new_pid=None: "oldsha",
        )
        assert outcomes[0].sha_matches is False
        assert update_quiesce.relaunch_is_complete(outcomes) is False

    def test_a_failed_unit_restart_is_reported(self):
        state = {"expected_sha": "newsha", "runtimes": [
            _records()[0].to_dict()
        ]}
        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: False,
            respawn_argv=lambda argv, record: None,
            pid_alive=lambda pid: False,
            probe_sha=lambda record, _new_pid=None: "newsha",
        )
        assert outcomes[0].relaunched is False
        assert update_quiesce.relaunch_is_complete(outcomes) is False

    def test_a_surviving_argv_runtime_is_not_respawned_as_a_duplicate(self):
        """The abort-then-restore path: a stop that failed partway leaves
        a runtime alive, and respawning it would put two processes on the
        same profile/port."""
        state = {"expected_sha": "newsha", "runtimes": [_records()[1].to_dict()]}
        respawned: list = []

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: respawned.append(argv) or 5555,
            pid_alive=lambda pid: pid == 4300,
            probe_sha=lambda record, _new_pid=None: "newsha",
        )

        assert respawned == []
        assert outcomes[0].relaunched is False
        assert outcomes[0].old_pid_gone is False
        assert "still running" in outcomes[0].error

    def test_a_desktop_owned_backend_needs_no_relaunch(self):
        """The Desktop app respawns its own backend, so the obligation is
        discharged without the updater launching anything."""
        record = RuntimeRecord(
            kind="gateway",
            profile="default",
            pid=4400,
            supervisor="desktop",
            restart_via="desktop",
        )
        state = {"expected_sha": "newsha", "runtimes": [record.to_dict()]}

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: False,
            respawn_argv=lambda argv, rec: None,
            pid_alive=lambda pid: False,
            probe_sha=lambda rec, _new_pid=None: "newsha",
        )

        assert outcomes[0].mechanism == "desktop"
        assert update_quiesce.relaunch_is_complete(outcomes) is True

    def test_complete_relaunch_reports_complete(self):
        state = {"expected_sha": "newsha", "runtimes": [
            r.to_dict() for r in _records()
        ]}
        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: 6000,
            pid_alive=lambda pid: False,
            probe_sha=lambda record, _new_pid=None: "newsha",
        )
        assert update_quiesce.relaunch_is_complete(outcomes) is True


class TestDischargingRelaunchedRecords:
    """The relaunch runs twice per update: once in the restart phase, once
    from ``cmd_update``'s command-boundary backstop.  A respawned argv
    runtime must not be acted on twice — that is a second process on the
    same profile/port.  A supervised unit is idempotent and stays owed."""

    def test_a_respawned_argv_record_is_dropped_even_when_its_sha_is_stale(self):
        update_quiesce.write_restart_pending_state(
            [_records()[1]], expected_sha="newsha"
        )
        state = update_quiesce.read_restart_pending_state()

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: 6000,
            pid_alive=lambda pid: False,
            # A replacement that has not written its state file yet still
            # reports the pre-update SHA, so the run is "incomplete".
            probe_sha=lambda record, _new_pid=None: "oldsha",
        )
        assert update_quiesce.relaunch_is_complete(outcomes) is False

        update_quiesce.discharge_relaunched_records(state, outcomes)
        assert update_quiesce.read_restart_pending_state() is None

    def test_an_idempotent_unit_restart_stays_owed(self):
        """`systemctl restart` twice is a restart, not a duplicate, so a
        stale-SHA unit record must survive for a later pass to retry."""
        update_quiesce.write_restart_pending_state(
            [_records()[0]], expected_sha="newsha"
        )
        state = update_quiesce.read_restart_pending_state()

        outcomes = update_quiesce.relaunch_recorded_runtimes(
            state,
            restart_unit=lambda unit, scope: True,
            respawn_argv=lambda argv, record: None,
            pid_alive=lambda pid: False,
            probe_sha=lambda record, _new_pid=None: "oldsha",
        )

        update_quiesce.discharge_relaunched_records(state, outcomes)
        remaining = update_quiesce.read_restart_pending_state()

        assert remaining is not None
        assert [r["unit"] for r in remaining["runtimes"]] == [
            "hermes-gateway.service"
        ]
        assert remaining["expected_sha"] == "newsha"

    def test_a_second_relaunch_pass_does_not_respawn_a_duplicate(self):
        """End-to-end shape of the bug: pass one relaunches with a stale
        SHA, pass two reads what is left and must respawn nothing."""
        update_quiesce.write_restart_pending_state(
            [_records()[1]], expected_sha="newsha"
        )
        respawned: list = []

        def _pass():
            state = update_quiesce.read_restart_pending_state()
            if not state or not state.get("runtimes"):
                return []
            outcomes = update_quiesce.relaunch_recorded_runtimes(
                state,
                restart_unit=lambda unit, scope: True,
                respawn_argv=lambda argv, record: (
                    respawned.append(argv) or 6000
                ),
                pid_alive=lambda pid: False,
                probe_sha=lambda record, _new_pid=None: "oldsha",
            )
            if update_quiesce.relaunch_is_complete(outcomes):
                update_quiesce.clear_restart_pending_state()
            else:
                update_quiesce.discharge_relaunched_records(state, outcomes)
            return outcomes

        assert len(_pass()) == 1
        assert _pass() == []
        assert len(respawned) == 1
