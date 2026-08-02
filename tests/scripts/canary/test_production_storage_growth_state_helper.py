from __future__ import annotations

import builtins
import copy
import fcntl
import hashlib
import json
import os
import runpy
import select
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract
from scripts.canary import production_storage_growth_executor as executor
from scripts.canary import production_storage_growth_installer as installer
from scripts.canary import production_storage_growth_state_helper as helper
from scripts.canary import production_cutover_passkey as cutover


RELEASE = "a" * 40
NOW = 2_000_000_000


def _minimal_plan() -> dict:
    return {
        "release_revision": RELEASE,
        "plan_sha256": "1" * 64,
        "provider_request_id": "fixed-request",
        "idempotency_key_sha256": "2" * 64,
    }


def _bundle() -> dict:
    return {
        "bundle_sha256": "3" * 64,
        "authorization_receipt": {
            "receipt_sha256": "4" * 64,
            "prior_journal_head_sha256": "5" * 64,
            "consumed_at_unix": NOW - 10,
            "execution_window_expires_at_unix": NOW + 10,
        },
    }


def _owner_binding(helper_payload: bytes = b"fixed helper") -> dict:
    artifacts = {
        name: {
            "release_relative": relative,
            "sha256": (
                hashlib.sha256(helper_payload).hexdigest()
                if name == "state_helper" else "1" * 64
            ),
            "size": len(helper_payload),
        }
        for name, relative in contract.RUNTIME_ARTIFACT_RELATIVES.items()
    }
    unsigned = {
        "schema": contract.RUNTIME_ARTIFACT_ATTESTATION_SCHEMA,
        "release_revision": RELEASE,
        "owner_support_manifest_sha256": "2" * 64,
        "owner_support_source_tree_oid": "3" * 40,
        "artifacts": artifacts,
    }
    return installer.build_owner_artifact_binding(
        RELEASE,
        {
            **unsigned,
            "attestation_sha256": protocol.sha256_json(unsigned),
        },
    )


def _predecessor_identity(
    pid: int,
    *,
    start_time_ticks: int = 100,
) -> installer._ProcessIdentity:
    return installer._ProcessIdentity(
        pid=pid,
        owner_uid=os.getuid(),
        start_time_ticks=start_time_ticks,
        executable_device=11,
        executable_inode=12,
        argv=(
            b"/usr/bin/python3",
            str(installer.OWNER_STATE_HELPER).encode(),
        ),
    )


def _machine(tmp_path: Path, *, now: int = NOW) -> helper.RootStateMachine:
    tmp_path.chmod(0o700)
    machine = helper.RootStateMachine(
        state_root=str(tmp_path),
        helper_path=str(tmp_path / "helper"),
        now=lambda: now,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    machine.release_sha = RELEASE
    machine.helper_sha = "6" * 64
    machine.receipt_public_key_hex = "7" * 64
    machine.plan = _minimal_plan()
    return machine


def _stub_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        helper,
        "validate_authorization",
        lambda *_args, **_kwargs: _bundle(),
    )
    monkeypatch.setattr(
        helper,
        "validate_observation_for_plan",
        lambda observation, _plan: (
            observation["state"], observation["observation_sha256"]
        ),
    )


def test_embedded_ed25519_verifier_rejects_forgery() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    message = b"release-bound storage authorization"
    signature = private.sign(message)
    assert helper.verify_ed25519(public, signature, message) is True
    forged = bytearray(signature)
    forged[0] ^= 1
    assert helper.verify_ed25519(public, bytes(forged), message) is False


def test_begin_journal_to_event_crash_gap_is_mechanically_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_validation(monkeypatch)
    machine = _machine(tmp_path)
    original_append = helper._append_events

    def crash_after_journal(*_args, **_kwargs):
        raise helper.StateHelperError("simulated_event_publication_crash")

    monkeypatch.setattr(helper, "_append_events", crash_after_journal)
    with pytest.raises(helper.StateHelperError, match="simulated_event"):
        machine.begin({
            "authorization_bundle": _bundle(),
            "initial_observation": {
                "state": "source", "observation_sha256": "8" * 64,
            },
        })
    journal_path, event_path = machine._paths()
    assert Path(journal_path).is_file()
    assert not Path(event_path).exists()

    monkeypatch.setattr(helper, "_append_events", original_append)
    recovered = _machine(tmp_path).begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "source", "observation_sha256": "8" * 64,
        },
    })
    assert recovered["recovered"] is True
    assert [event["event_kind"] for event in recovered["events"]] == [
        "execution_started"
    ]
    assert Path(event_path).read_bytes().endswith(b"\n")


