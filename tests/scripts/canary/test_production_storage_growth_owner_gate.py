from __future__ import annotations

import base64
import copy
import hashlib
import json
import multiprocessing
import os
import subprocess
import struct
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import passkey_v2_production_storage_growth as passkey
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import passkey_v2_service as service
from scripts.canary import full_canary_owner_launcher as owner_launcher
from scripts.canary import owner_gate_activation_seal as activation_seal
from scripts.canary import owner_gate_package
from scripts.canary import production_storage_growth_contract as contract
from scripts.canary import production_storage_growth_executor as executor
from scripts.canary import production_storage_growth_installer as installer
from scripts.canary import production_storage_growth_state_helper as state_helper
from scripts.canary.passkey_v2_signer import ReceiptSigner


NOW = 2_000_000_000
RELEASE = "a" * 40
BOOT_ID = "baf2a4ac-6450-4da8-a6de-d89a2f0c1250"


def _ssh_wire(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _sshsig(
    key: Ed25519PrivateKey,
    message: bytes,
    *,
    namespace: str,
) -> str:
    algorithm = b"ssh-ed25519"
    namespace_raw = namespace.encode("ascii")
    signed = (
        b"SSHSIG"
        + _ssh_wire(namespace_raw)
        + _ssh_wire(b"")
        + _ssh_wire(b"sha512")
        + _ssh_wire(hashlib.sha512(message).digest())
    )
    public = key.public_key().public_bytes_raw()
    envelope = (
        b"SSHSIG"
        + struct.pack(">I", 1)
        + _ssh_wire(_ssh_wire(algorithm) + _ssh_wire(public))
        + _ssh_wire(namespace_raw)
        + _ssh_wire(b"")
        + _ssh_wire(b"sha512")
        + _ssh_wire(_ssh_wire(algorithm) + _ssh_wire(key.sign(signed)))
    )
    body = base64.b64encode(envelope).decode("ascii")
    lines = [body[index : index + 70] for index in range(0, len(body), 70)]
    return (
        "-----BEGIN SSH SIGNATURE-----\n"
        + "\n".join(lines)
        + "\n-----END SSH SIGNATURE-----\n"
    )


def _external_iam_receipt(
    key: Ed25519PrivateKey,
    *,
    collected_at_unix: int = NOW,
) -> dict:
    key_id = hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest()
    unsigned = {
        "schema": passkey.EXTERNAL_IAM_RECEIPT_SCHEMA,
        "account": contract.AUTHENTICATED_ACCOUNT,
        "owner_subject_sha256": "1" * 64,
        "project": contract.PROJECT,
        "project_number": passkey.EXTERNAL_IAM_PROJECT_NUMBER,
        "zone": contract.ZONE,
        "instance_name": contract.INSTANCE_NAME,
        "instance_id": contract.INSTANCE_ID,
        "disk_name": contract.DISK_NAME,
        "disk_id": contract.DISK_ID,
        "permissions": {
            name: "GRANTED" for name in passkey.EXTERNAL_IAM_PERMISSIONS
        },
        "authorization_snapshot_sha256": "2" * 64,
        "instance_evidence_sha256": "3" * 64,
        "disk_evidence_sha256": "4" * 64,
        "collected_at_unix": collected_at_unix,
        "expires_at_unix": (
            collected_at_unix + passkey.EXTERNAL_IAM_TTL_SECONDS
        ),
        "owner_public_key_id": key_id,
    }
    signed = {**unsigned, "receipt_sha256": protocol.sha256_json(unsigned)}
    return {
        **signed,
        "signature_sshsig": _sshsig(
            key,
            protocol.canonical_json_bytes(signed),
            namespace=passkey.EXTERNAL_IAM_SSHSIG_NAMESPACE,
        ),
    }


def _observation(state: str = "source", *, collected_at_unix: int = NOW) -> dict:
    sizes = {
        "source": {
            "size_gb": 50,
            "disk_size_bytes": 53_687_091_200,
            "partition_size_bytes": 53_685_993_472,
            "filesystem_size_bytes": 52_591_026_176,
            "available_bytes": 4_261_064_704,
        },
        "partial": {
            "size_gb": 100,
            "disk_size_bytes": 107_374_182_400,
            "partition_size_bytes": 53_685_993_472,
            "filesystem_size_bytes": 52_591_026_176,
            "available_bytes": 4_261_064_704,
        },
        "target": {
            "size_gb": 100,
            "disk_size_bytes": 107_374_182_400,
            "partition_size_bytes": 107_373_084_672,
            "filesystem_size_bytes": 105_200_000_000,
            "available_bytes": 56_000_000_000,
        },
        "low_available": {
            "size_gb": 100,
            "disk_size_bytes": 107_374_182_400,
            "partition_size_bytes": 107_373_084_672,
            "filesystem_size_bytes": 105_200_000_000,
            "available_bytes": contract.MINIMUM_POSTFLIGHT_AVAILABLE_BYTES - 1,
        },
    }[state]
    return dict(
        contract.build_observation(
            collected_at_unix=collected_at_unix,
            authenticated_account=contract.AUTHENTICATED_ACCOUNT,
            impersonated_service_account=None,
            project=contract.PROJECT,
            zone=contract.ZONE,
            instance={
                "name": contract.INSTANCE_NAME,
                "id": contract.INSTANCE_ID,
                "status": "RUNNING",
                "zone": contract.ZONE,
                "self_link": contract.INSTANCE_SELF_LINK,
                "boot_disk_count": 1,
            },
            disk={
                "name": contract.DISK_NAME,
                "id": contract.DISK_ID,
                "type": contract.DISK_TYPE,
                "size_gb": sizes["size_gb"],
                "zone": contract.ZONE,
                "self_link": contract.DISK_SELF_LINK,
                "users": [contract.INSTANCE_SELF_LINK],
                "status": "READY",
                "source_image_project": "debian-cloud",
                "source_image_name": "debian-12-bookworm-v20260609",
            },
            boot_attachment={
                "boot": True,
                "auto_delete": True,
                "device_name": "persistent-disk-0",
                "mode": "READ_WRITE",
                "type": "PERSISTENT",
                "source": contract.DISK_SELF_LINK,
            },
            guest={
                "boot_id": BOOT_ID,
                "root_source": "/dev/sda1",
                "root_parent": "/dev/sda",
                "root_partition_number": 1,
                "root_filesystem": "ext4",
                "mountpoint": "/",
                "disk_size_bytes": sizes["disk_size_bytes"],
                "partition_size_bytes": sizes["partition_size_bytes"],
                "filesystem_size_bytes": sizes["filesystem_size_bytes"],
                "available_bytes": sizes["available_bytes"],
            },
        )
    )


def _artifact_attestation(marker: str = "1") -> dict:
    repository = Path(contract.__file__).parents[2]
    artifacts = {}
    for name, relative in contract.RUNTIME_ARTIFACT_RELATIVES.items():
        payload = (repository / relative).read_bytes()
        artifacts[name] = {
            "release_relative": relative,
            "sha256": (
                hashlib.sha256(payload).hexdigest()
                if marker == "1"
                else marker * 64
            ),
            "size": len(payload),
        }
    unsigned = {
        "schema": contract.RUNTIME_ARTIFACT_ATTESTATION_SCHEMA,
        "release_revision": RELEASE,
        "owner_support_manifest_sha256": "a" * 64,
        "owner_support_source_tree_oid": "b" * 40,
        "artifacts": artifacts,
    }
    return {
        **unsigned,
        "attestation_sha256": protocol.sha256_json(unsigned),
    }


def _plan() -> dict:
    return dict(
        contract.build_plan(
            source_preflight=_observation(),
            release_revision=RELEASE,
            runtime_artifact_attestation=_artifact_attestation(),
            now_unix=NOW,
        )
    )


@pytest.mark.parametrize("collection_seconds", (-1, 1, 301))
def test_owner_route_collects_and_rechecks_fixed_source_before_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collection_seconds: int,
) -> None:
    source = _observation(collected_at_unix=NOW)
    attachment = source["boot_attachment"]
    instance = {
        "disks": [{
            "boot": attachment["boot"],
            "autoDelete": attachment["auto_delete"],
            "deviceName": attachment["device_name"],
            "mode": attachment["mode"],
            "type": attachment["type"],
            "source": attachment["source"],
        }],
        "id": contract.INSTANCE_ID,
        "name": contract.INSTANCE_NAME,
        "selfLink": contract.INSTANCE_SELF_LINK,
        "status": "RUNNING",
        "zone": (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{contract.PROJECT}/zones/{contract.ZONE}"
        ),
    }
    disk = {
        "id": contract.DISK_ID,
        "name": contract.DISK_NAME,
        "selfLink": contract.DISK_SELF_LINK,
        "sizeGb": str(contract.SOURCE_SIZE_GB),
        "sourceImage": (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{contract.SOURCE_IMAGE_PROJECT}/global/images/"
            f"{contract.SOURCE_IMAGE_NAME}"
        ),
        "status": "READY",
        "type": (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{contract.PROJECT}/zones/{contract.ZONE}/diskTypes/"
            f"{contract.DISK_TYPE}"
        ),
        "users": [contract.INSTANCE_SELF_LINK],
        "zone": (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{contract.PROJECT}/zones/{contract.ZONE}"
        ),
    }
    calls: list[str] = []

    class Identity:
        def account_for_read_only_preflight(self) -> str:
            calls.append("identity")
            return contract.AUTHENTICATED_ACCOUNT

        def require_stable(self) -> None:
            calls.append("identity_stable")

    class Boundary:
        def request(self, **_kwargs):  # pragma: no cover - unused
            raise AssertionError("request is not part of source collection")

        def consume(self, **_kwargs):  # pragma: no cover - unused
            raise AssertionError("consume is not part of source collection")

    class ProductionTransport:
        def _authorization_snapshot(self, account: str) -> tuple[str, ...]:
            assert account == contract.AUTHENTICATED_ACCOUNT
            calls.append("authority")
            return ("fixed-authority",)

        def _run_read_only_gcloud_json(self, argv):
            assert "--quiet" in argv
            assert f"--account={contract.AUTHENTICATED_ACCOUNT}" in argv
            if argv[1] == "instances":
                calls.append("get_instance")
                return copy.deepcopy(instance)
            if argv[1] == "disks":
                calls.append("get_disk")
                return copy.deepcopy(disk)
            raise AssertionError("unexpected cloud command")

        def _run_remote_input(self, _argv, *, input_bytes: bytes, **_kwargs):
            request = protocol.decode_canonical_json(input_bytes)
            operation = request["operation"]
            calls.append(f"guest_{operation}")
            if operation == "readiness":
                install = installer.build_guest_install_request(RELEASE)
                unsigned_readiness = {
                    "schema": installer.GUEST_READINESS_SCHEMA,
                    "release_sha": RELEASE,
                    "entrypoint": str(installer.GUEST_ENTRYPOINT),
                    "entrypoint_sha256": install["guest_source_sha256"],
                    "entrypoint_uid": 0,
                    "entrypoint_gid": 0,
                    "entrypoint_mode": "0755",
                    "entrypoint_link_count": 1,
                    "installer_sha256": install["installer_sha256"],
                    "interpreter_path": "/usr/bin/python3",
                    "interpreter_resolved_path": "/usr/bin/python3.11",
                    "interpreter_sha256": "8" * 64,
                    "installation_receipt_sha256": "9" * 64,
                    "sudoers_path": str(installer.GUEST_SUDOERS_PATH),
                    "sudoers_required": False,
                    "sudoers_absent": True,
                    "root_transport_required": True,
                    "ready": True,
                }
                document = {
                    **unsigned_readiness,
                    "readiness_sha256": protocol.sha256_json(
                        unsigned_readiness
                    ),
                }
            elif operation == "observe":
                document = source["guest"]
            else:
                raise AssertionError("mutation guest operation reached")
            unsigned = {
                "schema": "muncho-production-storage-growth-guest-response.v1",
                "operation": operation,
                "ok": True,
                "document": document,
            }
            return subprocess.CompletedProcess(
                (),
                0,
                protocol.canonical_json_bytes({
                    **unsigned,
                    "response_sha256": protocol.sha256_json(unsigned),
                }),
                b"",
            )

    monkeypatch.setattr(
        installer,
        "attest_owner_state_root",
        lambda *_args, **_kwargs: {"ready": True},
    )
    artifacts = _artifact_attestation()
    moments = iter((NOW, NOW + collection_seconds))
    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=Identity(),
        passkey_boundary=Boundary(),
        production_transport=ProductionTransport(),
        runtime_artifact_attestor=lambda: (
            calls.append("artifacts") or artifacts
        ),
        state_root=tmp_path,
        wall_clock=lambda: next(moments),
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )

    if (
        collection_seconds < 0
        or collection_seconds > contract.PREFLIGHT_MAX_AGE_SECONDS
    ):
        with pytest.raises(
            owner_launcher.OwnerLauncherError,
            match="production_storage_source_preflight_invalid",
        ):
            route.collect_source_preflight()
        assert "identity_stable" not in calls
        return

    observed = route.collect_source_preflight()

    assert observed == source
    assert calls == [
        "artifacts",
        "identity",
        "authority",
        "guest_readiness",
        "get_instance",
        "get_disk",
        "guest_observe",
        "get_instance",
        "get_disk",
        "authority",
        "identity_stable",
        "artifacts",
    ]


