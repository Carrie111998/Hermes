"""A retry must not erase the runtimes the first attempt left down (#99450 R2-5).

``write_restart_pending_state`` replaced the durable record wholesale. The
second quiesce of an interrupted update therefore wrote only the runtimes
that were *still running* when it re-inventoried the fleet — and a runtime
the FIRST attempt stopped and never brought back is, by definition, not
running any more. Its relaunch obligation was silently dropped, leaving a
fleet member down with nothing on disk owed to it.

New obligations are now merged onto the undischarged ones, keyed by a stable
runtime identity (kind + profile + supervisor unit/scope + bind endpoint +
launch command) rather than by PID, which changes on every relaunch.
"""

from __future__ import annotations

import pytest

from hermes_cli import update_quiesce
from hermes_cli.update_inventory import RuntimeRecord


@pytest.fixture(autouse=True)
def _clean_state():
    update_quiesce.clear_restart_pending_state()
    yield
    update_quiesce.clear_restart_pending_state()


def _gateway(pid, *, unit="acme-gateway.service", profile="default"):
    return RuntimeRecord(
        kind="gateway",
        profile=profile,
        pid=pid,
        supervisor="systemd",
        restart_via="systemd",
        unit=unit,
        unit_scope="user",
    )


def _serve(pid, *, profile="edge", port=9119):
    return RuntimeRecord(
        kind="serve",
        profile=profile,
        pid=pid,
        supervisor="manual-serve",
        restart_via="respawn-argv",
        detail={
            "argv_list": ["hermes", "serve", "--port", str(port)],
            "host": "127.0.0.1",
            "port": port,
            "start_time": 1.0,
        },
    )


def _recorded():
    state = update_quiesce.read_restart_pending_state()
    return state["runtimes"] if state else []


def _ids(records):
    return sorted(update_quiesce.restart_record_identity(r) for r in records)


class TestTwoRunsAccumulate:
    def test_a_second_run_does_not_erase_the_first_runs_obligation(self):
        """Run 1 stops a gateway and dies. Run 2 re-inventories, sees only
        the serve that is still up, and must still owe the gateway."""
        assert update_quiesce.write_restart_pending_state(
            [_gateway(100)], expected_sha="a" * 40
        )
        assert update_quiesce.write_restart_pending_state(
            [_serve(200)], expected_sha="b" * 40
        )

        records = _recorded()
        assert len(records) == 2, records
        assert {r["kind"] for r in records} == {"gateway", "serve"}
        assert {r["pid"] for r in records} == {100, 200}
        # The retry's expectation is the current one.
        assert update_quiesce.read_restart_pending_state()["expected_sha"] == "b" * 40

    def test_the_same_runtime_is_updated_in_place_not_duplicated(self):
        """A runtime that came back and was re-inventoried under a NEW pid is
        the same obligation, not a second one."""
        assert update_quiesce.write_restart_pending_state([_gateway(100)])
        assert update_quiesce.write_restart_pending_state([_gateway(4242)])

        records = _recorded()
        assert len(records) == 1, records
        assert records[0]["pid"] == 4242

    def test_distinct_endpoints_on_one_profile_stay_distinct(self):
        """Two manual serves on the same profile are two obligations."""
        assert update_quiesce.write_restart_pending_state([_serve(200, port=9119)])
        assert update_quiesce.write_restart_pending_state([_serve(201, port=9200)])

        records = _recorded()
        assert len(records) == 2, records
        assert sorted(r["pid"] for r in records) == [200, 201]

    def test_three_runs_keep_every_still_down_runtime(self):
        for record in (_gateway(100), _serve(200), _gateway(300, unit="b.service")):
            assert update_quiesce.write_restart_pending_state([record])
        assert len(_recorded()) == 3, _recorded()


class TestDischargeStillShrinks:
    def test_a_relaunched_runtime_is_removed_not_merged_back(self):
        """The merge must not defeat the discharge — otherwise the record
        never empties and the backstop respawns duplicates forever."""
        gateway, serve = _gateway(100), _serve(200)
        assert update_quiesce.write_restart_pending_state(
            [gateway, serve], expected_sha="a" * 40
        )
        state = update_quiesce.read_restart_pending_state()

        outcomes = [
            update_quiesce.RelaunchOutcome(
                kind="serve",
                profile="edge",
                old_pid=200,
                mechanism="argv",
                relaunched=True,
            )
        ]
        assert update_quiesce.discharge_relaunched_records(state, outcomes) is True

        records = _recorded()
        assert [r["kind"] for r in records] == ["gateway"], records

    def test_a_fully_discharged_record_is_cleared(self):
        serve = _serve(200)
        assert update_quiesce.write_restart_pending_state([serve])
        state = update_quiesce.read_restart_pending_state()
        outcomes = [
            update_quiesce.RelaunchOutcome(
                kind="serve",
                profile="edge",
                old_pid=200,
                mechanism="argv",
                relaunched=True,
            )
        ]
        assert update_quiesce.discharge_relaunched_records(state, outcomes) is True
        assert update_quiesce.read_restart_pending_state() is None


class TestIdentityIsStableAcrossRepresentations:
    def test_a_record_and_its_dict_share_an_identity(self):
        serve = _serve(200)
        assert update_quiesce.write_restart_pending_state([serve])
        (as_dict,) = _recorded()
        assert update_quiesce.restart_record_identity(
            serve
        ) == update_quiesce.restart_record_identity(as_dict)

    def test_identity_ignores_the_pid(self):
        assert update_quiesce.restart_record_identity(
            _gateway(1)
        ) == update_quiesce.restart_record_identity(_gateway(2))

    def test_identity_separates_kinds_and_profiles(self):
        assert len(_ids([_gateway(1), _serve(2), _gateway(3, profile="other")])) == 3
