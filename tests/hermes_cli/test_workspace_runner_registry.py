import sqlite3
import stat

import pytest

from hermes_cli import workspace_runner_registry as registry_module
from hermes_cli.workspace_runner_registry import WorkspaceRunnerRegistry


def test_enrollment_is_one_time_and_device_credentials_are_not_plaintext(tmp_path):
    database = tmp_path / "control.db"
    registry = WorkspaceRunnerRegistry(database, master_key_path=tmp_path / "master.key")
    enrollment = registry.create_enrollment("Studio Mac", now=100, ttl_seconds=60)

    credentials = registry.consume_enrollment(
        enrollment.runner_id,
        enrollment.enrollment_token,
        now=110,
    )

    assert registry.authenticate(credentials.runner_id, credentials.device_token) is True
    with pytest.raises(ValueError, match="invalid or expired"):
        registry.consume_enrollment(
            enrollment.runner_id,
            enrollment.enrollment_token,
            now=111,
        )
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    raw = database.read_bytes()
    assert enrollment.enrollment_token.encode() not in raw
    assert credentials.device_token.encode() not in raw
    assert credentials.command_key not in raw
    registry.close()


def test_rotation_revocation_and_public_bindings_are_fail_closed(tmp_path):
    registry = WorkspaceRunnerRegistry(
        tmp_path / "control.db",
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Laptop", now=100, ttl_seconds=60)
    credentials = registry.consume_enrollment(
        enrollment.runner_id,
        enrollment.enrollment_token,
        now=101,
    )

    registry.sync_bindings(
        credentials.runner_id,
        [
            {
                "binding_id": "binding-1",
                "label": "Launch",
                "project_id": "project-1",
                "revoked": False,
            }
        ],
    )
    registry.sync_capabilities(credentials.runner_id, ["worker.codex", "worker.codex"])
    public = registry.list_runners()
    assert public[0]["capabilities"] == ["worker.codex"]
    assert public[0]["bindings"] == [
        {
            "binding_id": "binding-1",
            "label": "Launch",
            "project_id": "project-1",
            "status": "online",
        }
    ]
    assert "root_path" not in repr(public)

    rotated = registry.rotate_credentials(credentials.runner_id)
    assert registry.authenticate(credentials.runner_id, credentials.device_token) is False
    assert registry.authenticate(rotated.runner_id, rotated.device_token) is True

    registry.queue_command(
        rotated.runner_id,
        "command-revoked",
        {"command_id": "command-revoked", "envelope": {"payload": {"method": "fs.read"}}},
        now=102,
    )
    registry.mark_command_sent("command-revoked", now=103)
    registry.acknowledge_command(
        rotated.runner_id,
        "command-revoked",
        ack_state="accepted",
        now=104,
    )
    registry.revoke_runner(rotated.runner_id)
    assert registry.command_status(rotated.runner_id, "command-revoked")["state"] == "revoked"
    assert registry.authenticate(rotated.runner_id, rotated.device_token) is False
    assert registry.list_runners()[0]["revoked"] is True
    registry.close()


def test_binding_sync_rejects_local_paths(tmp_path):
    registry = WorkspaceRunnerRegistry(
        tmp_path / "control.db",
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Laptop", now=100, ttl_seconds=60)
    credentials = registry.consume_enrollment(
        enrollment.runner_id,
        enrollment.enrollment_token,
        now=101,
    )

    with pytest.raises(ValueError, match="path"):
        registry.sync_bindings(
            credentials.runner_id,
            [
                {
                    "binding_id": "binding-1",
                    "label": "Launch",
                    "project_id": "project-1",
                    "root_path": "/secret/repo",
                }
            ],
        )
    registry.close()


def test_remote_command_queue_replays_until_signed_result_is_persisted(tmp_path):
    database = tmp_path / "control.db"
    registry = WorkspaceRunnerRegistry(
        database,
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Laptop", now=100, ttl_seconds=60)
    credentials = registry.consume_enrollment(
        enrollment.runner_id,
        enrollment.enrollment_token,
        now=101,
    )
    frame = {
        "command_id": "command-1",
        "envelope": {
            "payload": {
                "binding_id": "binding-1",
                "method": "fs.write",
                "params": {"path": "notes/result.txt", "text": "ok"},
            },
            "signature": "a" * 64,
        },
        "type": "command",
    }

    registry.queue_command(credentials.runner_id, "command-1", frame, now=102)
    registry.mark_command_dispatched("command-1", now=103)
    assert registry.pending_commands(credentials.runner_id) == [frame]

    registry.complete_command(
        credentials.runner_id,
        "command-1",
        result={"ok": True, "result": {"written": True}},
        now=104,
    )
    assert registry.pending_commands(credentials.runner_id) == []
    assert registry.command_status(credentials.runner_id, "command-1")["state"] == "completed"
    assert b"/secret/" not in database.read_bytes()
    registry.close()


def test_remote_command_id_cannot_be_reused_with_another_payload(tmp_path):
    registry = WorkspaceRunnerRegistry(
        tmp_path / "control.db",
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Laptop", now=100, ttl_seconds=60)
    credentials = registry.consume_enrollment(
        enrollment.runner_id,
        enrollment.enrollment_token,
        now=101,
    )
    first = {"command_id": "command-1", "envelope": {"payload": {}}, "type": "command"}
    registry.queue_command(credentials.runner_id, "command-1", first, now=102)

    with pytest.raises(ValueError, match="reused"):
        registry.queue_command(
            credentials.runner_id,
            "command-1",
            {**first, "envelope": {"payload": {"changed": True}}},
            now=103,
        )
    registry.close()


def test_replayed_inflight_command_is_persisted_as_uncertain(tmp_path):
    registry = WorkspaceRunnerRegistry(
        tmp_path / "control.db",
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Studio Mac", now=100)
    runner_id = enrollment.runner_id
    registry.consume_enrollment(runner_id, enrollment.enrollment_token, now=101)
    frame = {
        "command_id": "command-uncertain",
        "envelope": {"payload": {"path": "relative.txt"}, "signature": "signed"},
        "type": "command",
    }
    registry.queue_command(runner_id, "command-uncertain", frame, now=102)
    registry.mark_command_dispatched("command-uncertain", now=103)

    registry.complete_command(
        runner_id,
        "command-uncertain",
        result={
            "command_id": "command-uncertain",
            "ok": False,
            "result": {
                "error": "runner restarted before command completion",
                "ok": False,
                "replayed": True,
                "uncertain": True,
            },
            "state": "uncertain",
        },
        now=104,
    )

    assert registry.command_status(runner_id, "command-uncertain")["state"] == "uncertain"
    assert registry.pending_commands(runner_id) == []

    registry.begin_reconciliation(runner_id, "command-uncertain", decision="retry", now=105)
    assert registry.command_status(runner_id, "command-uncertain")["state"] == "reconciling"
    registry.finish_reconciliation(
        runner_id,
        "command-uncertain",
        outcome="resumed",
        replacement_command_id="command-replacement",
        now=106,
    )
    status = registry.command_status(runner_id, "command-uncertain")
    assert status["state"] == "resumed"
    assert status["reconciliation"] == {
        "decision": "retry",
        "outcome": "resumed",
        "replacement_command_id": "command-replacement",
        "updated_at": 106,
    }
    registry.close()


def test_uncertain_command_can_be_explicitly_abandoned_but_not_silently_reopened(tmp_path):
    registry = WorkspaceRunnerRegistry(
        tmp_path / "control.db",
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Studio Mac", now=100)
    runner_id = enrollment.runner_id
    registry.consume_enrollment(runner_id, enrollment.enrollment_token, now=101)
    frame = {"command_id": "command-1", "envelope": {"payload": {}}, "type": "command"}
    registry.queue_command(runner_id, "command-1", frame, now=102)
    registry.mark_command_dispatched("command-1", now=103)
    registry.complete_command(
        runner_id,
        "command-1",
        result={"command_id": "command-1", "ok": False, "uncertain": True},
        now=104,
    )

    registry.abandon_command(runner_id, "command-1", now=105)
    assert registry.command_status(runner_id, "command-1")["state"] == "abandoned"
    with pytest.raises(ValueError, match="uncertain"):
        registry.begin_reconciliation(runner_id, "command-1", decision="retry", now=106)
    registry.close()


def test_runner_registry_enforces_durable_payload_and_pending_quotas(tmp_path, monkeypatch):
    registry = WorkspaceRunnerRegistry(
        tmp_path / "control.db",
        master_key_path=tmp_path / "master.key",
    )
    enrollment = registry.create_enrollment("Laptop", now=100, ttl_seconds=60)
    credentials = registry.consume_enrollment(
        enrollment.runner_id,
        enrollment.enrollment_token,
        now=101,
    )
    frame = {"command_id": "command-1", "envelope": {"payload": {"text": "small"}}}

    monkeypatch.setattr(registry_module, "MAX_PENDING_COMMANDS_PER_RUNNER", 1)
    registry.queue_command(credentials.runner_id, "command-1", frame, now=102)
    with pytest.raises(ValueError, match="quota"):
        registry.queue_command(
            credentials.runner_id,
            "command-2",
            {**frame, "command_id": "command-2"},
            now=103,
        )

    monkeypatch.setattr(registry_module, "MAX_COMMAND_FRAME_BYTES", 10)
    with pytest.raises(ValueError, match="size limit"):
        registry.queue_command(
            credentials.runner_id,
            "command-3",
            {"command_id": "command-3", "envelope": {"payload": {"text": "large"}}},
            now=104,
        )

    monkeypatch.setattr(registry_module, "MAX_COMMAND_RESULT_BYTES", 10)
    with pytest.raises(ValueError, match="size limit"):
        registry.complete_command(
            credentials.runner_id,
            "command-1",
            result={"ok": True, "result": {"text": "too large"}},
            now=105,
        )
    registry.close()