def test_owner_route_source_collection_rejects_cloud_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    reads = 0

    class Identity:
        def account_for_read_only_preflight(self) -> str:
            return contract.AUTHENTICATED_ACCOUNT

        def require_stable(self) -> None:  # pragma: no cover - drift first
            raise AssertionError("identity stability reached after cloud drift")

    class Boundary:
        request = lambda self, **_kwargs: None
        consume = lambda self, **_kwargs: None

    class ProductionTransport:
        def _authorization_snapshot(self, _account: str) -> tuple[str, ...]:
            return ("fixed-authority",)

        def _run_read_only_gcloud_json(self, argv):
            nonlocal reads
            reads += 1
            if argv[1] == "instances":
                observation = plan["source_preflight"]
                attachment = observation["boot_attachment"]
                return {
                    "disks": [{
                        "boot": attachment["boot"],
                        "autoDelete": attachment["auto_delete"],
                        "deviceName": attachment["device_name"],
                        "mode": attachment["mode"],
                        "type": attachment["type"],
                        "source": attachment["source"],
                    }],
                    "id": contract.INSTANCE_ID,
                    "name": contract.INSTANCE_NAME,
                    "selfLink": contract.INSTANCE_SELF_LINK,
                    "status": "RUNNING",
                    "zone": contract.ZONE,
                }
            return {
                "id": contract.DISK_ID,
                "name": contract.DISK_NAME,
                "selfLink": contract.DISK_SELF_LINK,
                "sizeGb": str(
                    contract.TARGET_SIZE_GB
                    if reads > 3
                    else contract.SOURCE_SIZE_GB
                ),
                "sourceImage": (
                    "https://www.googleapis.com/compute/v1/projects/"
                    f"{contract.SOURCE_IMAGE_PROJECT}/global/images/"
                    f"{contract.SOURCE_IMAGE_NAME}"
                ),
                "status": "READY",
                "type": contract.DISK_TYPE,
                "users": [contract.INSTANCE_SELF_LINK],
                "zone": contract.ZONE,
            }

        def _run_remote_input(self, _argv, *, input_bytes: bytes, **_kwargs):
            request = protocol.decode_canonical_json(input_bytes)
            operation = request["operation"]
            if operation == "readiness":
                install = installer.build_guest_install_request(RELEASE)
                unsigned_document = {
                    "schema": installer.GUEST_READINESS_SCHEMA,
                    "release_sha": RELEASE,
                    "entrypoint": str(installer.GUEST_ENTRYPOINT),
                    "entrypoint_sha256": install["guest_source_sha256"],
                    "entrypoint_uid": 0,
                    "entrypoint_gid": 0,
                    "entrypoint_mode": "0755",
                    "entrypoint_link_count": 1,
                    "installer_sha256": install["installer_sha256"],
                    "interpreter_path": "/usr/bin/python3",
                    "interpreter_resolved_path": "/usr/bin/python3.11",
                    "interpreter_sha256": "8" * 64,
                    "installation_receipt_sha256": "9" * 64,
                    "sudoers_path": str(installer.GUEST_SUDOERS_PATH),
                    "sudoers_required": False,
                    "sudoers_absent": True,
                    "root_transport_required": True,
                    "ready": True,
                }
                document = {
                    **unsigned_document,
                    "readiness_sha256": protocol.sha256_json(
                        unsigned_document
                    ),
                }
            else:
                document = plan["source_preflight"]["guest"]
            unsigned = {
                "schema": "muncho-production-storage-growth-guest-response.v1",
                "operation": operation,
                "ok": True,
                "document": document,
            }
            return subprocess.CompletedProcess(
                (),
                0,
                protocol.canonical_json_bytes({
                    **unsigned,
                    "response_sha256": protocol.sha256_json(unsigned),
                }),
                b"",
            )

    monkeypatch.setattr(
        installer,
        "attest_owner_state_root",
        lambda *_args, **_kwargs: {"ready": True},
    )
    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=Identity(),
        passkey_boundary=Boundary(),
        production_transport=ProductionTransport(),
        runtime_artifact_attestor=_artifact_attestation,
        state_root=tmp_path,
        wall_clock=lambda: NOW,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )

    with pytest.raises(
        owner_launcher.OwnerLauncherError,
        match="production_storage_source_preflight_invalid",
    ):
        route.collect_source_preflight()


