from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from scripts.canary import passkey_v2_production_storage_growth as passkey
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_contract as contract
from scripts.canary import production_storage_growth_executor as executor
from scripts.canary.passkey_v2_signer import ReceiptSigner


NOW = 2_000_000_000
RELEASE = "a" * 40
BOOT_ID = "baf2a4ac-6450-4da8-a6de-d89a2f0c1250"


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


def _plan() -> dict:
    return dict(
        contract.build_plan(
            source_preflight=_observation(),
            release_revision=RELEASE,
            executor_binary_sha256="1" * 64,
            mutation_wrapper_sha256="2" * 64,
            read_only_collector_sha256="3" * 64,
            remote_transport_sha256="4" * 64,
            now_unix=NOW,
        )
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
            prior_journal_head_sha256=protocol.GENESIS_JOURNAL_HEAD_SHA256,
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
        self.now = NOW
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
    )
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_runtime_binding_invalid",
    ):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
            now_unix=NOW + 4,
        )
    assert transport.calls == []


def test_source_to_target_executes_one_resize_and_online_growth(tmp_path: Path) -> None:
    bundle, plan, signer = _signed_bundle()
    transport = FakeTransport()
    result = _runner(tmp_path, transport, signer, plan).execute(
        growth_plan=plan,
        authorization_bundle=bundle,
        now_unix=NOW + 4,
    )
    assert result["state"] == "completed"
    assert result["mutations_performed_this_attempt"] == [
        "provider_disk_resize_50_to_100",
        "online_partition_and_ext4_growth",
    ]
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 1
    assert (tmp_path / f"{plan['plan_sha256']}.json").exists()


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
            now_unix=NOW + 4,
        )
    assert transport.state == "partial"
    assert [name for name, _ in transport.calls].count("resize") == 1

    transport.now = NOW + 4_000
    result = runner.execute(
        growth_plan=plan,
        authorization_bundle=bundle,
        now_unix=NOW + 4_000,
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
            now_unix=NOW + 4,
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
    with pytest.raises(
        executor.ProductionStorageExecutorError,
        match="production_storage_observation_invalid",
    ):
        runner.execute(
            growth_plan=plan,
            authorization_bundle=bundle,
            now_unix=NOW + 5,
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
            now_unix=NOW + 4,
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
        now_unix=NOW + 4,
    )
    path = tmp_path / f"{plan['plan_sha256']}.json"
    original_journal = path.read_text()
    calls_before = list(transport.calls)
    replay = runner.execute(
        growth_plan=plan,
        authorization_bundle=bundle,
        now_unix=NOW + 9_000,
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
            now_unix=NOW + 9_001,
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
            now_unix=NOW + 9_002,
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
            now_unix=NOW + 4,
        )
    assert [name for name, _ in transport.calls].count("resize") == 1
    assert [name for name, _ in transport.calls].count("grow") == 1
