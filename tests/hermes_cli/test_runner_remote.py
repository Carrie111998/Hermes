import base64
import json
import stat

import pytest

from hermes_cli.runner_protocol import RunnerCommand, sign_envelope, verify_envelope
from hermes_cli.runner_remote import (
    RemoteRunner,
    RunnerCredentialsFile,
    _sanitize_public_value,
)


def test_remote_runner_executes_only_signed_control_and_command_frames(tmp_path):
    key = b"k" * 32
    root = tmp_path / "repo"
    root.mkdir()
    remote = RemoteRunner(state_path=tmp_path / "runner.db", command_key=key)
    binding = remote.register_binding(
        binding_id="binding-1",
        label="Repo",
        path=root,
        project_id="project-1",
    )
    assert "path" not in repr(binding)

    lease = remote.process_frame(
        {
            "envelope": sign_envelope(
                {
                    "method": "lease.acquire",
                    "params": {
                        "binding_id": "binding-1",
                        "expected_head": None,
                        "owner": "run-1",
                        "ttl_seconds": 60,
                    },
                    "request_id": "ctl-2",
                },
                key,
            ),
            "request_id": "ctl-2",
            "type": "control",
        }
    )
    lease_payload = verify_envelope(lease["envelope"], key)["result"]
    command = RunnerCommand.create(
        attempt_id="attempt-1",
        binding_id="binding-1",
        fencing_token=lease_payload["fencing_token"],
        lease_id=lease_payload["lease_id"],
        method="fs.write",
        params={"path": "result.txt", "text": "hello"},
        run_id="run-1",
    )
    command_frame = {
        "command_id": command.command_id,
        "envelope": sign_envelope(command.to_dict(), key),
        "type": "command",
    }
    ack, early_result, accepted = remote.accept_command_frame(command_frame)
    ack_payload = verify_envelope(ack["envelope"], key)
    assert ack["type"] == "command.ack"
    assert ack_payload["command_id"] == command.command_id
    assert ack_payload["state"] == "accepted"
    assert early_result is None
    assert accepted is not None
    stored = remote.server.spool.command_result(command.command_id)
    assert stored is not None
    assert stored["state"] == "accepted"

    result = remote.execute_accepted_command(accepted)
    result_payload = verify_envelope(result["envelope"], key)
    assert result["type"] == "command.result"
    assert result_payload["ok"] is True
    assert result_payload["state"] == "completed"
    assert result_payload["replayed"] is False
    assert (root / "result.txt").read_text() == "hello"

    event_batch = remote.pending_event_batch()
    assert event_batch is not None
    events = verify_envelope(event_batch["envelope"], key)["events"]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    remote.acknowledge_event_batch(
        {
            "envelope": sign_envelope(
                {"event_ids": [event["event_id"] for event in events]},
                key,
            ),
            "type": "event.ack",
        }
    )
    assert remote.pending_event_batch() is None

    tampered = sign_envelope(command.to_dict(), key)
    tampered["payload"]["params"]["text"] = "tampered"
    with pytest.raises(ValueError, match="signature"):
        remote.process_frame(
            {
                "command_id": command.command_id,
                "envelope": tampered,
                "type": "command",
            }
        )
    remote.close()


def test_remote_runner_redacts_absolute_paths_from_all_outbound_result_strings():
    sanitized = _sanitize_public_value(
        {
            "output": "cwd /Users/alice/secret/repo and C:\\Users\\alice\\secret",
            "nested": [{"text": "/home/alice/private/file.txt"}],
        }
    )

    assert sanitized == {
        "output": "cwd [REDACTED_LOCAL_PATH] and [REDACTED_LOCAL_PATH]",
        "nested": [{"text": "[REDACTED_LOCAL_PATH]"}],
    }


def test_runner_credentials_file_is_private_and_does_not_store_enrollment_token(tmp_path):
    path = tmp_path / "credentials.json"
    credentials = RunnerCredentialsFile(path)
    credentials.save(
        runner_id="runner-1",
        device_token="device-secret",
        command_key=b"k" * 32,
    )

    loaded = credentials.load()
    assert loaded == {
        "command_key": base64.urlsafe_b64encode(b"k" * 32).decode("ascii"),
        "device_token": "device-secret",
        "runner_id": "runner-1",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "enrollment" not in json.dumps(loaded).lower()