def test_external_iam_receipt_requires_exact_signed_permissions_and_freshness() -> None:
    key = Ed25519PrivateKey.generate()
    receipt = _external_iam_receipt(key)
    key_hex = key.public_key().public_bytes_raw().hex()
    key_id = hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest()
    checked = passkey.validate_external_iam_receipt(
        receipt,
        now_unix=NOW + 10,
        expected_public_key_ed25519_hex=key_hex,
        expected_owner_key_id=key_id,
    )
    assert checked["permissions"] == {
        name: "GRANTED" for name in passkey.EXTERNAL_IAM_PERMISSIONS
    }

    denied = copy.deepcopy(receipt)
    denied["permissions"]["compute.disks.resize"] = "DENIED"
    with pytest.raises(
        passkey.ProductionStoragePasskeyError,
        match="production_storage_external_iam_invalid",
    ):
        passkey.validate_external_iam_receipt(
            denied,
            now_unix=NOW + 10,
            expected_public_key_ed25519_hex=key_hex,
            expected_owner_key_id=key_id,
        )

    with pytest.raises(
        passkey.ProductionStoragePasskeyError,
        match="production_storage_external_iam_invalid",
    ):
        passkey.validate_external_iam_receipt(
            receipt,
            now_unix=NOW + passkey.EXTERNAL_IAM_TTL_SECONDS,
            minimum_remaining_seconds=0,
            expected_public_key_ed25519_hex=key_hex,
            expected_owner_key_id=key_id,
        )


def _signed_bundle() -> tuple[dict, dict, ReceiptSigner]:
    plan = _plan()
    action = passkey.build_action_envelope(
        growth_plan=plan,
        authorization_nonce_sha256="5" * 64,
        authority_manifest_sha256="6" * 64,
        authority_host_receipt_sha256="7" * 64,
        external_iam_receipt_sha256="8" * 64,
        prior_authoritative_receipt_sha256="9" * 64,
        prior_event_head_sha256=protocol.GENESIS_JOURNAL_HEAD_SHA256,
        issued_at_unix=NOW,
    )
    challenge = protocol.build_challenge_record(
        envelope=action,
        challenge_id="C" * 32,
        challenge_b64url=base64
        .urlsafe_b64encode(b"x" * 32)
        .rstrip(b"=")
        .decode("ascii"),
        rp_id=protocol.PRODUCTION_RP_ID,
        origin=protocol.PRODUCTION_ORIGIN,
        created_at_unix=NOW + 1,
    )
    grant = protocol.build_passkey_grant(
        envelope=action,
        challenge=challenge,
        grant_id="G" * 32,
        approver_discord_user_id=passkey.OWNER_DISCORD_USER_ID,
        credential_id_sha256="a" * 64,
        credential_record_sha256="b" * 64,
        credential_migration_receipt_sha256="c" * 64,
        assertion_verification_sha256="d" * 64,
        credential_sign_count=4,
        credential_backed_up=True,
        granted_at_unix=NOW + 2,
    )
    runtime = protocol.build_runtime_binding(
        executor_release_sha=plan["release_revision"],
        executor_plan_sha256=plan["plan_sha256"],
        executor_binary_sha256=plan["executor_binary_sha256"],
        mutation_wrapper_sha256=plan["mutation_wrapper_sha256"],
        remote_transport_sha256=plan["remote_transport_sha256"],
    )
    signer = ReceiptSigner(Ed25519PrivateKey.generate())
    receipt = signer.sign(
        protocol.build_authorization_receipt_unsigned(
            envelope=action,
            grant=grant,
            challenge=challenge,
            runtime_binding=runtime,
            consume_attempt_id="e" * 64,
            consumed_at_unix=NOW + 3,
            prior_journal_head_sha256="f" * 64,
            receipt_public_key_id=signer.key_id,
        )
    )
    bundle = passkey.build_authorization_bundle(
        growth_plan=plan,
        action_envelope=action,
        challenge_record=challenge,
        grant_record=grant,
        authorization_receipt=receipt,
        receipt_public_key=signer.public_key,
    )
    return dict(bundle), plan, signer


class FakeTransport:
    def __init__(self, state: str = "source") -> None:
        self.state = state
        self.now = NOW + 4
        self.calls: list[tuple[str, str | None]] = []
        self.fail_after_resize = False

    def observe_exact_target(self) -> dict:
        self.calls.append(("observe", None))
        return _observation(self.state, collected_at_unix=self.now)

    def resize_exact_disk_once(self, *, provider_request_id: str) -> dict:
        self.calls.append(("resize", provider_request_id))
        self.state = "partial"
        if self.fail_after_resize:
            self.fail_after_resize = False
            raise RuntimeError("simulated transport loss after accepted resize")
        return {"accepted": True, "provider_request_id": provider_request_id}

    def grow_exact_root_online(self, *, idempotency_key_sha256: str) -> dict:
        self.calls.append(("grow", idempotency_key_sha256))
        self.state = "target"
        return {"completed": True, "idempotency_key": idempotency_key_sha256}


def _runner(
    tmp_path: Path,
    transport: FakeTransport,
    signer: ReceiptSigner,
    plan: dict | None = None,
) -> executor.ProductionStorageGrowthExecutor:
    plan = plan or _plan()
    runtime = protocol.build_runtime_binding(
        executor_release_sha=plan["release_revision"],
        executor_plan_sha256=plan["plan_sha256"],
        executor_binary_sha256=plan["executor_binary_sha256"],
        mutation_wrapper_sha256=plan["mutation_wrapper_sha256"],
        remote_transport_sha256=plan["remote_transport_sha256"],
    )
    return executor.ProductionStorageGrowthExecutor(
        state_root=tmp_path,
        transport=transport,
        receipt_public_key=signer.public_key,
        runtime_binding=runtime,
        read_only_collector_sha256=plan["read_only_collector_sha256"],
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        wall_clock=lambda: transport.now,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )


def test_root_state_helper_independently_validates_exact_authorization() -> None:
    bundle, plan, signer = _signed_bundle()
    public_hex = signer.public_key.public_bytes_raw().hex()
    checked = state_helper.validate_authorization(
        bundle,
        plan,
        public_hex,
        NOW + 4,
        require_current=True,
    )
    assert checked["bundle_sha256"] == bundle["bundle_sha256"]
    forged = copy.deepcopy(bundle)
    forged["authorization_receipt"]["caller_public_key"] = public_hex
    forged_unsigned = dict(forged)
    forged_unsigned.pop("bundle_sha256")
    forged["bundle_sha256"] = protocol.sha256_json(forged_unsigned)
    with pytest.raises(
        state_helper.StateHelperError,
        match="production_storage_authorization_invalid",
    ):
        state_helper.validate_authorization(
            forged,
            plan,
            public_hex,
            NOW + 4,
            require_current=True,
        )


