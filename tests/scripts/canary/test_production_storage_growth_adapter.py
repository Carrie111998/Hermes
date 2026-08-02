from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_adapter as adapter
from scripts.canary import production_storage_growth_contract as contract
from scripts.canary import production_storage_growth_guest as guest
from scripts.canary import production_storage_growth_installer as installer


BOOT_ID = "baf2a4ac-6450-4da8-a6de-d89a2f0c1250"
RELEASE = "a" * 40


def _owner_binding() -> dict:
    artifacts = {
        name: {
            "release_relative": relative,
            "sha256": "1" * 64,
            "size": 1024,
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
    return dict(installer.build_owner_artifact_binding(RELEASE, attestation))


def test_owner_state_installer_attests_digest_and_rejects_wrong_owner(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    state_root = tmp_path / "state"
    binding = _owner_binding()
    ready = installer.install_owner_state_root(
        RELEASE,
        sealed_artifact_binding=binding,
        state_root=state_root,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        effective_uid=lambda: 0,
        wall_clock=lambda: 2_000_000_000,
        artifact_verifier=lambda value, **_kwargs: value,
    )
    receipt = json.loads((state_root / ".installation.json").read_text())
    assert ready["ready"] is True
    assert ready["installer_sha256"] == binding["installer_sha256"]
    assert ready["sealed_artifact_binding_sha256"] == binding[
        "binding_sha256"
    ]
    assert receipt["installation_receipt_sha256"] == ready[
        "installation_receipt_sha256"
    ]
    with pytest.raises(
        installer.ProductionStorageInstallerError,
        match="production_storage_installer_storage_invalid",
    ):
        installer.attest_owner_state_root(
            RELEASE,
            sealed_artifact_binding=binding,
            state_root=state_root,
            expected_uid=os.getuid() + 1,
            expected_gid=os.getgid(),
        )


def test_guest_installer_proves_installed_digest_without_sudoers(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o755)
    entrypoint = tmp_path / "production-storage-growth-guest"
    installation_root = tmp_path / "guest-installation"
    receipt = installation_root / "installation.json"
    sudoers = tmp_path / "production-storage-growth.sudoers"
    request = installer.build_guest_install_request(RELEASE)
    interpreter = Path(os.path.realpath(installer.GUEST_INTERPRETER, strict=True))
    interpreter_state = interpreter.stat()

    def run_readiness(
        argv,
        *,
        input: bytes,
        stdout,
        stderr,
        check: bool,
        timeout: int,
    ):
        assert argv == (str(entrypoint),)
        assert stdout == subprocess.PIPE
        assert stderr == subprocess.DEVNULL
        assert check is False
        assert timeout == 30
        frame = installer.decode_canonical_json(input)
        assert frame["operation"] == "readiness"
        document = guest.FixedProductionStorageGuest(
            entrypoint=entrypoint,
            installation_receipt=receipt,
            sudoers_path=sudoers,
                expected_uid=os.getuid(),
                expected_gid=os.getgid(),
                expected_interpreter_uid=interpreter_state.st_uid,
                expected_interpreter_gid=interpreter_state.st_gid,
        ).readiness()
        unsigned = {
            "schema": guest.RESPONSE_SCHEMA,
            "operation": "readiness",
            "ok": True,
            "document": document,
        }
        response = {
            **unsigned,
            "response_sha256": protocol.sha256_json(unsigned),
        }
        return subprocess.CompletedProcess(
            argv,
            0,
            protocol.canonical_json_bytes(response),
            b"",
        )

    ready = installer.install_guest(
        request,
        source=installer.GUEST_SOURCE,
        entrypoint=entrypoint,
        installation_root=installation_root,
        installation_receipt=receipt,
        sudoers_path=sudoers,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        expected_interpreter_uid=interpreter_state.st_uid,
        expected_interpreter_gid=interpreter_state.st_gid,
        effective_uid=lambda: 0,
        wall_clock=lambda: 2_000_000_000,
        runner=run_readiness,
    )
    assert ready["entrypoint_sha256"] == hashlib.sha256(
        entrypoint.read_bytes()
    ).hexdigest()
    assert ready["sudoers_required"] is False
    assert ready["sudoers_absent"] is True
    assert entrypoint.read_bytes().startswith(b"#!/usr/bin/python3\n")
    assert ready["interpreter_path"] == "/usr/bin/python3"
    assert ready["interpreter_sha256"] == hashlib.sha256(
        interpreter.read_bytes()
    ).hexdigest()
    assert not sudoers.exists()

    installed = json.loads(receipt.read_text())
    original_receipt = receipt.read_bytes()
    installed["interpreter_sha256"] = "f" * 64
    unsigned_installed = {
        name: item
        for name, item in installed.items()
        if name != "installation_receipt_sha256"
    }
    installed["installation_receipt_sha256"] = protocol.sha256_json(
        unsigned_installed
    )
    receipt.write_bytes(protocol.canonical_json_bytes(installed) + b"\n")
    with pytest.raises(
        guest.ProductionStorageGuestError,
        match="production_storage_guest_readiness_invalid",
    ):
        guest.FixedProductionStorageGuest(
            entrypoint=entrypoint,
            installation_receipt=receipt,
            sudoers_path=sudoers,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
            expected_interpreter_uid=interpreter_state.st_uid,
            expected_interpreter_gid=interpreter_state.st_gid,
        ).readiness()
    receipt.write_bytes(original_receipt)

    with pytest.raises(
        guest.ProductionStorageGuestError,
        match="production_storage_guest_readiness_invalid",
    ):
        guest.FixedProductionStorageGuest(
            entrypoint=entrypoint,
            installation_receipt=receipt,
            sudoers_path=sudoers,
            expected_uid=os.getuid(),
            expected_gid=os.getgid() + 1,
            expected_interpreter_uid=interpreter_state.st_uid,
            expected_interpreter_gid=interpreter_state.st_gid,
        ).readiness()


def test_guest_boundary_runs_only_fixed_observe_and_growth_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: BOOT_ID)
    calls: list[tuple[str, ...]] = []
    state = {
        "partition": 53_685_993_472,
        "filesystem": 52_591_026_176,
    }

    def runner(argv: tuple[str, ...]) -> bytes:
        calls.append(tuple(argv))
        if argv[0] == "/usr/bin/findmnt":
            return json.dumps({
                "filesystems": [
                    {
                        "source": "/dev/sda1",
                        "fstype": "ext4",
                        "target": "/",
                        "avail": 6_000_000_000,
                        "size": state["filesystem"],
                    }
                ]
            }).encode()
        if argv[0] == "/usr/bin/lsblk":
            return json.dumps({
                "blockdevices": [
                    {
                        "path": "/dev/sda1",
                        "pkname": "sda",
                        "partn": 1,
                        "size": state["partition"],
                    }
                ]
            }).encode()
        if argv[0] == "/usr/sbin/blockdev":
            return b"107374182400\n"
        if "/usr/bin/growpart" in argv:
            state["partition"] = 107_373_084_672
        if "/usr/sbin/resize2fs" in argv:
            state["filesystem"] = 105_200_000_000
        return b""

    boundary = guest.FixedProductionStorageGuest(runner=runner)
    observed = boundary.observe()
    grown = boundary.grow(idempotency_key_sha256="a" * 64)
    assert observed["root_source"] == "/dev/sda1"
    assert grown["completed"] is True
    mutation_calls = [
        item
        for item in calls
        if item[0] in {"/usr/bin/growpart", "/usr/sbin/resize2fs"}
    ]
    assert mutation_calls == [
        (
            "/usr/bin/growpart",
            "/dev/sda",
            "1",
        ),
        (
            "/usr/sbin/resize2fs",
            "/dev/sda1",
        ),
    ]


def test_guest_recovery_skips_growpart_after_accepted_boundary_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: BOOT_ID)
    state = {
        "partition": 53_685_993_472,
        "filesystem": 52_591_026_176,
        "fail_after_growpart": True,
    }
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...]) -> bytes:
        calls.append(tuple(argv))
        if argv[0] == "/usr/bin/findmnt":
            return json.dumps({
                "filesystems": [{
                    "source": "/dev/sda1",
                    "fstype": "ext4",
                    "target": "/",
                    "avail": 6_000_000_000,
                    "size": state["filesystem"],
                }]
            }).encode()
        if argv[0] == "/usr/bin/lsblk":
            return json.dumps({
                "blockdevices": [{
                    "path": "/dev/sda1",
                    "pkname": "sda",
                    "partn": 1,
                    "size": state["partition"],
                }]
            }).encode()
        if argv[0] == "/usr/sbin/blockdev":
            return b"107374182400\n"
        if "/usr/bin/growpart" in argv:
            state["partition"] = 107_373_084_672
            if state["fail_after_growpart"]:
                state["fail_after_growpart"] = False
                raise RuntimeError("lost boundary after accepted growpart")
        if "/usr/sbin/resize2fs" in argv:
            state["filesystem"] = 105_200_000_000
        return b""

    boundary = guest.FixedProductionStorageGuest(runner=runner)
    with pytest.raises(RuntimeError, match="lost boundary"):
        boundary.grow(idempotency_key_sha256="a" * 64)
    recovered = boundary.grow(idempotency_key_sha256="a" * 64)
    assert recovered["completed"] is True
    assert sum("/usr/bin/growpart" in item for item in calls) == 1
    assert sum("/usr/sbin/resize2fs" in item for item in calls) == 1