def test_completion_journal_to_event_crash_gap_is_mechanically_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_validation(monkeypatch)
    machine = _machine(tmp_path)
    machine.begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "source", "observation_sha256": "8" * 64,
        },
    })
    original_append = helper._append_events
    original_write = helper._write_atomic

    def crash_after_completed_journal(*_args, **_kwargs):
        raise helper.StateHelperError("simulated_completion_event_crash")

    monkeypatch.setattr(helper, "_append_events", crash_after_completed_journal)
    with pytest.raises(helper.StateHelperError, match="simulated_completion"):
        machine.complete({
            "final_observation": {
                "state": "target", "observation_sha256": "9" * 64,
            },
        })
    journal_path, event_path = machine._paths()
    completed_journal = Path(journal_path).read_bytes()
    started_events = Path(event_path).read_bytes()

    def forbid_second_write(*_args, **_kwargs):
        raise AssertionError("completion retry must not rewrite journal")

    repairs: list[int] = []

    def repair_terminal_event(*args, **kwargs):
        repairs.append(1)
        return original_append(*args, **kwargs)

    monkeypatch.setattr(helper, "_write_atomic", forbid_second_write)
    monkeypatch.setattr(helper, "_append_events", repair_terminal_event)
    retried = machine.complete({
        "final_observation": {
            "state": "target", "observation_sha256": "9" * 64,
        },
    })
    assert retried["journal"]["state"] == "completed"
    assert Path(journal_path).read_bytes() == completed_journal
    repaired_events = Path(event_path).read_bytes()
    assert repaired_events != started_events
    assert repaired_events.count(
        b'"event_kind":"execution_completed"'
    ) == 1
    assert repairs == [1]

    monkeypatch.setattr(helper, "_append_events", forbid_second_write)
    assert machine.complete({
        "final_observation": {
            "state": "target", "observation_sha256": "9" * 64,
        },
    }) == retried
    assert Path(event_path).read_bytes() == repaired_events

    monkeypatch.setattr(helper, "_write_atomic", original_write)
    monkeypatch.setattr(helper, "_append_events", original_append)

    recovered = _machine(tmp_path).begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "target", "observation_sha256": "9" * 64,
        },
    })
    assert recovered["journal"]["state"] == "completed"
    assert [event["event_kind"] for event in recovered["events"]] == [
        "execution_started", "execution_completed"
    ]
    assert recovered["events"][-1] == recovered["journal"][
        "transition_event"
    ]


def test_complete_is_exact_idempotent_and_rejects_different_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_validation(monkeypatch)
    machine = _machine(tmp_path)
    machine.begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "source", "observation_sha256": "8" * 64,
        },
    })
    final = {
        "state": "target", "observation_sha256": "9" * 64,
    }
    first = machine.complete({"final_observation": final})
    journal_path, event_path = machine._paths()
    journal_raw = Path(journal_path).read_bytes()
    event_raw = Path(event_path).read_bytes()

    def forbid_write(*_args, **_kwargs):
        raise AssertionError("completed journal must never be written again")

    monkeypatch.setattr(helper, "_write_atomic", forbid_write)
    monkeypatch.setattr(helper, "_append_events", forbid_write)
    second = machine.complete({"final_observation": dict(final)})
    assert second == first
    assert Path(journal_path).read_bytes() == journal_raw
    assert Path(event_path).read_bytes() == event_raw
    assert event_raw.count(b'"event_kind":"execution_completed"') == 1

    with pytest.raises(
        helper.StateHelperError,
        match="production_storage_state_helper_sequence_invalid",
    ):
        machine.complete({
            "final_observation": {
                "state": "target", "observation_sha256": "a" * 64,
            },
        })
    assert Path(journal_path).read_bytes() == journal_raw
    assert Path(event_path).read_bytes() == event_raw


def test_divergent_transition_event_is_not_repaired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_validation(monkeypatch)
    machine = _machine(tmp_path)
    machine.begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "source", "observation_sha256": "8" * 64,
        },
    })
    journal_path, _event_path = machine._paths()
    journal = json.loads(Path(journal_path).read_text())
    journal["transition_event"]["observation_sha256"] = "a" * 64
    transition_unsigned = dict(journal["transition_event"])
    transition_unsigned.pop("event_head_sha256")
    journal["transition_event"]["event_head_sha256"] = helper.sha256_json(
        transition_unsigned
    )
    unsigned = dict(journal)
    unsigned.pop("journal_sha256")
    journal["journal_sha256"] = helper.sha256_json(unsigned)
    helper._write_atomic(
        journal_path, journal, str(tmp_path), os.getuid(), os.getgid()
    )

    with pytest.raises(helper.StateHelperError, match="event_log_invalid"):
        _machine(tmp_path).begin({
            "authorization_bundle": _bundle(),
            "initial_observation": {
                "state": "source", "observation_sha256": "8" * 64,
            },
        })


def test_delayed_recovery_uses_original_signed_window_not_current_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks: list[bool] = []

    def validate(_bundle_value, _plan_value, _key, _now, require_current=True):
        checks.append(require_current)
        if require_current and _now >= NOW + 10:
            raise helper.StateHelperError(
                "production_storage_authorization_invalid"
            )
        return _bundle()

    monkeypatch.setattr(helper, "validate_authorization", validate)
    monkeypatch.setattr(
        helper,
        "validate_observation_for_plan",
        lambda observation, _plan: (
            observation["state"], observation["observation_sha256"]
        ),
    )
    _machine(tmp_path, now=NOW).begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "source", "observation_sha256": "8" * 64,
        },
    })
    recovered = _machine(tmp_path, now=NOW + 100).begin({
        "authorization_bundle": _bundle(),
        "initial_observation": {
            "state": "partial", "observation_sha256": "9" * 64,
        },
    })
    assert recovered["recovered"] is True
    assert checks == [True, False]