def test_exact_plan_is_production_specific_and_has_no_shrink_fiction() -> None:
    plan = _plan()
    assert plan["project"] == "adventico-ai-platform"
    assert plan["instance_id"] == "1094477181810932795"
    assert plan["disk_id"] == "8330339521755118650"
    assert (plan["source_size_gb"], plan["target_size_gb"]) == (50, 100)
    assert plan["maximum_provider_resize_operations"] == 1
    assert plan["rollback_by_shrink_allowed"] is False
    assert plan["forward_recovery_required"] is True
    assert plan["minimum_postflight_available_bytes"] == 5 * 1024**3

    tampered = copy.deepcopy(plan)
    tampered["idempotency_key_sha256"] = "f" * 64
    tampered["provider_request_id"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    tampered["plan_sha256"] = protocol.sha256_json({
        name: item for name, item in tampered.items() if name != "plan_sha256"
    })
    with pytest.raises(contract.ProductionStorageGrowthError):
        contract.validate_plan(tampered)


def test_signed_owner_gate_package_and_activation_close_over_handler() -> None:
    required = {
        "scripts/canary/passkey_v2_production_storage_growth.py",
        "scripts/canary/passkey_v2_service.py",
        "scripts/canary/production_storage_growth_contract.py",
    }
    mutation_authority = {
        "scripts/canary/production_storage_growth_adapter.py",
        "scripts/canary/production_storage_growth_executor.py",
        "scripts/canary/production_storage_growth_guest.py",
    }
    assert required.issubset(set(owner_gate_package.ROOT_RUNTIME_FILES))
    assert required.issubset(activation_seal._REQUIRED_ACTIVATION_PAYLOADS)
    assert mutation_authority.isdisjoint(owner_gate_package.ROOT_RUNTIME_FILES)
    assert mutation_authority.isdisjoint(
        activation_seal._REQUIRED_ACTIVATION_PAYLOADS
    )


def test_preflight_rejects_stale_or_wrong_exact_identity() -> None:
    stale = _observation(collected_at_unix=NOW - 301)
    with pytest.raises(contract.ProductionStorageGrowthError):
        contract.validate_observation(stale, now_unix=NOW)

    wrong = _observation()
    wrong["disk"]["id"] = "wrong"
    wrong["observation_sha256"] = protocol.sha256_json({
        name: item for name, item in wrong.items() if name != "observation_sha256"
    })
    with pytest.raises(contract.ProductionStorageGrowthError):
        contract.validate_observation(wrong, now_unix=NOW)

    plan = _plan()
    refreshed = _observation(collected_at_unix=NOW + 20)
    refreshed["guest"]["available_bytes"] -= 1024
    refreshed["observation_sha256"] = protocol.sha256_json({
        name: item for name, item in refreshed.items() if name != "observation_sha256"
    })
    assert (
        contract.classify_observation(refreshed, now_unix=NOW + 20, plan=plan)
        == "source"
    )


def test_authorization_binds_exact_plan_runtime_and_fresh_window() -> None:
    bundle, plan, signer = _signed_bundle()
    checked = passkey.validate_authorization_bundle(
        bundle,
        growth_plan=plan,
        receipt_public_key=signer.public_key,
        now_unix=NOW + 4,
        require_current=True,
    )
    facts = passkey.mechanical_approval_facts(checked["action_envelope"])
    assert facts["disk_id"] == contract.DISK_ID
    assert facts["provider_request_id"] == plan["provider_request_id"]
    assert facts["disk_shrink_available"] is False

    tampered = copy.deepcopy(bundle)
    tampered["authorization_receipt"]["runtime_binding"]["executor_binary_sha256"] = (
        "f" * 64
    )
    tampered["bundle_sha256"] = protocol.sha256_json({
        name: item for name, item in tampered.items() if name != "bundle_sha256"
    })
    with pytest.raises(passkey.ProductionStoragePasskeyError):
        passkey.validate_authorization_bundle(
            tampered,
            growth_plan=plan,
            receipt_public_key=signer.public_key,
            now_unix=NOW + 4,
            require_current=True,
        )


def test_executor_rejects_runtime_digest_mismatch_before_observation(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    wrong_runtime = protocol.build_runtime_binding(
        executor_release_sha=plan["release_revision"],
        executor_plan_sha256=plan["plan_sha256"],
        executor_binary_sha256="f" * 64,
        mutation_wrapper_sha256=plan["mutation_wrapper_sha256"],
        remote_transport_sha256=plan["remote_transport_sha256"],
    )
    runner = executor.ProductionStorageGrowthExecutor(
        state_root=tmp_path,
        transport=transport,
        receipt_public_key=signer.public_key,
        runtime_binding=wrong_runtime,
        read_only_collector_sha256=plan["read_only_collector_sha256"],
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        wall_clock=lambda: transport.now,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_runtime_binding_invalid",
    ):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )
    assert transport.calls == []


def test_executor_rejects_self_consistent_but_unobserved_artifact_hashes(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    runtime = protocol.build_runtime_binding(
        executor_release_sha=plan["release_revision"],
        executor_plan_sha256=plan["plan_sha256"],
        executor_binary_sha256=plan["executor_binary_sha256"],
        mutation_wrapper_sha256=plan["mutation_wrapper_sha256"],
        remote_transport_sha256=plan["remote_transport_sha256"],
    )
    runner = executor.ProductionStorageGrowthExecutor(
        state_root=tmp_path,
        transport=transport,
        receipt_public_key=signer.public_key,
        runtime_binding=runtime,
        read_only_collector_sha256=plan["read_only_collector_sha256"],
        runtime_artifact_attestor=lambda: _artifact_attestation("2"),
        wall_clock=lambda: transport.now,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )

    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_runtime_binding_invalid",
    ):
        runner.execute(growth_plan=plan, authorization_bundle=bundle)
    assert transport.calls == []


def test_owner_route_rejects_caller_self_consistent_runtime_hashes_before_passkey(
    tmp_path: Path,
) -> None:
    plan = _plan()
    calls: list[str] = []

    class Boundary:
        def request(self, **_kwargs):
            calls.append("passkey")
            raise AssertionError("passkey must not be reached")

        def consume(self, **_kwargs):  # pragma: no cover - unused
            raise AssertionError("consume must not be reached")

    class ProductionTransport:
        def _run_remote_input(self, *_args, **_kwargs):
            raise AssertionError("guest must not be reached")

    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=object(),
        passkey_boundary=Boundary(),
        production_transport=ProductionTransport(),
        runtime_artifact_attestor=lambda: _artifact_attestation("2"),
        state_root=tmp_path,
        wall_clock=lambda: NOW,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )
    with pytest.raises(
        owner_launcher.OwnerLauncherError,
        match="production_storage_runtime_artifact_changed",
    ):
        route.request(
            growth_plan=plan,
            authorization_nonce_sha256="5" * 64,
        )
    assert calls == []


def test_source_to_target_executes_one_resize_and_online_growth(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    result = _runner(tmp_path, transport, signer, plan).execute(
        growth_plan=plan,
        authorization_bundle=bundle,
    )
    assert result["state"] == "completed"
    assert result["mutations_performed_this_attempt"] == [
        "provider_disk_resize_50_to_100",
        "online_partition_and_ext4_growth",
    ]
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 1
    journal_path = tmp_path / f"{plan['plan_sha256']}.json"
    assert journal_path.exists()
    assert json.loads(journal_path.read_text())["prior_journal_head_sha256"] == (
        "f" * 64
    )


def test_each_post_mutation_observation_uses_current_wall_clock(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    original_resize = transport.resize_exact_disk_once
    original_grow = transport.grow_exact_root_online

    def delayed_resize(*, provider_request_id: str) -> dict:
        result = original_resize(provider_request_id=provider_request_id)
        transport.now += 700
        return result

    def delayed_grow(*, idempotency_key_sha256: str) -> dict:
        result = original_grow(idempotency_key_sha256=idempotency_key_sha256)
        transport.now += 700
        return result

    transport.resize_exact_disk_once = delayed_resize  # type: ignore[method-assign]
    transport.grow_exact_root_online = delayed_grow  # type: ignore[method-assign]
    result = _runner(tmp_path, transport, signer, plan).execute(
        growth_plan=plan,
        authorization_bundle=bundle,
    )
    assert result["state"] == "completed"
    journal = json.loads((tmp_path / f"{plan['plan_sha256']}.json").read_text())
    assert journal["completed_at_unix"] == NOW + 1_404
    assert journal["final_observation"]["collected_at_unix"] == NOW + 1_404


def test_crash_after_resize_recovers_forward_without_second_resize(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    transport.fail_after_resize = True
    runner = _runner(tmp_path, transport, signer, plan)
    with pytest.raises(RuntimeError, match="simulated transport loss"):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )
    assert transport.state == "partial"
    assert [name for name, _ in transport.calls].count("resize") == 1

    transport.now = NOW + 4_000
    result = runner.execute(
        growth_plan=plan,
        authorization_bundle=bundle,
    )
    assert result["state"] == "completed"
    assert result["recovered_from_started_journal"] is True
    assert result["mutations_performed_this_attempt"] == [
        "online_partition_and_ext4_growth"
    ]
    assert [name for name, _ in transport.calls].count("resize") == 1


def test_recovery_rejects_boot_identity_drift_without_more_mutation(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    transport.fail_after_resize = True
    runner = _runner(tmp_path, transport, signer, plan)
    with pytest.raises(RuntimeError, match="simulated transport loss"):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )

    def drifted_observation() -> dict:
        transport.calls.append(("observe", None))
        observed = _observation("partial", collected_at_unix=NOW + 5)
        observed["guest"]["boot_id"] = "12766eab-d474-405a-b1e6-f5afb10a37d3"
        observed["observation_sha256"] = protocol.sha256_json({
            name: item
            for name, item in observed.items()
            if name != "observation_sha256"
        })
        return observed

    transport.observe_exact_target = drifted_observation  # type: ignore[method-assign]
    transport.now = NOW + 5
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_observation_invalid",
    ):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 0


def test_partial_state_without_started_receipt_is_not_adopted(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport("partial")
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_unowned_partial_state",
    ):
        _runner(tmp_path, transport, signer, plan).execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )
    assert not (tmp_path / f"{plan['plan_sha256']}.json").exists()
    assert not any(name in {"resize", "grow"} for name, _ in transport.calls)


