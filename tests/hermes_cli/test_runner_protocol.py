import copy

import pytest

from hermes_cli.runner_protocol import (
    RUNNER_PROTOCOL_VERSION,
    RunnerCommand,
    RunnerEvent,
    sign_envelope,
    verify_envelope,
)


def test_runner_command_roundtrip_preserves_idempotency_and_fencing():
    command = RunnerCommand.create(
        attempt_id="attempt-1",
        method="fs.read",
        run_id="run-1",
        binding_id="binding-1",
        params={"path": "src/app.ts"},
        lease_id="lease-1",
        fencing_token=7,
        command_id="command-1",
    )

    assert RunnerCommand.from_dict(command.to_dict()) == command
    assert command.protocol_version == RUNNER_PROTOCOL_VERSION
    assert command.command_id == "command-1"
    assert command.fencing_token == 7


def test_unknown_method_and_invalid_fencing_are_rejected():
    with pytest.raises(ValueError, match="method"):
        RunnerCommand.create(
            attempt_id="attempt-1",
            method="host.shell",
            run_id="run-1",
            binding_id="binding-1",
            params={},
            lease_id="lease-1",
            fencing_token=1,
        )

    with pytest.raises(ValueError, match="fencing"):
        RunnerCommand.create(
            attempt_id="attempt-1",
            method="fs.read",
            run_id="run-1",
            binding_id="binding-1",
            params={"path": "a"},
            lease_id="lease-1",
            fencing_token=-1,
        )


def test_signed_envelope_detects_payload_and_signature_tampering():
    command = RunnerCommand.create(
        attempt_id="attempt-1",
        method="git.status",
        run_id="run-1",
        binding_id="binding-1",
        params={},
        lease_id="lease-1",
        fencing_token=1,
        command_id="command-1",
    )
    envelope = sign_envelope(command.to_dict(), b"device-secret")

    assert verify_envelope(envelope, b"device-secret") == command.to_dict()

    changed = copy.deepcopy(envelope)
    changed["payload"]["binding_id"] = "binding-2"
    with pytest.raises(ValueError, match="signature"):
        verify_envelope(changed, b"device-secret")

    changed_signature = copy.deepcopy(envelope)
    changed_signature["signature"] = "0" * 64
    with pytest.raises(ValueError, match="signature"):
        verify_envelope(changed_signature, b"device-secret")


def test_runner_event_sequence_and_terminal_state_are_validated():
    event = RunnerEvent.create(
        run_id="run-1",
        attempt_id="attempt-1",
        sequence=3,
        event_type="run.completed",
        payload={"exit_code": 0},
        event_id="event-1",
    )

    assert RunnerEvent.from_dict(event.to_dict()) == event

    with pytest.raises(ValueError, match="sequence"):
        RunnerEvent.create(
            run_id="run-1",
            attempt_id="attempt-1",
            sequence=0,
            event_type="run.output",
            payload={},
        )