def test_iap_guest_client_exposes_only_fixed_frame_invocation() -> None:
    calls: list[bytes] = []

    def invoke_fixed_guest(stdin: bytes) -> bytes:
        calls.append(stdin)
        frame = protocol.decode_canonical_json(stdin)
        unsigned = {
            "schema": guest.RESPONSE_SCHEMA,
            "operation": frame["operation"],
            "ok": True,
            "document": {"boot_id": BOOT_ID},
        }
        return protocol.canonical_json_bytes({
            **unsigned,
            "response_sha256": protocol.sha256_json(unsigned),
        })

    client = adapter.FixedProductionIapGuestClient(
        invoke_fixed_guest=invoke_fixed_guest
    )
    assert client.observe() == {"boot_id": BOOT_ID}
    assert protocol.decode_canonical_json(calls[0])["document"] == {}


class _Response:
    def __init__(self, value: dict) -> None:
        self._value = value

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._value).encode()


def test_compute_resize_uses_exact_rest_target_and_request_id() -> None:
    requests = []
    responses = iter([
        {
            "name": "operation-1",
            "operationType": "resize",
            "targetId": contract.DISK_ID,
        },
        {
            "id": contract.DISK_ID,
            "sizeGb": str(contract.TARGET_SIZE_GB),
            "status": "READY",
        },
    ])

    def urlopen(request, *, timeout: int):
        assert timeout == 30
        requests.append(request)
        return _Response(next(responses))

    client = adapter.FixedProductionComputeClient(
        token_provider=lambda: "token",
        account_provider=lambda: contract.AUTHENTICATED_ACCOUNT,
        urlopen=urlopen,
        sleep=lambda _seconds: None,
    )
    result = client.resize_once(
        provider_request_id="00000000-0000-0000-0000-000000000001"
    )
    assert result["accepted"] is True
    assert requests[0].method == "POST"
    assert (
        f"/disks/{contract.DISK_NAME}/resize?requestId="
        "00000000-0000-0000-0000-000000000001"
    ) in requests[0].full_url
    assert json.loads(requests[0].data) == {"sizeGb": "100"}
    assert f"/disks/{contract.DISK_NAME}" in requests[1].full_url


def test_compute_client_rejects_wrong_active_account_before_network() -> None:
    client = adapter.FixedProductionComputeClient(
        token_provider=lambda: "token",
        account_provider=lambda: "wrong@example.com",
        urlopen=lambda *_args, **_kwargs: pytest.fail("network was reached"),
    )
    with pytest.raises(
        adapter.ProductionStorageAdapterError,
        match="production_storage_compute_boundary_invalid",
    ):
        client.get_disk()