def test_completed_replay_is_read_only_and_tampered_journal_blocks(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, signer, plan)
    runner.execute(
        growth_plan=plan,
        authorization_bundle=bundle,
    )
    path = tmp_path / f"{plan['plan_sha256']}.json"
    original_journal = path.read_text()
    calls_before = list(transport.calls)
    replay = runner.execute(
        growth_plan=plan,
        authorization_bundle=bundle,
    )
    assert replay["mutations_performed_this_attempt"] == []
    assert transport.calls == calls_before

    journal = json.loads(path.read_text())
    journal["provider_request_id"] = "00000000-0000-0000-0000-000000000000"
    journal["journal_sha256"] = protocol.sha256_json({
        name: item for name, item in journal.items() if name != "journal_sha256"
    })
    path.write_text(json.dumps(journal, separators=(",", ":"), sort_keys=True))
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_journal_invalid",
    ):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )

    journal = json.loads(original_journal)
    journal["final_observation"] = _observation(
        "low_available", collected_at_unix=journal["completed_at_unix"]
    )
    journal["journal_sha256"] = protocol.sha256_json({
        name: item for name, item in journal.items() if name != "journal_sha256"
    })
    path.write_text(json.dumps(journal, separators=(",", ":"), sort_keys=True))
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_journal_invalid",
    ):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )


def test_postflight_available_bytes_threshold_is_mandatory(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()

    def low_growth(*, idempotency_key_sha256: str) -> dict:
        transport.calls.append(("grow", idempotency_key_sha256))
        transport.state = "low_available"
        return {"completed": True}

    transport.grow_exact_root_online = low_growth  # type: ignore[method-assign]
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_postflight_threshold_not_met",
    ):
        _runner(tmp_path, transport, signer, plan).execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 1


def _intake_frame(operation: str, document: dict) -> dict:
    unsigned = {
        "schema": passkey.REMOTE_FRAME_SCHEMA,
        "operation": operation,
        "release_sha": RELEASE,
        "document": document,
    }
    return {**unsigned, "frame_sha256": protocol.sha256_json(unsigned)}


class _UnusedExecutor:
    def call(self, *_args, **_kwargs) -> dict:
        raise AssertionError("production storage route reached generic executor")


class _AuthorityClient:
    def __init__(self, signer: ReceiptSigner) -> None:
        self.signer = signer
        self.action: dict | None = None
        self.challenge: dict | None = None
        self.grant: dict | None = None

    def call(self, operation: str, document: dict) -> dict:
        if operation == "create_request":
            self.action = dict(document["action_envelope"])
            self.challenge = dict(
                protocol.build_challenge_record(
                    envelope=self.action,
                    challenge_id="C" * 32,
                    challenge_b64url=base64
                    .urlsafe_b64encode(b"z" * 32)
                    .rstrip(b"=")
                    .decode("ascii"),
                    rp_id=protocol.PRODUCTION_RP_ID,
                    origin=protocol.PRODUCTION_ORIGIN,
                    created_at_unix=NOW + 1,
                )
            )
            self.grant = dict(
                protocol.build_passkey_grant(
                    envelope=self.action,
                    challenge=self.challenge,
                    grant_id="G" * 32,
                    approver_discord_user_id=passkey.OWNER_DISCORD_USER_ID,
                    credential_id_sha256="1" * 64,
                    credential_record_sha256="2" * 64,
                    credential_migration_receipt_sha256="3" * 64,
                    assertion_verification_sha256="4" * 64,
                    credential_sign_count=8,
                    credential_backed_up=True,
                    granted_at_unix=NOW + 2,
                )
            )
            return {
                "request_id": self.action["request_id"],
                "action_envelope_sha256": self.action["envelope_sha256"],
                "challenge_record_sha256": self.challenge["challenge_record_sha256"],
                "expires_at_unix": self.action["expires_at_unix"],
            }
        if operation == "render":
            assert self.action is not None
            return dict(service._render_authority_action(self.action))
        if operation == "consume":
            assert self.action is not None
            assert self.challenge is not None
            assert self.grant is not None
            receipt = self.signer.sign(
                protocol.build_authorization_receipt_unsigned(
                    envelope=self.action,
                    grant=self.grant,
                    challenge=self.challenge,
                    runtime_binding=document["runtime_binding"],
                    consume_attempt_id=document["consume_attempt_id"],
                    consumed_at_unix=NOW + 3,
                    prior_journal_head_sha256="7" * 64,
                    receipt_public_key_id=self.signer.key_id,
                )
            )
            return {
                "disposition": "authorized_once",
                "authorization_receipt": receipt,
                "action_envelope": self.action,
                "challenge_record": self.challenge,
                "grant_record": self.grant,
            }
        raise AssertionError(f"unexpected authority operation {operation}")


def test_service_routes_exact_production_storage_request_and_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    signer = ReceiptSigner(Ed25519PrivateKey.generate())
    authority = _AuthorityClient(signer)
    runtime = protocol.build_runtime_binding(
        executor_release_sha=RELEASE,
        executor_plan_sha256=plan["plan_sha256"],
        executor_binary_sha256=plan["executor_binary_sha256"],
        mutation_wrapper_sha256=plan["mutation_wrapper_sha256"],
        remote_transport_sha256=plan["remote_transport_sha256"],
    )

    def binding_loader(release_revision: str, supplied_plan: dict) -> tuple:
        assert release_revision == RELEASE
        assert supplied_plan == plan
        return runtime, "6" * 64, "7" * 64, signer.public_key

    iam = {"receipt_sha256": "8" * 64}
    monkeypatch.setattr(
        passkey,
        "validate_external_iam_receipt",
        lambda value, **_kwargs: value,
    )

    requested = service.handle_intake_frame(
        _intake_frame(
            "request_production_storage_growth",
            {
                "growth_plan": plan,
                "authorization_nonce_sha256": "5" * 64,
                "external_iam_receipt": iam,
            },
        ),
        authority_client=authority,
        executor_client=_UnusedExecutor(),
        release_revision=RELEASE,
        now_unix=NOW,
        production_storage_binding_loader=binding_loader,
    )
    consumed = service.handle_intake_frame(
        _intake_frame(
            "consume_production_storage_growth",
            {
                "growth_plan": plan,
                "request_id": requested["document"]["request_id"],
                "consume_attempt_id": "e" * 64,
                "external_iam_receipt": iam,
            },
        ),
        authority_client=authority,
        executor_client=_UnusedExecutor(),
        release_revision=RELEASE,
        now_unix=NOW + 4,
        production_storage_binding_loader=binding_loader,
    )
    authorization = consumed["document"]["authorization_bundle"]
    checked = passkey.validate_authorization_bundle(
        authorization,
        growth_plan=plan,
        receipt_public_key=signer.public_key,
        now_unix=NOW + 4,
        require_current=True,
    )
    assert checked["authorization_receipt"]["prior_journal_head_sha256"] == ("7" * 64)
    assert requested["document"]["production_host_mutation_performed"] is False
    assert consumed["document"]["production_host_mutation_performed"] is False
    assert passkey.FACTS_SCHEMA.encode("ascii") in service._APPROVAL_JS