def test_state_helper_frames_have_no_caller_key_or_path_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_validation(monkeypatch)
    machine = _machine(tmp_path)
    with pytest.raises(
        helper.StateHelperError,
        match="production_storage_state_helper_frame_invalid",
    ):
        machine.begin({
            "authorization_bundle": _bundle(),
            "initial_observation": {
                "state": "source", "observation_sha256": "8" * 64,
            },
            "receipt_public_key_ed25519_hex": "a" * 64,
        })


def test_installer_holds_validated_execution_lock_during_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    original_write = installer._write_atomic
    lock_checks: list[bool] = []

    def checked_write(path: Path, payload: bytes, **kwargs) -> None:
        if path == state_root / ".installation.json":
            lock_path = state_root / ".execution.lock"
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(
                        descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                lock_checks.append(True)
            finally:
                os.close(descriptor)
        original_write(path, payload, **kwargs)

    monkeypatch.setattr(installer, "_write_atomic", checked_write)
    installer.install_owner_state_root(
        RELEASE,
        sealed_artifact_binding=_owner_binding(),
        state_root=state_root,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        effective_uid=lambda: 0,
        artifact_verifier=lambda value, **_kwargs: value,
    )
    assert lock_checks == [True]
    assert (state_root / ".execution.lock").stat().st_mode & 0o777 == 0o600


def test_helper_acquires_execution_lock_before_reading_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    helper_path = state_root / "helper"
    helper_path.write_bytes(b"fixed helper")
    helper_path.chmod(0o555)
    receipt_path = state_root / ".installation.json"
    receipt_path.write_bytes(b"{}\n")
    receipt_path.chmod(0o600)
    held = installer._acquire_owner_execution_lock(
        state_root, uid=os.getuid(), gid=os.getgid()
    )
    lock_attempted = threading.Event()
    receipt_read = threading.Event()
    original_flock = fcntl.flock

    def observed_flock(descriptor: int, operation: int) -> None:
        if operation == fcntl.LOCK_EX:
            lock_attempted.set()
        original_flock(descriptor, operation)

    def observed_read(_path: str) -> dict:
        receipt_read.set()
        raise helper.StateHelperError("expected_invalid_receipt")

    monkeypatch.setattr(helper.fcntl, "flock", observed_flock)
    monkeypatch.setattr(helper, "_read_json", observed_read)
    machine = helper.RootStateMachine(
        state_root=str(state_root),
        helper_path=str(helper_path),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    failures: list[Exception] = []

    def open_helper() -> None:
        try:
            machine.open()
        except Exception as error:  # noqa: BLE001 - asserted below
            failures.append(error)

    thread = threading.Thread(target=open_helper)
    thread.start()
    assert lock_attempted.wait(timeout=1)
    assert receipt_read.wait(timeout=0.05) is False
    installer._release_owner_execution_lock(held)
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert receipt_read.is_set()
    assert len(failures) == 1
    assert isinstance(failures[0], helper.StateHelperError)


@pytest.mark.parametrize("preexisting", (False, True))
def test_owner_publish_transaction_restores_all_four_artifacts(
    tmp_path: Path,
    preexisting: bool,
) -> None:
    helper_path = tmp_path / "helper"
    sudoers_path = tmp_path / "sudoers"
    receipt_path = tmp_path / "receipt"
    public_path = tmp_path / "public"
    paths = (
        (helper_path, 0o555, b"old helper"),
        (sudoers_path, 0o440, b"fixed sudoers\n"),
        (receipt_path, 0o600, b"old receipt\n"),
        (public_path, 0o444, b"old public\n"),
    )
    if preexisting:
        for path, mode, payload in paths:
            path.write_bytes(payload)
            path.chmod(mode)

    public = {"schema": "new-public"}
    expected_public = protocol.canonical_json_bytes(public) + b"\n"

    def accept(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def fail_final_attestation() -> dict:
        assert helper_path.read_bytes() == b"new helper"
        assert not sudoers_path.exists()
        assert receipt_path.read_bytes() == b"new receipt\n"
        assert public_path.read_bytes() == expected_public
        raise installer.ProductionStorageInstallerError(
            "simulated_public_attestation_failure"
        )

    lock = installer._acquire_owner_execution_lock(
        tmp_path, uid=os.getuid(), gid=os.getgid()
    )
    try:
        with pytest.raises(
            installer.ProductionStorageInstallerError,
            match="simulated_public_attestation_failure",
        ):
            installer._publish_owner_installation_transaction(
                state_helper_path=helper_path,
                state_helper_payload=b"new helper",
                sudoers_path=sudoers_path,
                sudoers_payload=b"fixed sudoers\n",
                receipt_path=receipt_path,
                receipt_payload=b"new receipt\n",
                public_readiness_path=public_path,
                build_public_readiness=lambda: public,
                attest_public_readiness=fail_final_attestation,
                quiesce_predecessors=lambda: None,
                uid=os.getuid(),
                gid=os.getgid(),
                sudoers_validator=accept,
            )
    finally:
        installer._release_owner_execution_lock(lock)
    for path, mode, payload in paths:
        if preexisting:
            assert path.read_bytes() == payload
            assert path.stat().st_mode & 0o777 == mode
        else:
            assert not path.exists()


def test_rollback_restores_predecessor_before_sudo_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_path = tmp_path / "helper"
    sudoers_path = tmp_path / "sudoers"
    receipt_path = tmp_path / "receipt"
    public_path = tmp_path / "public"
    old_payloads = {
        helper_path: b"old helper",
        sudoers_path: b"old sudoers\n",
        receipt_path: b"old receipt\n",
        public_path: b"old public\n",
    }
    modes = {
        helper_path: 0o555,
        sudoers_path: 0o440,
        receipt_path: 0o600,
        public_path: 0o444,
    }
    for path, payload in old_payloads.items():
        path.write_bytes(payload)
        path.chmod(modes[path])

    original_write = installer._write_atomic
    restore_order: list[Path] = []
    sudo_admitted = threading.Event()
    observed_at_admission: list[tuple[bytes, bytes]] = []

    def observed_write(path: Path, payload: bytes, **kwargs) -> None:
        original_write(path, payload, **kwargs)
        if payload == old_payloads.get(path):
            restore_order.append(path)
            if path == sudoers_path:
                sudo_admitted.set()

    def concurrent_invocation() -> None:
        if sudo_admitted.wait(timeout=2):
            observed_at_admission.append((
                helper_path.read_bytes(), receipt_path.read_bytes()
            ))

    monkeypatch.setattr(installer, "_write_atomic", observed_write)
    watcher = threading.Thread(target=concurrent_invocation)
    watcher.start()
    lock = installer._acquire_owner_execution_lock(
        tmp_path, uid=os.getuid(), gid=os.getgid()
    )
    try:
        with pytest.raises(
            installer.ProductionStorageInstallerError,
            match="simulated_public_attestation_failure",
        ):
            installer._publish_owner_installation_transaction(
                state_helper_path=helper_path,
                state_helper_payload=b"new helper",
                sudoers_path=sudoers_path,
                sudoers_payload=b"new sudoers\n",
                receipt_path=receipt_path,
                receipt_payload=b"new receipt\n",
                public_readiness_path=public_path,
                build_public_readiness=lambda: {"schema": "new"},
                attest_public_readiness=lambda: (_ for _ in ()).throw(
                    installer.ProductionStorageInstallerError(
                        "simulated_public_attestation_failure"
                    )
                ),
                quiesce_predecessors=lambda: None,
                uid=os.getuid(),
                gid=os.getgid(),
                sudoers_validator=lambda command, **_kwargs: (
                    subprocess.CompletedProcess(command, 0, b"", b"")
                ),
            )
    finally:
        installer._release_owner_execution_lock(lock)
    watcher.join(timeout=2)
    assert not watcher.is_alive()
    assert restore_order == [
        public_path, receipt_path, helper_path, sudoers_path
    ]
    assert observed_at_admission == [(b"old helper", b"old receipt\n")]


def test_failure_after_sudoers_replace_revokes_and_quiesces_before_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_path = tmp_path / "helper"
    sudoers_path = tmp_path / "sudoers"
    receipt_path = tmp_path / "receipt"
    public_path = tmp_path / "public"
    survived_path = tmp_path / "successor-survived"
    old = {
        helper_path: (0o555, b"old helper"),
        sudoers_path: (0o440, b"old sudoers\n"),
        receipt_path: (0o600, b"old receipt\n"),
        public_path: (0o444, b"old public\n"),
    }
    for path, (mode, payload) in old.items():
        path.write_bytes(payload)
        path.chmod(mode)

    original_write = installer._write_atomic
    successor: list[subprocess.Popen[bytes]] = []
    successor_identity: list[installer._ProcessIdentity] = []
    admission_absent_during_quiesce: list[bool] = []

    successor_source = r"""
import fcntl
import os
import sys

helper_path, receipt_path, lock_path, survived_path = sys.argv[1:]
if open(helper_path, "rb").read() != b"new helper":
    raise SystemExit(3)
if open(receipt_path, "rb").read() != b"new receipt\n":
    raise SystemExit(4)
sys.stdout.write("successor-validated\n")
sys.stdout.flush()
lock = os.open(lock_path, os.O_RDWR)
fcntl.flock(lock, fcntl.LOCK_EX)
open(survived_path, "w").write("survived")
"""

    def fail_after_sudoers_replace(
        path: Path, payload: bytes, **kwargs
    ) -> None:
        if path != sudoers_path or payload != b"new sudoers\n":
            original_write(path, payload, **kwargs)
            return
        staged = tmp_path / ".sudoers-after-replace"
        staged.write_bytes(payload)
        staged.chmod(0o440)
        os.replace(staged, path)
        process = subprocess.Popen(
            (
                sys.executable,
                "-c",
                successor_source,
                str(helper_path),
                str(receipt_path),
                str(tmp_path / ".execution.lock"),
                str(survived_path),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        successor.append(process)
        successor_identity.append(
            _predecessor_identity(process.pid, start_time_ticks=200)
        )
        assert process.stdout is not None
        readable, _, _ = select.select([process.stdout], [], [], 2)
        assert readable
        assert process.stdout.readline() == b"successor-validated\n"
        raise installer.ProductionStorageInstallerError(
            "simulated_after_sudoers_replace"
        )

    def quiesce() -> None:
        if not successor or successor[0].poll() is not None:
            return
        process = successor[0]
        identity = successor_identity[0]
        admission_absent_during_quiesce.append(
            not sudoers_path.exists()
        )
        installer._quiesce_predecessor_helpers(
            authorized_client_uid=os.getuid(),
            process_lister=lambda: (
                (identity,) if process.poll() is None else ()
            ),
            identity_reader=lambda pid: (
                identity
                if pid == process.pid and process.poll() is None else None
            ),
            pidfd_opener=lambda pid, _flags: 99 if pid == process.pid else -1,
            pidfd_signaler=lambda _descriptor, signum, _info, _flags: (
                process.send_signal(signum)
            ),
            fd_closer=lambda _descriptor: None,
        )

    monkeypatch.setattr(installer, "_write_atomic", fail_after_sudoers_replace)
    lock = installer._acquire_owner_execution_lock(
        tmp_path, uid=os.getuid(), gid=os.getgid()
    )
    try:
        with pytest.raises(
            installer.ProductionStorageInstallerError,
            match="simulated_after_sudoers_replace",
        ):
            installer._publish_owner_installation_transaction(
                state_helper_path=helper_path,
                state_helper_payload=b"new helper",
                sudoers_path=sudoers_path,
                sudoers_payload=b"new sudoers\n",
                receipt_path=receipt_path,
                receipt_payload=b"new receipt\n",
                public_readiness_path=public_path,
                build_public_readiness=lambda: {"schema": "new"},
                attest_public_readiness=lambda: {"schema": "new"},
                quiesce_predecessors=quiesce,
                uid=os.getuid(),
                gid=os.getgid(),
                sudoers_validator=lambda command, **_kwargs: (
                    subprocess.CompletedProcess(command, 0, b"", b"")
                ),
            )
    finally:
        if successor and successor[0].poll() is None:
            successor[0].kill()
            successor[0].wait(timeout=2)
        installer._release_owner_execution_lock(lock)
    assert admission_absent_during_quiesce == [True]
    assert successor and successor[0].returncode != 0
    assert not survived_path.exists()
    for path, (mode, payload) in old.items():
        assert path.read_bytes() == payload
        assert path.stat().st_mode & 0o777 == mode


def test_predecessor_pid_reuse_before_pidfd_revalidation_fails_closed() -> None:
    discovered = _predecessor_identity(4242, start_time_ticks=100)
    reused = _predecessor_identity(4242, start_time_ticks=101)
    signals: list[tuple[int, int, object, int]] = []
    closed: list[int] = []

    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_predecessor_identity_changed",
    ):
        installer._quiesce_predecessor_helpers(
            authorized_client_uid=os.getuid(),
            process_lister=lambda: (discovered,),
            identity_reader=lambda _pid: reused,
            pidfd_opener=lambda _pid, _flags: 99,
            pidfd_signaler=lambda *args: signals.append(args),
            fd_closer=closed.append,
            sleeper=lambda _seconds: None,
        )
    assert signals == []
    assert closed == [99]


def test_predecessor_quiescence_fails_closed_without_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(installer.os, "pidfd_open", raising=False)
    monkeypatch.delattr(
        installer.signal, "pidfd_send_signal", raising=False
    )
    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_predecessor_pidfd_unavailable",
    ):
        installer._quiesce_predecessor_helpers(
            authorized_client_uid=os.getuid(),
            process_lister=lambda: (),
        )


def test_pid_reuse_after_final_check_signals_original_pidfd() -> None:
    discovered = _predecessor_identity(4242, start_time_ticks=100)
    reused = _predecessor_identity(4242, start_time_ticks=101)
    numeric_identity = [discovered]
    listing_calls = 0
    pidfd_targets = {99: discovered}
    signals: list[tuple[int, int, installer._ProcessIdentity]] = []

    def list_processes() -> tuple[installer._ProcessIdentity, ...]:
        nonlocal listing_calls
        listing_calls += 1
        return (discovered,) if listing_calls == 1 else ()

    def final_revalidation(_pid: int) -> installer._ProcessIdentity:
        current = numeric_identity[0]
        numeric_identity[0] = reused
        return current

    def signal_pidfd(
        descriptor: int, signum: int, _info: object, _flags: int
    ) -> None:
        signals.append((descriptor, signum, pidfd_targets[descriptor]))

    installer._quiesce_predecessor_helpers(
        authorized_client_uid=os.getuid(),
        process_lister=list_processes,
        identity_reader=final_revalidation,
        pidfd_opener=lambda _pid, _flags: 99,
        pidfd_signaler=signal_pidfd,
        fd_closer=lambda _descriptor: None,
        sleeper=lambda _seconds: None,
    )
    assert numeric_identity == [reused]
    assert signals == [
        (99, installer.signal.SIGTERM, discovered),
        (99, installer.signal.SIGKILL, discovered),
    ]


def test_execution_lock_release_closes_fd_without_leak(tmp_path: Path) -> None:
    descriptor_root = (
        Path("/proc/self/fd")
        if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    )
    before = len(tuple(descriptor_root.iterdir()))
    for _ in range(32):
        descriptor = installer._acquire_owner_execution_lock(
            tmp_path, uid=os.getuid(), gid=os.getgid()
        )
        installer._release_owner_execution_lock(descriptor)
        installer._release_owner_execution_lock(descriptor)
        with pytest.raises(OSError):
            os.fstat(descriptor)
    after = len(tuple(descriptor_root.iterdir()))
    assert after == before


def test_successor_quiesces_864c239_predecessor_cached_before_lock(
    tmp_path: Path,
) -> None:
    helper_path = tmp_path / "helper"
    sudoers_path = tmp_path / "sudoers"
    receipt_path = tmp_path / "receipt"
    public_path = tmp_path / "public"
    continued_path = tmp_path / "predecessor-continued"
    old_helper = b"864c239 predecessor helper"
    old_authority = "cached-864c239-authority"
    old_receipt = {
        "state_helper_sha256": hashlib.sha256(old_helper).hexdigest(),
        "authority": old_authority,
    }
    helper_path.write_bytes(old_helper)
    helper_path.chmod(0o555)
    sudoers_path.write_bytes(b"old sudoers\n")
    sudoers_path.chmod(0o440)
    receipt_path.write_bytes(
        protocol.canonical_json_bytes(old_receipt) + b"\n"
    )
    receipt_path.chmod(0o600)
    public_path.write_bytes(b"old public\n")
    public_path.chmod(0o444)
    lock = installer._acquire_owner_execution_lock(
        tmp_path, uid=os.getuid(), gid=os.getgid()
    )
    lock_identity = os.fstat(lock)

    # Concurrency-critical open order copied from the installed 864c239
    # predecessor: validate receipt+binary, cache authority, then take lock.
    predecessor_source = r"""
import fcntl
import hashlib
import json
import os
import sys

helper_path, receipt_path, lock_path, continued_path = sys.argv[1:]
helper_raw = open(helper_path, "rb").read()
receipt = json.loads(open(receipt_path, "rb").read())
if receipt["state_helper_sha256"] != hashlib.sha256(helper_raw).hexdigest():
    raise SystemExit(3)
cached_authority = receipt["authority"]
sys.stdout.write("validated-before-lock\n")
sys.stdout.flush()
lock = os.open(lock_path, os.O_RDWR)
fcntl.flock(lock, fcntl.LOCK_EX)
open(continued_path, "w").write(cached_authority)
"""
    predecessor = subprocess.Popen(
        (
            sys.executable,
            "-c",
            predecessor_source,
            str(helper_path),
            str(receipt_path),
            str(tmp_path / ".execution.lock"),
            str(continued_path),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    predecessor_identity = _predecessor_identity(predecessor.pid)
    public = {"schema": "successor-public"}

    def accept(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def attest_public() -> dict:
        raw = public_path.read_bytes()
        return protocol.decode_canonical_json(raw[:-1])

    survived_transaction = False
    try:
        assert predecessor.stdout is not None
        readable, _, _ = select.select([predecessor.stdout], [], [], 2)
        assert readable
        assert predecessor.stdout.readline() == b"validated-before-lock\n"
        installed = installer._publish_owner_installation_transaction(
            state_helper_path=helper_path,
            state_helper_payload=b"successor helper",
            sudoers_path=sudoers_path,
            sudoers_payload=b"successor sudoers\n",
            receipt_path=receipt_path,
            receipt_payload=b"successor receipt\n",
            public_readiness_path=public_path,
            build_public_readiness=lambda: public,
            attest_public_readiness=attest_public,
            quiesce_predecessors=lambda: (
                installer._quiesce_predecessor_helpers(
                    authorized_client_uid=os.getuid(),
                    process_lister=lambda: (
                        (predecessor_identity,)
                        if predecessor.poll() is None else ()
                    ),
                    identity_reader=lambda pid: (
                        predecessor_identity
                        if pid == predecessor.pid
                        and predecessor.poll() is None else None
                    ),
                    pidfd_opener=lambda pid, _flags: (
                        99 if pid == predecessor.pid else -1
                    ),
                    pidfd_signaler=lambda _descriptor, signum, _info, _flags: (
                        predecessor.send_signal(signum)
                    ),
                    fd_closer=lambda _descriptor: None,
                )
            ),
            uid=os.getuid(),
            gid=os.getgid(),
            sudoers_validator=accept,
        )
        survived_transaction = predecessor.poll() is None
    finally:
        if predecessor.poll() is None:
            predecessor.kill()
            predecessor.wait(timeout=2)
        installer._release_owner_execution_lock(lock)
    assert installed == public
    assert survived_transaction is False
    assert predecessor.returncode != 0
    assert not continued_path.exists()
    lock_path_identity = (tmp_path / ".execution.lock").stat()
    assert lock_path_identity.st_dev == lock_identity.st_dev
    assert lock_path_identity.st_ino == lock_identity.st_ino
    assert helper_path.read_bytes() == b"successor helper"
    assert sudoers_path.read_bytes() == b"successor sudoers\n"


def test_malformed_root_receipt_key_fails_with_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    helper_path = tmp_path / "helper"
    helper_path.write_bytes(b"fixed helper")
    helper_path.chmod(0o555)
    artifacts = {
        name: {
            "release_relative": relative,
            "sha256": (
                hashlib.sha256(b"fixed helper").hexdigest()
                if name == "state_helper" else "1" * 64
            ),
            "size": 12,
        }
        for name, relative in contract.RUNTIME_ARTIFACT_RELATIVES.items()
    }
    artifact_unsigned = {
        "schema": contract.RUNTIME_ARTIFACT_ATTESTATION_SCHEMA,
        "release_revision": RELEASE,
        "owner_support_manifest_sha256": "2" * 64,
        "owner_support_source_tree_oid": "3" * 40,
        "artifacts": artifacts,
    }
    artifact_attestation = {
        **artifact_unsigned,
        "attestation_sha256": protocol.sha256_json(artifact_unsigned),
    }
    binding = installer.build_owner_artifact_binding(
        RELEASE, artifact_attestation
    )
    installer.install_owner_state_root(
        RELEASE,
        sealed_artifact_binding=binding,
        state_root=tmp_path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        effective_uid=lambda: 0,
        artifact_verifier=lambda value, **_kwargs: value,
        state_helper_source=helper_path,
    )
    authority = {
        "release_sha": RELEASE,
        "receipt_public_key_ed25519_hex": "not-hex",
        "receipt_public_key_id": "0" * 64,
        "root_owned_trust_bundle_validated": True,
        "rotation_requires_new_release_and_owner_install": True,
    }
    authority["attestation_sha256"] = helper.sha256_json(authority)
    receipt_path = tmp_path / ".installation.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["authority_key_attestation"] = authority
    receipt["authority_key_attestation_sha256"] = authority[
        "attestation_sha256"
    ]
    receipt_unsigned = dict(receipt)
    receipt_unsigned.pop("installation_receipt_sha256")
    receipt["installation_receipt_sha256"] = helper.sha256_json(
        receipt_unsigned
    )
    receipt_path.write_bytes(helper.canonical_bytes(receipt) + b"\n")
    receipt_path.chmod(0o600)
    monkeypatch.setenv("SUDO_UID", str(os.getuid()))
    monkeypatch.setenv("SUDO_GID", str(os.getgid()))
    machine = helper.RootStateMachine(
        state_root=str(tmp_path),
        helper_path=str(helper_path),
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
    )
    with pytest.raises(
        helper.StateHelperError,
        match="production_storage_state_helper_installation_invalid",
    ):
        machine.open()


def test_installer_pwd_import_failure_is_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = {
        name: {
            "release_relative": relative,
            "sha256": "1" * 64,
            "size": 1,
        }
        for name, relative in contract.RUNTIME_ARTIFACT_RELATIVES.items()
    }
    unsigned = {
        "schema": contract.RUNTIME_ARTIFACT_ATTESTATION_SCHEMA,
        "release_revision": RELEASE,
        "owner_support_manifest_sha256": "2" * 64,
        "owner_support_source_tree_oid": "3" * 40,
        "artifacts": artifacts,
    }
    attestation = {
        **unsigned,
        "attestation_sha256": protocol.sha256_json(unsigned),
    }
    binding = installer.build_owner_artifact_binding(RELEASE, attestation)
    original_import = builtins.__import__

    def without_pwd(name, *args, **kwargs):
        if name == "pwd":
            raise ImportError("pwd unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_pwd)
    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_owner_installer_invalid",
    ):
        installer.install_owner_state_root(
            RELEASE,
            sealed_artifact_binding=binding,
            state_root=tmp_path / "state",
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            effective_uid=lambda: 0,
            artifact_verifier=lambda value, **_kwargs: value,
        )


def test_root_installer_independently_revalidates_portable_key_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    raw = public.public_bytes_raw()
    trust = {
        "authority_release_sha": RELEASE,
        "trust_bundle_sha256": "6" * 64,
    }
    unsigned = {
        "schema": "muncho-production-storage-authority-key-attestation.v1",
        "release_sha": RELEASE,
        "receipt_public_key_ed25519_hex": raw.hex(),
        "receipt_public_key_id": hashlib.sha256(raw).hexdigest(),
        "portable_trust_bundle_sha256": "6" * 64,
        "portable_trust_bundle": trust,
        "authority_manifest_sha256": "7" * 64,
        "authority_host_receipt_sha256": "8" * 64,
        "root_owned_trust_bundle_validated": True,
        "rotation_requires_new_release_and_owner_install": True,
    }
    attestation = {
        **unsigned,
        "attestation_sha256": protocol.sha256_json(unsigned),
    }
    calls: list[dict] = []

    def validate(value):
        calls.append(dict(value))
        return trust, public

    monkeypatch.setattr(cutover, "validate_trust_bundle", validate)
    assert installer._validate_authority_key_attestation(
        attestation, release_sha=RELEASE
    ) == attestation
    assert calls == [trust]
    forged = copy.deepcopy(attestation)
    forged["receipt_public_key_ed25519_hex"] = "0" * 64
    forged_unsigned = dict(forged)
    forged_unsigned.pop("attestation_sha256")
    forged["attestation_sha256"] = protocol.sha256_json(forged_unsigned)
    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_authority_key_attestation_invalid",
    ):
        installer._validate_authority_key_attestation(
            forged, release_sha=RELEASE
        )


def test_state_helper_imports_when_fcntl_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def without_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("fcntl unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_fcntl)
    loaded = runpy.run_path(str(Path(helper.__file__)), run_name="helper_smoke")
    assert loaded["fcntl"] is None


def test_sudoers_fragment_requires_visudo_and_leaves_no_failed_install(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixed-sudoers"
    payload = b"owner ALL=(root) NOPASSWD: /fixed/helper\n"
    captured: list[tuple[str, ...]] = []

    def reject(command, **_kwargs):
        captured.append(tuple(command))
        return subprocess.CompletedProcess(command, 1, b"", b"")

    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_state_helper_sudoers_invalid",
    ):
        installer._install_validated_sudoers(
            path,
            payload,
            uid=os.getuid(),
            gid=os.getgid(),
            runner=reject,
        )
    assert captured and captured[0][:2] == ("/usr/sbin/visudo", "-cf")
    assert not path.exists()


def test_sudoers_fragment_install_is_exact_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixed-sudoers"
    payload = b"owner ALL=(root) NOPASSWD: /fixed/helper\n"

    def accept(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, b"", b"")

    installer._install_validated_sudoers(
        path,
        payload,
        uid=os.getuid(),
        gid=os.getgid(),
        runner=accept,
    )
    assert path.read_bytes() == payload
    assert path.stat().st_mode & 0o777 == 0o440
    path.chmod(0o600)
    path.write_bytes(b"different\n")
    path.chmod(0o440)
    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_state_helper_sudoers_conflict",
    ):
        installer._install_validated_sudoers(
            path,
            payload,
            uid=os.getuid(),
            gid=os.getgid(),
            runner=accept,
        )


def test_privileged_session_close_kills_and_reaps_once() -> None:
    class Input:
        def close(self) -> None:
            pass

    class Process:
        stdin = Input()

        def __init__(self) -> None:
            self.waits = 0
            self.kills = 0

        def wait(self, *, timeout: int) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("helper", timeout)
            return 0

        def kill(self) -> None:
            self.kills += 1

    session = object.__new__(executor.ProductionStoragePrivilegedStateSession)
    process = Process()
    session._process = process
    session.close()
    session.close()
    assert process.kills == 1
    assert process.waits == 2


def test_privileged_session_rejects_stale_public_readiness_before_attest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Input:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def write(self, _raw: bytes) -> int:  # pragma: no cover - not reached
            raise AssertionError("attest must not be sent")

        def flush(self) -> None:  # pragma: no cover - not reached
            raise AssertionError("attest must not be sent")

    ready_unsigned = {
        "schema": executor.STATE_HELPER_RESPONSE_SCHEMA,
        "operation": "ready",
        "ok": True,
        "document": {
            "release_sha": RELEASE,
            "state_helper_sha256": "6" * 64,
            "installation_receipt_sha256": "7" * 64,
            "lock_acquired": True,
        },
    }
    ready = protocol.canonical_json_bytes({
        **ready_unsigned,
        "response_sha256": protocol.sha256_json(ready_unsigned),
    }) + b"\n"

    class Output:
        def readline(self, _maximum: int) -> bytes:
            return ready

    class Process:
        def __init__(self) -> None:
            self.stdin = Input()
            self.stdout = Output()
            self.waited = False

        def wait(self, *, timeout: int) -> int:
            self.waited = True
            return 0

    process = Process()
    monkeypatch.setattr(contract, "validate_plan", lambda value: value)
    session = executor.ProductionStoragePrivilegedStateSession(
        release_sha=RELEASE,
        growth_plan=_minimal_plan(),
        expected_installation_receipt_sha256="8" * 64,
        runner=lambda *_args, **_kwargs: process,
    )
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_state_helper_unavailable",
    ):
        session.open()
    assert process.stdin.closed is True
    assert process.waited is True
