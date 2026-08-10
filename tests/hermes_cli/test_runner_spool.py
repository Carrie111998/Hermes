from pathlib import Path
import sqlite3

import pytest

from hermes_cli.runner_protocol import RunnerCommand, RunnerEvent
from hermes_cli.runner_spool import RunnerSpool


def test_binding_path_stays_device_local_and_survives_restart(tmp_path):
    database = tmp_path / "runner.db"
    root = tmp_path / "repo"
    root.mkdir()

    spool = RunnerSpool(database)
    binding = spool.register_binding(project_id="project-1", root_path=root, label="Repo")

    assert "root_path" not in binding.public_dict()
    assert binding.public_dict() == {
        "binding_id": binding.binding_id,
        "label": "Repo",
        "project_id": "project-1",
        "revoked": False,
    }
    assert spool.resolve_binding(binding.binding_id) == root.resolve()
    spool.close()

    reopened = RunnerSpool(database)
    assert reopened.resolve_binding(binding.binding_id) == root.resolve()


def test_lease_fencing_expiry_and_head_change(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    spool = RunnerSpool(tmp_path / "runner.db")
    binding = spool.register_binding(project_id="project-1", root_path=root, label="Repo")

    first = spool.acquire_lease(
        binding_id=binding.binding_id,
        owner="run-1",
        ttl_seconds=10,
        expected_head="head-a",
        now=100,
    )
    assert first.fencing_token == 1

    with pytest.raises(ValueError, match="leased"):
        spool.acquire_lease(
            binding_id=binding.binding_id,
            owner="run-2",
            ttl_seconds=10,
            expected_head="head-a",
            now=105,
        )

    second = spool.acquire_lease(
        binding_id=binding.binding_id,
        owner="run-2",
        ttl_seconds=10,
        expected_head="head-a",
        now=111,
    )
    assert second.fencing_token == 2

    with pytest.raises(ValueError, match="stale"):
        spool.validate_lease(
            binding_id=binding.binding_id,
            lease_id=first.lease_id,
            fencing_token=first.fencing_token,
            live_head="head-a",
            now=112,
        )

    with pytest.raises(ValueError, match="HEAD"):
        spool.validate_lease(
            binding_id=binding.binding_id,
            lease_id=second.lease_id,
            fencing_token=second.fencing_token,
            live_head="head-b",
            now=112,
        )


def test_commands_are_idempotent_and_results_are_replayable(tmp_path):
    spool = RunnerSpool(tmp_path / "runner.db")
    command = RunnerCommand.create(
        attempt_id="attempt-1",
        method="fs.read",
        run_id="run-1",
        binding_id="binding-1",
        params={"path": "README.md"},
        lease_id="lease-1",
        fencing_token=1,
        command_id="command-1",
    )

    assert spool.begin_command(command) is True
    assert spool.begin_command(command) is False
    spool.complete_command("command-1", state="completed", result={"text": "hello"})

    stored = spool.command_result("command-1")
    assert stored == {"result": {"text": "hello"}, "state": "completed"}


def test_event_spool_reconciles_in_order_after_restart(tmp_path):
    database = tmp_path / "runner.db"
    spool = RunnerSpool(database)
    first = RunnerEvent.create(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=1,
        event_type="run.started",
        payload={},
        event_id="event-1",
    )
    second = RunnerEvent.create(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=2,
        event_type="run.output",
        payload={"chunk": "hello"},
        event_id="event-2",
    )

    assert spool.append_event(second) is True
    assert spool.append_event(first) is True
    assert spool.append_event(first) is False
    assert [event.sequence for event in spool.pending_events("attempt-1")] == [1, 2]

    spool.ack_events("attempt-1", through_sequence=1)
    spool.close()

    reopened = RunnerSpool(database)
    assert [event.sequence for event in reopened.pending_events("attempt-1")] == [2]
    reopened.close()


def test_restart_marks_unfinished_commands_uncertain_and_emits_replay_event(tmp_path):
    database = tmp_path / "runner.db"
    spool = RunnerSpool(database)
    binding = spool.register_binding(
        label="Repo",
        project_id="project-1",
        root_path=tmp_path,
    )
    command = RunnerCommand.create(
        attempt_id="attempt-uncertain",
        binding_id=binding.binding_id,
        command_id="command-uncertain",
        fencing_token=1,
        lease_id="lease-uncertain",
        method="fs.write",
        params={"path": "result.txt", "text": "value"},
        run_id="run-uncertain",
    )
    assert spool.begin_command(command) is True
    spool.close()

    reopened = RunnerSpool(database)
    reconciled = reopened.reconcile_incomplete_commands()

    assert reconciled == ["command-uncertain"]
    stored = reopened.command_result("command-uncertain")
    assert stored == {
        "result": {"error": "runner restarted before command completion", "uncertain": True},
        "state": "uncertain",
    }
    events = reopened.pending_events("attempt-uncertain")
    assert [event.event_type for event in events] == ["run.uncertain"]
    assert events[0].payload["command_id"] == "command-uncertain"
    reopened.close()


def test_terminal_result_and_event_commit_atomically(tmp_path):
    database = tmp_path / "runner.db"
    root = tmp_path / "repo"
    root.mkdir()
    spool = RunnerSpool(database)
    binding = spool.register_binding(project_id="project-1", root_path=root, label="Repo")
    command = RunnerCommand.create(
        attempt_id="attempt-atomic",
        binding_id=binding.binding_id,
        command_id="command-atomic",
        fencing_token=1,
        lease_id="lease-atomic",
        method="fs.write",
        params={"path": "result.txt", "text": "value"},
        run_id="run-atomic",
    )
    assert spool.begin_command(command) is True
    event = RunnerEvent.create(
        attempt_id=command.attempt_id,
        event_type="run.completed",
        payload={"command_id": command.command_id},
        run_id=command.run_id,
        sequence=1,
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER reject_terminal_event BEFORE INSERT ON events "
            "BEGIN SELECT RAISE(ABORT, 'terminal event rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="terminal event rejected"):
        spool.complete_command_with_event(
            command.command_id,
            state="completed",
            result={"ok": True},
            event=event,
        )
    assert spool.command_result(command.command_id) == {"result": None, "state": "accepted"}
    assert spool.pending_events(command.attempt_id) == []