def test_service_and_owner_boundary_attest_release_bound_storage_key() -> None:
    raw_key = bytes(range(32))
    trust = {"trust_bundle_sha256": "6" * 64}
    unsigned = {
        "schema": "muncho-production-storage-authority-key-attestation.v1",
        "release_sha": RELEASE,
        "receipt_public_key_ed25519_hex": raw_key.hex(),
        "receipt_public_key_id": hashlib.sha256(raw_key).hexdigest(),
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
    response = service.handle_intake_frame(
        _intake_frame("attest_production_storage_authority", {}),
        authority_client=_AuthorityClient(
            ReceiptSigner(Ed25519PrivateKey.generate())
        ),
        executor_client=_UnusedExecutor(),
        release_revision=RELEASE,
        now_unix=NOW,
        production_storage_authority_attestor=(
            lambda release: attestation if release == RELEASE else {}
        ),
    )
    assert response["document"] == attestation

    class Transport:
        def invoke_owner_gate(self, canonical_frame: bytes) -> bytes:
            frame = protocol.decode_canonical_json(canonical_frame)
            assert frame["operation"] == (
                "attest_production_storage_authority"
            )
            response_unsigned = {
                "schema": passkey.REMOTE_RESPONSE_SCHEMA,
                "operation": frame["operation"],
                "release_sha": RELEASE,
                "ok": True,
                "document": attestation,
            }
            return protocol.canonical_json_bytes({
                **response_unsigned,
                "response_sha256": protocol.sha256_json(response_unsigned),
            })

    boundary = passkey.ProductionStoragePasskeyBoundary(
        RELEASE, Transport()
    )
    assert boundary.attest_authority() == attestation
    attestation["receipt_public_key_id"] = "0" * 64
    with pytest.raises(
        passkey.ProductionStoragePasskeyError,
        match="production_storage_authority_key_attestation_invalid",
    ):
        boundary.attest_authority()


def test_owner_route_preflights_requests_then_applies_locally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    tmp_path.chmod(0o700)
    installer.install_owner_state_root(
        RELEASE,
        sealed_artifact_binding=installer.build_owner_artifact_binding(
            RELEASE,
            plan["runtime_artifact_attestation"],
        ),
        state_root=tmp_path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        effective_uid=lambda: 0,
        wall_clock=lambda: NOW,
        artifact_verifier=lambda value, **_kwargs: value,
    )
    signer_key = Ed25519PrivateKey.generate()
    runtime = protocol.build_runtime_binding(
        executor_release_sha=RELEASE,
        executor_plan_sha256=plan["plan_sha256"],
        executor_binary_sha256=plan["executor_binary_sha256"],
        mutation_wrapper_sha256=plan["mutation_wrapper_sha256"],
        remote_transport_sha256=plan["remote_transport_sha256"],
    )
    calls: list[str] = []

    class Identity:
        owner_subject_sha256 = "a" * 64
        approved_account = contract.AUTHENTICATED_ACCOUNT

        def account_for_read_only_preflight(self) -> str:
            calls.append("identity_preflight")
            return contract.AUTHENTICATED_ACCOUNT

        def bind_approved_subject(self, expected: str) -> None:
            assert expected == hashlib.sha256(
                contract.AUTHENTICATED_ACCOUNT.encode()
            ).hexdigest()
            calls.append("identity_bind")

        def require_stable(self) -> None:
            calls.append("identity_stable")

        def __call__(self) -> str:
            return "token"

    class ProductionTransport:
        def _run_remote_input(self, _argv, *, input_bytes: bytes, **_kwargs):
            calls.append("guest")
            request = protocol.decode_canonical_json(input_bytes)
            if request["operation"] == "readiness":
                install_request = installer.build_guest_install_request(RELEASE)
                readiness_unsigned = {
                    "schema": installer.GUEST_READINESS_SCHEMA,
                    "release_sha": RELEASE,
                    "entrypoint": str(installer.GUEST_ENTRYPOINT),
                    "entrypoint_sha256": install_request[
                        "guest_source_sha256"
                    ],
                    "entrypoint_uid": 0,
                    "entrypoint_gid": 0,
                    "entrypoint_mode": "0755",
                    "entrypoint_link_count": 1,
                    "installer_sha256": install_request[
                        "installer_sha256"
                    ],
                    "interpreter_path": "/usr/bin/python3",
                    "interpreter_resolved_path": "/usr/bin/python3.11",
                    "interpreter_sha256": "8" * 64,
                    "installation_receipt_sha256": "9" * 64,
                    "sudoers_path": str(installer.GUEST_SUDOERS_PATH),
                    "sudoers_required": False,
                    "sudoers_absent": True,
                    "root_transport_required": True,
                    "ready": True,
                }
                document = {
                    **readiness_unsigned,
                    "readiness_sha256": protocol.sha256_json(
                        readiness_unsigned
                    ),
                }
            else:
                document = plan["source_preflight"]["guest"]
            unsigned = {
                "schema": "muncho-production-storage-growth-guest-response.v1",
                "operation": request["operation"],
                "ok": True,
                "document": document,
            }
            payload = protocol.canonical_json_bytes({
                **unsigned,
                "response_sha256": protocol.sha256_json(unsigned),
            })
            return subprocess.CompletedProcess((), 0, payload, b"")

    class Boundary:
        def request(self, **kwargs) -> dict:
            calls.append("passkey_request")
            assert kwargs["external_iam_receipt"]["receipt_sha256"] == "b" * 64
            return {"request_id": "c" * 64}

        def consume(self, **kwargs) -> dict:
            calls.append("passkey_consume")
            return {
                "receipt_public_key_ed25519_hex": (
                    signer_key.public_key().public_bytes_raw().hex()
                ),
                "authorization_bundle": {
                    "bundle_sha256": "d" * 64,
                    "authorization_receipt": {"runtime_binding": runtime},
                },
            }

    stable_iam = {
        "account": contract.AUTHENTICATED_ACCOUNT,
        "owner_subject_sha256": "a" * 64,
        "project": contract.PROJECT,
        "project_number": passkey.EXTERNAL_IAM_PROJECT_NUMBER,
        "zone": contract.ZONE,
        "instance_name": contract.INSTANCE_NAME,
        "instance_id": contract.INSTANCE_ID,
        "disk_name": contract.DISK_NAME,
        "disk_id": contract.DISK_ID,
        "permissions": {
            name: "GRANTED" for name in passkey.EXTERNAL_IAM_PERMISSIONS
        },
        "authorization_snapshot_sha256": "1" * 64,
        "instance_evidence_sha256": "2" * 64,
        "disk_evidence_sha256": "3" * 64,
        "owner_public_key_id": passkey.EXTERNAL_IAM_OWNER_KEY_ID,
    }
    iam_calls = 0

    def collect_iam(**_kwargs) -> dict:
        nonlocal iam_calls
        iam_calls += 1
        calls.append("iam")
        return {**stable_iam, "receipt_sha256": ("b" if iam_calls == 1 else "e") * 64}

    class FakeExecutor:
        def __init__(self, **_kwargs) -> None:
            calls.append("executor_construct")

        def execute(self, **_kwargs) -> dict:
            calls.append("executor_execute")
            return {"state": "completed", "result_sha256": "f" * 64}

    monkeypatch.setattr(
        owner_launcher,
        "collect_fresh_production_storage_growth_external_iam",
        collect_iam,
    )
    monkeypatch.setattr(
        passkey,
        "validate_external_iam_receipt",
        lambda value, **_kwargs: value,
    )
    from scripts.canary import production_storage_growth_adapter as adapter

    monkeypatch.setattr(adapter, "FixedProductionComputeClient", lambda **_kwargs: object())
    monkeypatch.setattr(adapter, "FixedProductionStorageAdapter", lambda **_kwargs: object())
    monkeypatch.setattr(executor, "ProductionStorageGrowthExecutor", FakeExecutor)
    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=Identity(),
        passkey_boundary=Boundary(),
        production_transport=ProductionTransport(),
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        state_root=tmp_path,
        wall_clock=lambda: NOW + 10,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )
    requested = route.request(
        growth_plan=plan,
        authorization_nonce_sha256="5" * 64,
    )
    terminal = route.apply_or_recover(
        growth_plan=plan,
        request_id=requested["passkey_request"]["request_id"],
        consume_attempt_id="6" * 64,
        external_iam_receipt=requested["external_iam_receipt"],
    )
    assert terminal["state"] == "completed"
    assert calls.index("passkey_consume") < calls.index("executor_execute")
    assert calls.count("iam") == 2
    assert calls.count("passkey_request") == 1
    assert calls.count("passkey_consume") == 1


def test_owner_route_recaptures_and_revalidates_time_after_slow_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    moments = iter((NOW + 1, NOW + 299, NOW + 300))
    receipt = {"receipt_sha256": "a" * 64}
    observed_validation_times: list[int] = []

    class Boundary:
        def request(self, **kwargs) -> dict:
            assert kwargs["now_unix"] == NOW + 300
            assert kwargs["external_iam_receipt"] is receipt
            return {"request_id": "b" * 64}

        def consume(self, **_kwargs) -> dict:  # pragma: no cover - unused
            raise AssertionError("consume is not part of request")

    class ProductionTransport:
        def _run_remote_input(self, *_args, **_kwargs):  # pragma: no cover
            raise AssertionError("preflight is replaced below")

    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=object(),
        passkey_boundary=Boundary(),
        production_transport=ProductionTransport(),
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        state_root=tmp_path,
        wall_clock=lambda: next(moments),
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
    )

    def slow_preflight(*, growth_plan: dict) -> dict:
        assert growth_plan["plan_sha256"] == plan["plan_sha256"]
        assert route._wall_clock() == NOW + 1
        assert route._wall_clock() == NOW + 299
        return {
            "receipt_sha256": "c" * 64,
            "external_iam_receipt": receipt,
        }

    route.preflight = slow_preflight  # type: ignore[method-assign]

    def validate_iam(value, *, now_unix: int, **_kwargs):
        assert value is receipt
        observed_validation_times.append(now_unix)
        return value

    monkeypatch.setattr(passkey, "validate_external_iam_receipt", validate_iam)
    requested = route.request(
        growth_plan=plan,
        authorization_nonce_sha256="5" * 64,
    )
    assert requested["passkey_request"]["request_id"] == "b" * 64
    assert observed_validation_times == [NOW + 300]


def test_privileged_state_helper_must_open_before_passkey_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    calls: list[str] = []

    class Boundary:
        def request(self, **_kwargs):  # pragma: no cover - constructor gate
            return {}

        def consume(self, **_kwargs):
            calls.append("consume")
            raise AssertionError("consume must not run")

    class Transport:
        def _run_remote_input(self, *_args, **_kwargs):
            raise AssertionError("transport must not run")

    class Session:
        def open(self):
            calls.append("open")
            raise executor.ProductionStorageExecutorError(
                "production_storage_state_helper_unavailable"
            )

        def close(self):
            calls.append("close")

    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=object(),
        passkey_boundary=Boundary(),
        production_transport=Transport(),
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        state_root=executor.PRODUCTION_STATE_ROOT,
        wall_clock=lambda: NOW,
        expected_state_uid=0,
        expected_state_gid=0,
        state_session_factory=lambda **_kwargs: Session(),
    )
    monkeypatch.setattr(
        route,
        "_attest_runtime_artifacts",
        lambda **_kwargs: plan["runtime_artifact_attestation"],
    )
    monkeypatch.setattr(
        route,
        "_attest_owner_state",
        lambda _binding: {
            "private_installation_receipt_sha256": "4" * 64,
        },
    )
    monkeypatch.setattr(
        passkey,
        "validate_external_iam_receipt",
        lambda value, **_kwargs: value,
    )
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_state_helper_unavailable",
    ):
        route.apply_or_recover(
            growth_plan=plan,
            request_id="1" * 64,
            consume_attempt_id="2" * 64,
            external_iam_receipt={"receipt_sha256": "3" * 64},
        )
    assert calls == ["open", "close"]


def test_privileged_state_helper_closes_after_consume_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _plan()
    calls: list[str] = []

    class Boundary:
        def request(self, **_kwargs):  # pragma: no cover - constructor gate
            return {}

        def consume(self, **_kwargs):
            calls.append("consume")
            raise owner_launcher.OwnerLauncherError("consume_failed")

    class Transport:
        def _run_remote_input(self, *_args, **_kwargs):
            raise AssertionError("transport must not run")

    class Session:
        def open(self):
            calls.append("open")
            return {"lock_acquired": True}

        def close(self):
            calls.append("close")

    boundary = Boundary()
    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=object(),
        passkey_boundary=boundary,
        production_transport=Transport(),
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        state_root=executor.PRODUCTION_STATE_ROOT,
        wall_clock=lambda: NOW,
        expected_state_uid=0,
        expected_state_gid=0,
        state_session_factory=lambda **_kwargs: Session(),
    )
    monkeypatch.setattr(
        route,
        "_attest_runtime_artifacts",
        lambda **_kwargs: plan["runtime_artifact_attestation"],
    )
    monkeypatch.setattr(
        route,
        "_attest_owner_state",
        lambda _binding: {
            "private_installation_receipt_sha256": "4" * 64,
        },
    )
    monkeypatch.setattr(
        passkey,
        "validate_external_iam_receipt",
        lambda value, **_kwargs: value,
    )

    def consume_then_fail(**kwargs):
        boundary.consume(
            growth_plan=kwargs["plan"],
            request_id=kwargs["request_id"],
            consume_attempt_id=kwargs["consume_attempt_id"],
        )

    monkeypatch.setattr(route, "_apply_after_state_ready", consume_then_fail)
    with pytest.raises(owner_launcher.OwnerLauncherError, match="consume_failed"):
        route.apply_or_recover(
            growth_plan=plan,
            request_id="1" * 64,
            consume_attempt_id="2" * 64,
            external_iam_receipt={"receipt_sha256": "3" * 64},
        )
    assert calls == ["open", "consume", "close"]


def test_owner_route_runs_real_adapter_and_executor_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, plan, signer = _signed_bundle()
    tmp_path.chmod(0o700)
    installer.install_owner_state_root(
        RELEASE,
        sealed_artifact_binding=installer.build_owner_artifact_binding(
            RELEASE,
            plan["runtime_artifact_attestation"],
        ),
        state_root=tmp_path,
        expected_uid=os.getuid(),
        expected_gid=os.getgid(),
        effective_uid=lambda: 0,
        wall_clock=lambda: NOW,
        artifact_verifier=lambda value, **_kwargs: value,
    )
    cloud_state = {"resized": False, "grown": False}
    mutations: list[str] = []
    install_request = installer.build_guest_install_request(RELEASE)

    readiness_unsigned = {
        "schema": installer.GUEST_READINESS_SCHEMA,
        "release_sha": RELEASE,
        "entrypoint": str(installer.GUEST_ENTRYPOINT),
        "entrypoint_sha256": install_request["guest_source_sha256"],
        "entrypoint_uid": 0,
        "entrypoint_gid": 0,
        "entrypoint_mode": "0755",
        "entrypoint_link_count": 1,
        "installer_sha256": install_request["installer_sha256"],
        "interpreter_path": "/usr/bin/python3",
        "interpreter_resolved_path": "/usr/bin/python3.11",
        "interpreter_sha256": "8" * 64,
        "installation_receipt_sha256": "9" * 64,
        "sudoers_path": str(installer.GUEST_SUDOERS_PATH),
        "sudoers_required": False,
        "sudoers_absent": True,
        "root_transport_required": True,
        "ready": True,
    }
    readiness = {
        **readiness_unsigned,
        "readiness_sha256": protocol.sha256_json(readiness_unsigned),
    }

    class Identity:
        owner_subject_sha256 = "a" * 64
        approved_account = contract.AUTHENTICATED_ACCOUNT

        def account_for_read_only_preflight(self) -> str:
            return self.approved_account

        def bind_approved_subject(self, expected: str) -> None:
            assert expected == hashlib.sha256(
                contract.AUTHENTICATED_ACCOUNT.encode()
            ).hexdigest()

        def require_stable(self) -> None:
            return None

        def __call__(self) -> str:
            return "token"

    class Boundary:
        def request(self, **_kwargs) -> dict:
            return {"request_id": "c" * 64}

        def consume(self, **_kwargs) -> dict:
            return {
                "receipt_public_key_ed25519_hex": (
                    signer.public_key.public_bytes_raw().hex()
                ),
                "authorization_bundle": bundle,
            }

    class ProductionTransport:
        def _run_remote_input(self, _argv, *, input_bytes: bytes, **_kwargs):
            request = protocol.decode_canonical_json(input_bytes)
            operation = request["operation"]
            if operation == "readiness":
                document = readiness
            elif operation == "observe":
                state = (
                    "target"
                    if cloud_state["grown"]
                    else "partial"
                    if cloud_state["resized"]
                    else "source"
                )
                document = _observation(
                    state,
                    collected_at_unix=NOW + 10,
                )["guest"]
            else:
                assert operation == "grow"
                cloud_state["grown"] = True
                mutations.append("grow")
                document = {
                    "completed": True,
                    "idempotency_key_sha256": request["document"][
                        "idempotency_key_sha256"
                    ],
                    "guest": _observation(
                        "target",
                        collected_at_unix=NOW + 10,
                    )["guest"],
                }
            unsigned = {
                "schema": "muncho-production-storage-growth-guest-response.v1",
                "operation": operation,
                "ok": True,
                "document": document,
            }
            payload = protocol.canonical_json_bytes({
                **unsigned,
                "response_sha256": protocol.sha256_json(unsigned),
            })
            return subprocess.CompletedProcess((), 0, payload, b"")

    class HttpResponse:
        def __init__(self, value: dict) -> None:
            self._value = value

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self._value).encode()

    def urlopen(request, *, timeout: int):
        assert timeout == 30
        url = request.full_url
        if request.method == "POST":
            assert url.endswith(
                f"/disks/{contract.DISK_NAME}/resize?requestId="
                f"{plan['provider_request_id']}"
            )
            cloud_state["resized"] = True
            mutations.append("resize")
            return HttpResponse({
                "name": "operation-1",
                "id": "123",
                "operationType": "resize",
                "targetId": contract.DISK_ID,
            })
        if f"/instances/{contract.INSTANCE_NAME}" in url:
            return HttpResponse({
                "name": contract.INSTANCE_NAME,
                "id": contract.INSTANCE_ID,
                "status": "RUNNING",
                "zone": f"zones/{contract.ZONE}",
                "selfLink": contract.INSTANCE_SELF_LINK,
                "disks": [{
                    "boot": True,
                    "autoDelete": True,
                    "deviceName": "persistent-disk-0",
                    "mode": "READ_WRITE",
                    "type": "PERSISTENT",
                    "source": contract.DISK_SELF_LINK,
                }],
            })
        return HttpResponse({
            "name": contract.DISK_NAME,
            "id": contract.DISK_ID,
            "type": f"diskTypes/{contract.DISK_TYPE}",
            "sizeGb": "100" if cloud_state["resized"] else "50",
            "zone": f"zones/{contract.ZONE}",
            "selfLink": contract.DISK_SELF_LINK,
            "users": [contract.INSTANCE_SELF_LINK],
            "status": "READY",
            "sourceImage": (
                "https://www.googleapis.com/compute/v1/projects/"
                f"{contract.SOURCE_IMAGE_PROJECT}/global/images/"
                f"{contract.SOURCE_IMAGE_NAME}"
            ),
        })

    stable_iam = {
        "account": contract.AUTHENTICATED_ACCOUNT,
        "owner_subject_sha256": "a" * 64,
        "project": contract.PROJECT,
        "project_number": passkey.EXTERNAL_IAM_PROJECT_NUMBER,
        "zone": contract.ZONE,
        "instance_name": contract.INSTANCE_NAME,
        "instance_id": contract.INSTANCE_ID,
        "disk_name": contract.DISK_NAME,
        "disk_id": contract.DISK_ID,
        "permissions": {
            name: "GRANTED" for name in passkey.EXTERNAL_IAM_PERMISSIONS
        },
        "authorization_snapshot_sha256": "1" * 64,
        "instance_evidence_sha256": "2" * 64,
        "disk_evidence_sha256": "3" * 64,
        "owner_public_key_id": passkey.EXTERNAL_IAM_OWNER_KEY_ID,
    }
    iam_calls = 0

    def collect_iam(**_kwargs):
        nonlocal iam_calls
        iam_calls += 1
        return {
            **stable_iam,
            "receipt_sha256": ("b" if iam_calls == 1 else "e") * 64,
        }

    monkeypatch.setattr(
        owner_launcher,
        "collect_fresh_production_storage_growth_external_iam",
        collect_iam,
    )
    monkeypatch.setattr(
        passkey,
        "validate_external_iam_receipt",
        lambda value, **_kwargs: value,
    )
    route = owner_launcher.ProductionStorageGrowthOwnerRoute(
        release_sha=RELEASE,
        owner_identity=Identity(),
        passkey_boundary=Boundary(),
        production_transport=ProductionTransport(),
        runtime_artifact_attestor=lambda: plan[
            "runtime_artifact_attestation"
        ],
        state_root=tmp_path,
        wall_clock=lambda: NOW + 10,
        expected_state_uid=os.getuid(),
        expected_state_gid=os.getgid(),
        compute_urlopen=urlopen,
        compute_sleep=lambda _seconds: None,
    )
    requested = route.request(
        growth_plan=plan,
        authorization_nonce_sha256="5" * 64,
    )
    terminal = route.apply_or_recover(
        growth_plan=plan,
        request_id=requested["passkey_request"]["request_id"],
        consume_attempt_id="6" * 64,
        external_iam_receipt=requested["external_iam_receipt"],
    )
    assert terminal["state"] == "completed"
    assert mutations == ["resize", "grow"]
    assert terminal["executor_result"]["mutations_performed_this_attempt"] == [
        "provider_disk_resize_50_to_100",
        "online_partition_and_ext4_growth",
    ]


def test_executor_event_log_is_chained_to_shared_authority_head(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    result = _runner(tmp_path, transport, signer, plan).execute(
        growth_plan=plan,
        authorization_bundle=bundle,
    )
    lines = (tmp_path / f"{plan['plan_sha256']}.events.jsonl").read_bytes().splitlines()
    events = [protocol.decode_canonical_json(line) for line in lines]
    assert [item["event_kind"] for item in events] == [
        "execution_started",
        "execution_completed",
    ]
    assert events[0]["prior_event_head_sha256"] == "f" * 64
    assert events[1]["prior_event_head_sha256"] == events[0]["event_head_sha256"]
    assert result["event_head_sha256"] == events[1]["event_head_sha256"]


def test_event_log_rejects_tamper_truncation_reorder_mode_and_hardlink(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, signer, plan)
    runner.execute(growth_plan=plan, authorization_bundle=bundle)
    path = tmp_path / f"{plan['plan_sha256']}.events.jsonl"
    original = path.read_bytes()
    lines = original.splitlines()

    variants = [
        original.replace(plan["plan_sha256"].encode(), b"0" * 64, 1),
        original[:-1],
        b"\n".join(reversed(lines)) + b"\n",
    ]
    for payload in variants:
        path.write_bytes(payload)
        os.chmod(path, 0o600)
        with pytest.raises(
            executor.ProductionStorageExecutorError,
            match="production_storage_event_log_invalid",
        ):
            runner.execute(growth_plan=plan, authorization_bundle=bundle)

    path.write_bytes(original)
    os.chmod(path, 0o640)
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_state_storage_invalid",
    ):
        runner.execute(growth_plan=plan, authorization_bundle=bundle)

    path.unlink()
    attacker = tmp_path / "attacker-events.jsonl"
    attacker.write_bytes(original)
    os.chmod(attacker, 0o600)
    os.link(attacker, path)
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_state_storage_invalid",
    ):
        runner.execute(growth_plan=plan, authorization_bundle=bundle)


def test_completed_event_append_boundary_crash_recovers_without_duplicate(
    tmp_path: Path,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, signer, plan)
    original_append = runner._append_event
    crashed = False

    def append_then_crash(*args, **kwargs):
        nonlocal crashed
        event = original_append(*args, **kwargs)
        if kwargs["event_kind"] == "execution_completed" and not crashed:
            crashed = True
            raise RuntimeError("lost owner process after durable event append")
        return event

    runner._append_event = append_then_crash  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="lost owner process"):
        runner.execute(growth_plan=plan, authorization_bundle=bundle)
    runner._append_event = original_append  # type: ignore[method-assign]
    recovered = runner.execute(growth_plan=plan, authorization_bundle=bundle)
    lines = (
        tmp_path / f"{plan['plan_sha256']}.events.jsonl"
    ).read_bytes().splitlines()
    assert recovered["state"] == "completed"
    assert len(lines) == 2


def test_mid_event_publication_crash_after_resize_preserves_last_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    runner = _runner(tmp_path, transport, signer, plan)
    real_write = executor.os.write
    write_calls = 0

    def fail_halfway_through_completed_publication(
        fd: int,
        payload: bytes,
    ) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            partial = max(1, len(payload) // 2)
            return real_write(fd, payload[:partial])
        if write_calls == 3:
            raise OSError("simulated power loss during event publication")
        return real_write(fd, payload)

    monkeypatch.setattr(
        executor.os,
        "write",
        fail_halfway_through_completed_publication,
    )
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_event_log_write_failed",
    ):
        runner.execute(growth_plan=plan, authorization_bundle=bundle)

    event_path = tmp_path / f"{plan['plan_sha256']}.events.jsonl"
    published = event_path.read_bytes().splitlines()
    assert len(published) == 1
    assert protocol.decode_canonical_json(published[0])["event_kind"] == (
        "execution_started"
    )
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 1

    monkeypatch.setattr(executor.os, "write", real_write)
    recovered = runner.execute(growth_plan=plan, authorization_bundle=bundle)
    assert recovered["state"] == "completed"
    assert recovered["mutations_performed_this_attempt"] == []
    assert len(event_path.read_bytes().splitlines()) == 2
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 1


def test_executor_rejects_symlinked_journal_before_observation(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    target = tmp_path / "attacker.json"
    target.write_text("{}", encoding="utf-8")
    (tmp_path / f"{plan['plan_sha256']}.json").symlink_to(target)
    transport = FakeTransport()
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_state_storage_invalid",
    ):
        _runner(tmp_path, transport, signer, plan).execute(
            growth_plan=plan,
            authorization_bundle=bundle,
        )
    assert transport.calls == []


def test_two_processes_race_one_exact_transaction_under_flock(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    context = multiprocessing.get_context("fork")
    manager = context.Manager()
    shared = manager.Namespace()
    shared.state = "source"
    calls = manager.list()
    results = context.Queue()

    class SharedTransport:
        now = NOW + 3

        def observe_exact_target(self) -> dict:
            self.now += 1
            calls.append((os.getpid(), "observe"))
            return _observation(shared.state, collected_at_unix=self.now)

        def resize_exact_disk_once(self, *, provider_request_id: str) -> dict:
            calls.append((os.getpid(), "resize"))
            shared.state = "partial"
            return {"accepted": True, "provider_request_id": provider_request_id}

        def grow_exact_root_online(self, *, idempotency_key_sha256: str) -> dict:
            calls.append((os.getpid(), "grow"))
            shared.state = "target"
            return {"completed": True, "idempotency_key_sha256": idempotency_key_sha256}

    def execute() -> None:
        try:
            transport = SharedTransport()
            results.put((
                "ok",
                _runner(tmp_path, transport, signer, plan).execute(
                    growth_plan=plan,
                    authorization_bundle=bundle,
                ),
            ))
        except BaseException as error:  # pragma: no cover - surfaced below
            results.put(("error", repr(error)))

    processes = [context.Process(target=execute) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    assert [item[0] for item in outcomes] == ["ok", "ok"]
    assert [name for _pid, name in calls].count("resize") == 1
    assert [name for _pid, name in calls].count("grow") == 1
