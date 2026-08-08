from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shlex
import stat
import struct
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway import canonical_writer_production_cutover as cutover
from gateway import production_cron_migration
from scripts.canary import full_canary_owner_launcher as canary_transport
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_initial_collector as initial_collector
from scripts.canary import production_cutover_owner_launcher as owner
from scripts.canary import production_cutover_public_stager as stager
from scripts.canary import production_cutover_activation_lock as authority_lock
from scripts.canary import production_cutover_unit_input_rotation as rotation
from scripts.canary import production_storage_growth_installer as storage_installer
from scripts.canary import upstream_sync_rail_successor_rebind as successor_rebind
from tests.gateway.test_canonical_writer_production_cutover import (
    MemoryJournal,
    NOW,
    Services,
    Snapshots,
    _approval,
    _cutover_plan,
    _database_recovery_receipt,
    _freeze,
    _isolated_canary_goal_prerequisite,
    _mechanical_package,
    _runtime_attestation,
    _snapshot,
)
from tests.scripts.canary.test_production_cutover_initial_observe import (
    _cron_inventory,
    _host_facts,
)
REVISION = "a" * 40


@pytest.fixture(autouse=True)
def _clear_process_ca_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep launcher identity tests independent of gateway import side effects."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"])
def test_owner_transport_rejects_custom_ca_environment(
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(name, "/untrusted/test-ca.pem")
    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="custom_ca_bundle_forbidden",
    ):
        canary_transport._reject_custom_ca_environment()


def _operational_receipt_key_ids() -> dict[str, str]:
    return {
        domain: f"{index:064x}"
        for index, domain in enumerate(
            sorted(package.CREDENTIALS_BY_DOMAIN), start=1
        )
    }


def _operational_identity_inputs() -> tuple[dict, dict]:
    domains = sorted(_operational_receipt_key_ids())
    return (
        {
            domain: {
                "user": f"muncho-edge-{domain}",
                "group": f"muncho-edge-{domain}",
                "uid": 2100 + index,
                "gid": 2100 + index,
            }
            for index, domain in enumerate(domains)
        },
        {
            domain: {
                "group": f"muncho-edge-{domain}-c",
                "gid": 2200 + index,
            }
            for index, domain in enumerate(domains)
        },
    )


def _unit_input_payload(revision: str) -> dict:
    operational_identities, operational_socket_groups = (
        _operational_identity_inputs()
    )
    return {
        "schema": package.UNIT_INPUT_PAYLOAD_SCHEMA,
        "database_ip": "10.20.30.40",
        "target": {
            "project": "adventico-ai-platform",
            "zone": "europe-west3-a",
            "vm": "ai-platform-runtime-01",
            "database": "ai_platform_brain",
            "sql_instance": "production-pg18",
            "sql_host": "10.20.30.40",
            "tls_server_name": "production.example.internal",
            "port": 5432,
            "writer_login": "muncho_production_writer_login",
        },
        "gateway": {
            "user": "ai-platform-brain",
            "group": "ai-platform-brain",
            "uid": 1000,
            "gid": 1000,
        },
        "writer": {
            "user": "muncho-canonical-writer",
            "group": "muncho-canonical-writer",
            "uid": 2000,
            "gid": 2000,
        },
        "projector": {
            "user": "muncho-projector",
            "group": "muncho-projector",
            "uid": 2004,
            "gid": 2004,
        },
        "routeback": {
            "user": "muncho-discord-egress",
            "group": "muncho-discord-egress",
            "uid": 2002,
            "gid": 2002,
        },
        "connector": {
            "user": "muncho-discord-connector",
            "group": "muncho-discord-connector",
            "uid": 2001,
            "gid": 2001,
        },
        "mac_ops": {
            "user": "muncho-mac-ops-edge",
            "group": "muncho-mac-ops-edge",
            "uid": 2003,
            "gid": 2003,
        },
        "browser": {
            "user": "muncho-capability-browser",
            "group": "muncho-capability-browser",
            "uid": 2006,
            "gid": 2006,
        },
        "worker": {
            "user": "muncho-worker",
            "group": "muncho-worker",
            "uid": 2007,
            "gid": 2007,
        },
        "writer_client_group": {
            "group": "muncho-writer-client",
            "gid": 2005,
        },
        "worker_client_group": {
            "group": "muncho-worker-clients",
            "gid": 2008,
        },
        "operational_edge_identities": operational_identities,
        "operational_edge_socket_groups": operational_socket_groups,
        "writer_capability_public_key_id": "c" * 64,
        "discord_edge_receipt_public_key_id": "a" * 64,
        "operational_edge_key_foundation_sha256": "d" * 64,
        "operational_edge_receipt_public_key_ids": (
            _operational_receipt_key_ids()
        ),
        "discord_reconciliation_intent": {
            "schema": package.DISCORD_RECONCILIATION_INTENT_SCHEMA,
            "purpose": package.DISCORD_RECONCILIATION_INTENT_PURPOSE,
            "release_revision": revision,
            "legacy_public_policy_sha256": "1" * 64,
            "target_public_policy_sha256": "2" * 64,
            "reviewed_reconciliation": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "release_owner_uid": 1000,
        "release_owner_gid": 1000,
        "bwrap_sha256": "6" * 64,
        "shell_sha256": "7" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _collector_receipt(now: int, services: Services) -> dict:
    source_key = Ed25519PrivateKey.generate()
    source = _freeze(source_key, services)
    authority = source.value["cutover_authority"]

    def retime_service(value: dict) -> dict:
        unsigned = {
            **{
                name: item
                for name, item in value.items()
                if name != "observation_sha256"
            },
            "observed_at_unix": now,
        }
        return {
            **unsigned,
            "observation_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest(),
        }

    cron_inventory = _cron_inventory(now)
    host_facts = _host_facts(now)
    mechanical_package = _mechanical_package(host_facts)
    continuity_plan = production_cron_migration.build_owner_approved_plan(
        cron_inventory,
        dispositions=[],
        approval_id="11111111-1111-4111-8111-111111111111",
        approved_by="owner",
    )
    unsigned = {
        "schema": owner.COLLECTOR_SCHEMA,
        "release_revision": REVISION,
        "target": source.value["target"],
        "artifacts": authority["artifacts"],
        "gateway_before": retime_service(source.value["gateway_before"]),
        "writer_before": retime_service(source.value["writer_before"]),
        "connector_before": retime_service(source.value["connector_before"]),
        "gateway_target_identity": authority["gateway_target_identity"],
        "writer_target_identity": authority["writer_target_identity"],
        "connector_target_identity": authority["connector_target_identity"],
        "host_transition": authority["host_transition"],
        "capability_topology": authority["capability_topology"],
        "initial_snapshot": _snapshot(
            14_073,
            observed_at=now,
        ).to_mapping(),
        "cron_inventory": cron_inventory,
        "cron_continuity_plan": continuity_plan,
        "mechanical_job_host_facts": host_facts,
        "mechanical_job_package": mechanical_package,
        "observed_at_unix": now,
        "source_boot_id_sha256": "f" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    return {
        **unsigned,
        "receipt_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest(),
    }


def _private_key_file(tmp_path: Path, key: Ed25519PrivateKey) -> Path:
    path = (tmp_path / "owner-ed25519.pem").resolve()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path


def _openssh_private_key_file(
    tmp_path: Path,
    key: Ed25519PrivateKey,
) -> Path:
    path = (tmp_path / "owner-ed25519-openssh").resolve()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path


def _patch_staged_paths(monkeypatch, staged: Path) -> None:
    staged.mkdir(mode=0o700, exist_ok=True)
    staged.chmod(0o700)
    os.chown(staged, os.geteuid(), os.getegid())
    monkeypatch.setattr(
        package,
        "STAGED_UNIT_INPUT_PLAN_PATH",
        staged / "unit-input-plan.json",
    )
    monkeypatch.setattr(
        package,
        "STAGED_UNIT_INPUT_APPROVAL_PATH",
        staged / "unit-input-approval.json",
    )
    monkeypatch.setattr(
        package,
        "FIXED_UNIT_INPUTS_PATH",
        staged / "production-unit-inputs.json",
    )
    monkeypatch.setattr(cutover, "STAGED_FREEZE_PLAN_PATH", staged / "freeze-plan.json")
    monkeypatch.setattr(
        cutover,
        "STAGED_FREEZE_APPROVAL_PATH",
        staged / "freeze-approval.json",
    )
    monkeypatch.setattr(
        cutover,
        "STAGED_CUTOVER_PLAN_PATH",
        staged / "cutover-plan.json",
    )


def _stage_freeze_with_test_claim(
    monkeypatch: pytest.MonkeyPatch,
    publication: dict,
    *,
    now: int,
    journal: MemoryJournal,
    proof_marker: str = "1",
    proof_consumed_at: int | None = None,
    proof_window_seconds: int = 3_600,
    action_payload_marker: str = "a",
) -> dict:
    consumed_at = now if proof_consumed_at is None else proof_consumed_at
    proof = {
        "proof_sha256": proof_marker * 64,
        "action_envelope": {
            "request_id": proof_marker * 64,
            "envelope_sha256": proof_marker * 64,
            "action_payload_sha256": action_payload_marker * 64,
            "authority_release_sha": REVISION,
        },
        "authorization_receipt": {
            "receipt_sha256": proof_marker * 64,
            "consume_attempt_id": proof_marker * 64,
            "consumed_at_unix": consumed_at,
            "execution_window_expires_at_unix": (
                consumed_at + proof_window_seconds
            ),
        },
    }
    frame = {
        "schema": stager.passkey.CUTOVER_CLAIM_FRAME_SCHEMA,
        "publication": publication,
        "passkey_proof": proof,
        "claim_sha256": "5" * 64,
    }

    def validate(_value, *, now_unix):
        if now_unix >= proof["authorization_receipt"][
            "execution_window_expires_at_unix"
        ]:
            raise stager.passkey.ProductionCutoverPasskeyError(
                "production_cutover_passkey_proof_invalid"
            )
        return publication, proof

    monkeypatch.setattr(stager.passkey, "validate_claim_frame", validate)
    monkeypatch.setattr(
        stager.passkey,
        "validate_claim_frame_for_recorded_replay",
        lambda value: (publication, proof),
    )
    return stager.stage_publication(
        frame,
        require_root=False,
        now_unix=now,
        journal=journal,
        lock_factory=nullcontext,
    )


def test_owner_key_stays_local_while_freeze_publication_is_staged(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    services = Services()
    collector = _collector_receipt(now, services)
    private = Ed25519PrivateKey.generate()
    key_path = _private_key_file(tmp_path, private)
    loaded = owner.load_owner_private_key(key_path)

    plan, approval, publication = owner.author_freeze(
        collector_receipt=collector,
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=loaded,
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )

    serialized = _canonical(publication)
    assert plan.sha256.encode() in serialized
    assert approval["approval_sha256"].encode() in serialized
    assert key_path.read_bytes() not in serialized
    assert b"PRIVATE KEY" not in serialized
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    receipt = _stage_freeze_with_test_claim(
        monkeypatch, publication, now=now, journal=MemoryJournal()
    )
    assert receipt["action"] == "freeze-authority"
    assert stat.S_IMODE((staged / "freeze-plan.json").stat().st_mode) == 0o400
    assert stat.S_IMODE((staged / "freeze-approval.json").stat().st_mode) == 0o400
    assert key_path.exists()


def test_approved_sequence_runs_freeze_tail_then_cutover_plan_without_new_semantics(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    services = Services()
    private = Ed25519PrivateKey.generate()
    freeze, approval, freeze_publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, services),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=private,
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    journal = MemoryJournal()
    _stage_freeze_with_test_claim(
        monkeypatch,
        freeze_publication,
        now=now,
        journal=journal,
    )

    tail = cutover.execute_final_tail_capture(
        freeze,
        approval,
        cutover.FreezeDependencies(
            services=services,
            snapshots=Snapshots(_snapshot(14_081, marker="2", observed_at=now)),
            journal=journal,
            lock=nullcontext,
        ),
        now_unix=now,
    )
    plan, publication = owner.author_cutover(
        freeze_plan=freeze.to_mapping(),
        freeze_approval=approval,
        final_tail_receipt=tail.to_mapping(),
        gateway_stopped=services.gateway.to_mapping(),
        writer_stopped=services.writer.to_mapping(),
        connector_stopped=services.connector.to_mapping(),
    )
    receipt = stager.stage_publication(
        publication,
        require_root=False,
        now_unix=now,
        journal=journal,
        lock_factory=nullcontext,
    )

    assert receipt["action"] == "cutover-plan"
    assert plan.value["freeze_approval_sha256"] == approval["approval_sha256"]
    assert plan.value["legacy_truth_decision"] == freeze.value[
        "cutover_authority"
    ]["legacy_truth_decision"]
    assert stat.S_IMODE((staged / "cutover-plan.json").stat().st_mode) == 0o400


def test_stager_rejects_tampered_publication_without_creating_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    services = Services()
    _plan, _approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, services),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    publication["documents"]["plan"]["release_revision"] = "b" * 40
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)

    try:
        _stage_freeze_with_test_claim(
            monkeypatch,
            publication,
            now=now,
            journal=MemoryJournal(),
        )
    except stager.PublicStagingError:
        pass
    else:
        raise AssertionError("tampered publication unexpectedly staged")

    assert list(staged.iterdir()) == []


def test_stager_rejects_bare_freeze_before_any_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    _plan, _approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, Services()),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    journal = MemoryJournal()

    with pytest.raises(
        stager.PublicStagingError,
        match="public_staging_passkey_claim_required",
    ):
        stager.stage_publication(
            publication,
            require_root=False,
            now_unix=now,
            journal=journal,
            lock_factory=nullcontext,
        )

    assert journal.load(publication["documents"]["plan"]["plan_sha256"]) == []
    assert list(staged.iterdir()) == []


def test_stager_accepts_exact_replay_and_pre_mutation_reauthorization(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    plan, _approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, Services()),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    journal = MemoryJournal()

    first = _stage_freeze_with_test_claim(
        monkeypatch,
        publication,
        now=now,
        journal=journal,
        proof_window_seconds=60,
    )
    replay = _stage_freeze_with_test_claim(
        monkeypatch,
        publication,
        now=now,
        journal=journal,
        proof_window_seconds=60,
    )
    with pytest.raises(
        stager.PublicStagingError,
        match="public_staging_passkey_claim_conflict",
    ):
        _stage_freeze_with_test_claim(
            monkeypatch,
            publication,
            now=now,
            journal=journal,
            proof_marker="6",
        )
    superseded = _stage_freeze_with_test_claim(
        monkeypatch,
        publication,
        now=now + 60,
        journal=journal,
        proof_marker="6",
    )

    assert all(item["created"] is True for item in first["files"])
    assert all(item["created"] is False for item in replay["files"])
    assert all(item["created"] is False for item in superseded["files"])
    assert [
        entry.value["event"] for entry in journal.load(plan.sha256)
    ] == ["passkey_claim", "authority", "passkey_claim_superseded"]
    active_entry, active_claim = cutover.require_recorded_passkey_claim(
        journal,
        plan_sha256=plan.sha256,
        approval_sha256=publication["documents"]["approval"][
            "approval_sha256"
        ],
        release_revision=REVISION,
    )
    assert active_entry.value["event"] == "passkey_claim_superseded"
    assert active_claim["passkey_proof_sha256"] == "6" * 64


def test_stager_rejects_reauthorization_after_freeze_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    plan, approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, Services()),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    journal = MemoryJournal()
    _stage_freeze_with_test_claim(
        monkeypatch,
        publication,
        now=now,
        journal=journal,
        proof_window_seconds=60,
    )
    claim_entry, claim = cutover.require_recorded_passkey_claim(
        journal,
        plan_sha256=plan.sha256,
        approval_sha256=approval["approval_sha256"],
        release_revision=REVISION,
    )
    cutover._require_or_record_passkey_intent(
        journal,
        journal_plan_sha256=plan.sha256,
        phase="freeze_stop",
        freeze_plan_sha256=plan.sha256,
        freeze_approval_sha256=approval["approval_sha256"],
        cutover_plan_sha256=None,
        claim_entry=claim_entry,
        claim=claim,
        now_unix=now,
    )

    with pytest.raises(
        stager.PublicStagingError,
        match="public_staging_passkey_claim_conflict",
    ):
        _stage_freeze_with_test_claim(
            monkeypatch,
            publication,
            now=now + 60,
            journal=journal,
            proof_marker="6",
        )

    assert [
        entry.value["event"] for entry in journal.load(plan.sha256)
    ] == ["passkey_claim", "authority", "passkey_intent"]


def test_stager_recovers_claim_to_authority_crash_without_backdating(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    plan, approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, Services()),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    journal = MemoryJournal()
    original = cutover._append_authority
    monkeypatch.setattr(
        cutover,
        "_append_authority",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected authority journal failure")
        ),
    )

    with pytest.raises(OSError, match="injected authority"):
        _stage_freeze_with_test_claim(
            monkeypatch,
            publication,
            now=now,
            journal=journal,
        )
    assert [
        entry.value["event"] for entry in journal.load(plan.sha256)
    ] == ["passkey_claim"]
    assert list(staged.iterdir()) == []

    monkeypatch.setattr(cutover, "_append_authority", original)
    recovered = _stage_freeze_with_test_claim(
        monkeypatch,
        publication,
        now=now + 4_000,
        journal=journal,
        proof_consumed_at=now,
    )
    entries = journal.load(plan.sha256)

    assert all(item["created"] is True for item in recovered["files"])
    assert [entry.value["event"] for entry in entries] == [
        "passkey_claim",
        "authority",
    ]
    assert entries[0].value["recorded_at_unix"] == now
    assert entries[1].value["recorded_at_unix"] == now + 4_000
    validated, _claim_entry, _claim = cutover._validated_claimed_approval(
        journal,
        plan=plan,
        approval_value=approval,
    )
    assert validated.sha256 == approval["approval_sha256"]


def test_stager_rolls_back_only_new_files_after_partial_publication_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    services = Services()
    _plan, _approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, services),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    original = stager._install_exact
    calls = 0

    def fail_second(path, payload, *, uid, gid):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise stager.PublicStagingError("injected_public_staging_failure")
        return original(path, payload, uid=uid, gid=gid)

    monkeypatch.setattr(stager, "_install_exact", fail_second)
    journal = MemoryJournal()
    try:
        _stage_freeze_with_test_claim(
            monkeypatch,
            publication,
            now=now,
            journal=journal,
        )
    except stager.PublicStagingError:
        pass
    else:
        raise AssertionError("partial publication unexpectedly succeeded")

    assert list(staged.iterdir()) == []
    plan_sha256 = publication["documents"]["plan"]["plan_sha256"]
    assert [
        entry.value["event"] for entry in journal.load(plan_sha256)
    ] == ["passkey_claim", "authority"]

    # The durable claim is the recovery authority after a crash between the
    # journal commit and file installation.  Exact old bytes may finish that
    # interrupted publication after both short leases expire.
    monkeypatch.setattr(stager, "_install_exact", original)
    recovered = _stage_freeze_with_test_claim(
        monkeypatch,
        publication,
        now=now + 4_000,
        journal=journal,
        proof_consumed_at=now,
    )

    assert all(item["created"] is True for item in recovered["files"])
    assert [
        entry.value["event"] for entry in journal.load(plan_sha256)
    ] == ["passkey_claim", "authority"]


def test_stager_rejects_expired_unused_claim_before_any_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    _plan, _approval, publication = owner.author_freeze(
        collector_receipt=_collector_receipt(now, Services()),
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        database_recovery_receipt=_database_recovery_receipt(
            rechecked_at_unix=now
        ),
        truth_mode="start_new_truth_epoch",
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    journal = MemoryJournal()

    with pytest.raises(
        stager.PublicStagingError,
        match="public_staging_passkey_claim_expired",
    ):
        _stage_freeze_with_test_claim(
            monkeypatch,
            publication,
            now=now + 4_000,
            journal=journal,
            proof_consumed_at=now,
        )

    plan_sha256 = publication["documents"]["plan"]["plan_sha256"]
    assert journal.load(plan_sha256) == []
    assert list(staged.iterdir()) == []


def test_owner_private_key_loader_rejects_symlink(tmp_path: Path) -> None:
    private = Ed25519PrivateKey.generate()
    key_path = _private_key_file(tmp_path, private)
    link = tmp_path / "owner-key-link.pem"
    link.symlink_to(key_path)

    try:
        owner.load_owner_private_key(link)
    except owner.OwnerCutoverError as exc:
        assert str(exc) == "owner_cutover_private_key_invalid"
    else:
        raise AssertionError("symlinked private key unexpectedly accepted")


def test_owner_private_key_loader_accepts_unencrypted_ed25519_openssh(
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    key_path = _openssh_private_key_file(tmp_path, private)

    loaded = owner.load_owner_private_key(key_path)

    assert loaded.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ) == private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )


def test_owner_private_key_loader_rejects_encrypted_openssh(tmp_path: Path) -> None:
    key_path = (tmp_path / "encrypted-owner-ed25519").resolve()
    completed = subprocess.run(
        (
            "/usr/bin/ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "not-exported",
            "-f",
            str(key_path),
        ),
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0
    key_path.chmod(0o600)

    with pytest.raises(
        owner.OwnerCutoverError,
        match="owner_cutover_private_key_invalid",
    ):
        owner.load_owner_private_key(key_path)


def test_owner_validates_fresh_initial_observation_before_host_planning() -> None:
    now = int(time.time())
    full = _collector_receipt(now, Services())
    fields = {
        "release_revision",
        "target",
        "artifacts",
        "gateway_before",
        "writer_before",
        "connector_before",
        "initial_snapshot",
        "cron_inventory",
        "cron_continuity_plan",
        "mechanical_job_host_facts",
        "mechanical_job_package",
        "observed_at_unix",
        "source_boot_id_sha256",
        "secret_material_recorded",
        "secret_digest_recorded",
    }
    unsigned = {
        "schema": owner.INITIAL_COLLECTOR_SCHEMA,
        **{name: full[name] for name in fields},
    }
    receipt = {
        **unsigned,
        "receipt_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest(),
    }

    assert owner.validate_initial_collector_receipt(
        receipt,
        release_revision=REVISION,
        now_unix=now,
    ) == receipt

    gateway = dict(receipt["gateway_before"])
    gateway["observed_at_unix"] = now - owner.MAX_COLLECTOR_AGE_SECONDS - 1
    gateway["observation_sha256"] = hashlib.sha256(
        _canonical({
            name: item
            for name, item in gateway.items()
            if name != "observation_sha256"
        })
    ).hexdigest()
    changed = {**receipt, "gateway_before": gateway}
    changed["receipt_sha256"] = hashlib.sha256(
        _canonical({
            name: item
            for name, item in changed.items()
            if name != "receipt_sha256"
        })
    ).hexdigest()
    try:
        owner.validate_initial_collector_receipt(
            changed,
            release_revision=REVISION,
            now_unix=now,
        )
    except owner.OwnerCutoverError as exc:
        assert str(exc) == "owner_cutover_initial_collector_content_invalid"
    else:
        raise AssertionError("stale initial service observation unexpectedly accepted")


def test_owner_accepts_only_freeze_bound_stopped_service_receipt() -> None:
    private = Ed25519PrivateKey.generate()
    services = Services()
    freeze = _freeze(private, services)
    approval = _approval(private, freeze)
    services.stop_gateway()
    receipt = initial_collector.collect_stopped_services(
        REVISION,
        freeze_plan=freeze.to_mapping(),
        freeze_approval=approval,
        services=services,
        clock=lambda: NOW,
        boot_reader=lambda: "f" * 64,
        require_root=False,
    )

    assert owner.validate_stopped_collector_receipt(
        receipt,
        freeze_plan=freeze.to_mapping(),
        freeze_approval=approval,
        now_unix=NOW,
    ) == receipt

    changed = {**receipt, "freeze_plan_sha256": "b" * 64}
    changed["receipt_sha256"] = hashlib.sha256(
        _canonical({
            name: item
            for name, item in changed.items()
            if name != "receipt_sha256"
        })
    ).hexdigest()
    try:
        owner.validate_stopped_collector_receipt(
            changed,
            freeze_plan=freeze.to_mapping(),
            freeze_approval=approval,
            now_unix=NOW,
        )
    except owner.OwnerCutoverError as exc:
        assert str(exc) == "owner_cutover_stopped_collector_invalid"
    else:
        raise AssertionError("wrong freeze digest unexpectedly accepted")


def _unit_input_authority(revision: str, now: int) -> tuple[dict, dict, dict]:
    return owner.build_unit_input_authority(
        release_revision=revision,
        unit_inputs=_unit_input_payload(revision),
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(revision),
        now_unix=now,
    )


def _rotation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    now: int,
) -> tuple[Path, tuple[dict, dict, dict], tuple[dict, dict, dict]]:
    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir(mode=0o700)
    evidence.chmod(0o700)
    os.chown(evidence, os.geteuid(), os.getegid())
    staged = evidence / "staged"
    _patch_staged_paths(monkeypatch, staged)
    monkeypatch.setattr(cutover, "EVIDENCE_ROOT", evidence)
    predecessor = _unit_input_authority("a" * 40, now)
    successor = _unit_input_authority("b" * 40, now)
    stager.stage_publication(
        predecessor[2],
        require_root=False,
        now_unix=now,
    )
    package.bootstrap_fixed_unit_inputs(
        authority_plan_path=package.STAGED_UNIT_INPUT_PLAN_PATH,
        authority_approval_path=package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        unit_inputs_path=package.FIXED_UNIT_INPUTS_PATH,
        require_root=False,
        now_unix=now,
    )
    return staged, predecessor, successor


def test_unit_input_authority_writers_heal_exact_temporary_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )

    plan_path = staged / "unit-input-plan.json"
    stage_alias = staged / ".unit-input-plan.json.stage.999999"
    os.link(plan_path, stage_alias)
    assert plan_path.stat().st_nlink == 2
    staged_receipt = stager.stage_publication(
        predecessor[2],
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    assert staged_receipt["files"][0]["created"] is False
    assert plan_path.stat().st_nlink == 1
    assert not stage_alias.exists()

    fixed_path = staged / "production-unit-inputs.json"
    bootstrap_alias = (
        staged / ".production-unit-inputs.json.bootstrap.999998"
    )
    os.link(fixed_path, bootstrap_alias)
    assert fixed_path.stat().st_nlink == 2
    bootstrap_receipt = package.bootstrap_fixed_unit_inputs(
        authority_plan_path=package.STAGED_UNIT_INPUT_PLAN_PATH,
        authority_approval_path=package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        unit_inputs_path=package.FIXED_UNIT_INPUTS_PATH,
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    assert bootstrap_receipt["created"] is False
    assert fixed_path.stat().st_nlink == 1
    assert not bootstrap_alias.exists()

    approval_path = staged / "unit-input-approval.json"
    plan_bootstrap_alias = (
        staged / ".unit-input-plan.json.stage.999996"
    )
    approval_bootstrap_alias = (
        staged / ".unit-input-approval.json.rotate.999995"
    )
    os.link(plan_path, plan_bootstrap_alias)
    os.link(approval_path, approval_bootstrap_alias)
    assert plan_path.stat().st_nlink == 2
    assert approval_path.stat().st_nlink == 2
    package.bootstrap_fixed_unit_inputs(
        authority_plan_path=package.STAGED_UNIT_INPUT_PLAN_PATH,
        authority_approval_path=package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        unit_inputs_path=package.FIXED_UNIT_INPUTS_PATH,
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    assert plan_path.stat().st_nlink == 1
    assert approval_path.stat().st_nlink == 1
    assert not plan_bootstrap_alias.exists()
    assert not approval_bootstrap_alias.exists()

    rotation_alias = staged / ".unit-input-approval.json.rotate.999997"
    os.link(approval_path, rotation_alias)
    assert approval_path.stat().st_nlink == 2
    rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    assert not rotation_alias.exists()


def test_unit_input_public_stager_locks_and_rechecks_freshness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir(mode=0o700)
    evidence.chmod(0o700)
    os.chown(evidence, os.geteuid(), os.getegid())
    staged = evidence / "staged"
    _patch_staged_paths(monkeypatch, staged)
    publication = _unit_input_authority("a" * 40, now)[2]

    class Busy:
        def __enter__(self):
            raise RuntimeError("another writer activation lifecycle is running")

        def __exit__(self, *_args):
            return False

    with pytest.raises(
        stager.PublicStagingError,
        match="public_staging_activation_lock_unavailable",
    ):
        stager.stage_publication(
            publication,
            require_root=False,
            now_unix=now,
            lock_factory=Busy,
        )
    assert list(staged.iterdir()) == []

    clock = [now]
    monkeypatch.setattr(stager.time, "time", lambda: clock[0])

    class ExpireWhileWaiting:
        def __enter__(self):
            clock[0] = now + 4_000

        def __exit__(self, *_args):
            return False

    with pytest.raises(
        stager.PublicStagingError,
        match="public_staging_documents_invalid",
    ):
        stager.stage_publication(
            publication,
            require_root=False,
            lock_factory=ExpireWhileWaiting,
        )
    assert list(staged.iterdir()) == []


def test_unit_input_package_bootstrap_shares_activation_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, _successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    }

    class Busy:
        def __enter__(self):
            raise RuntimeError("another writer activation lifecycle is running")

        def __exit__(self, *_args):
            return False

    with pytest.raises(
        package.PackagingError,
        match="cutover_unit_inputs_activation_lock_unavailable",
    ):
        package.bootstrap_fixed_unit_inputs(
            authority_plan_path=package.STAGED_UNIT_INPUT_PLAN_PATH,
            authority_approval_path=package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            unit_inputs_path=package.FIXED_UNIT_INPUTS_PATH,
            require_root=False,
            now_unix=now,
            lock_factory=Busy,
        )
    assert {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    } == before


def test_authority_lock_reuses_only_a_proven_inherited_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated: list[int] = []
    flocked: list[tuple[int, int]] = []
    inherited: list[tuple[int, bool]] = []
    monkeypatch.setattr(authority_lock.sys, "platform", "linux")
    monkeypatch.setenv(authority_lock.INHERITED_LOCK_FD_ENV, "7")
    monkeypatch.setattr(
        authority_lock,
        "_validate_lock_identity",
        lambda descriptor: validated.append(descriptor),
    )
    monkeypatch.setattr(
        authority_lock.fcntl,
        "flock",
        lambda descriptor, operation: flocked.append(
            (descriptor, operation)
        ),
    )
    monkeypatch.setattr(
        authority_lock.os,
        "get_inheritable",
        lambda descriptor: descriptor == 7,
    )
    monkeypatch.setattr(
        authority_lock.os,
        "set_inheritable",
        lambda descriptor, value: inherited.append((descriptor, value)),
    )

    with authority_lock.authority_activation_lock(require_root=True):
        assert validated == [7, 7]

    assert flocked == [(
        7,
        authority_lock.fcntl.LOCK_EX | authority_lock.fcntl.LOCK_NB,
    )]
    assert inherited == [(7, True), (7, True)]

    monkeypatch.setenv(authority_lock.INHERITED_LOCK_FD_ENV, "2")
    with pytest.raises(
        authority_lock.AuthorityActivationLockError,
        match="production_authority_activation_lock_invalid",
    ):
        with authority_lock.authority_activation_lock(require_root=True):
            raise AssertionError("invalid inherited descriptor was entered")


def test_authority_lock_preserves_errors_from_the_protected_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        authority_lock.INHERITED_LOCK_FD_ENV,
        raising=False,
    )
    monkeypatch.setattr(
        authority_lock.activation,
        "_host_activation_lock",
        lambda: nullcontext(7),
    )
    monkeypatch.setattr(authority_lock.sys, "platform", "linux")
    monkeypatch.setattr(
        authority_lock,
        "_validate_lock_identity",
        lambda _descriptor: None,
    )
    inheritable: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        authority_lock.os,
        "get_inheritable",
        lambda _descriptor: False,
    )
    monkeypatch.setattr(
        authority_lock.os,
        "set_inheritable",
        lambda descriptor, value: inheritable.append(
            (descriptor, value)
        ),
    )
    body_error = RuntimeError("protected body failed")

    with pytest.raises(RuntimeError) as caught:
        with authority_lock.authority_activation_lock(require_root=True):
            assert (
                os.environ[authority_lock.INHERITED_LOCK_FD_ENV]
                == "7"
            )
            raise body_error

    assert caught.value is body_error
    assert authority_lock.INHERITED_LOCK_FD_ENV not in os.environ
    assert inheritable == [(7, True), (7, False)]


def test_authority_lock_reports_inheritability_probe_failure_stably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        authority_lock.INHERITED_LOCK_FD_ENV,
        raising=False,
    )
    monkeypatch.setattr(authority_lock.sys, "platform", "linux")
    monkeypatch.setattr(
        authority_lock.activation,
        "_host_activation_lock",
        lambda: nullcontext(7),
    )
    monkeypatch.setattr(
        authority_lock,
        "_validate_lock_identity",
        lambda _descriptor: None,
    )

    def _probe_failure(_descriptor: int) -> bool:
        raise OSError("probe failed")

    monkeypatch.setattr(
        authority_lock.os,
        "get_inheritable",
        _probe_failure,
    )
    restore_calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        authority_lock.os,
        "set_inheritable",
        lambda descriptor, value: restore_calls.append(
            (descriptor, value)
        ),
    )

    with pytest.raises(
        authority_lock.AuthorityActivationLockError,
        match="production_authority_activation_lock_unavailable",
    ):
        with authority_lock.authority_activation_lock(require_root=True):
            raise AssertionError("unreachable")

    assert authority_lock.INHERITED_LOCK_FD_ENV not in os.environ
    assert restore_calls == []


def test_authority_lock_publishes_exact_outer_descriptor_for_nested_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        authority_lock.INHERITED_LOCK_FD_ENV,
        raising=False,
    )
    monkeypatch.setattr(authority_lock.sys, "platform", "linux")
    monkeypatch.setattr(
        authority_lock.activation,
        "_host_activation_lock",
        lambda: nullcontext(11),
    )
    validated: list[int] = []
    flocked: list[tuple[int, int]] = []
    inheritable: list[tuple[int, bool]] = []
    inheritable_state = {11: False}
    monkeypatch.setattr(
        authority_lock,
        "_validate_lock_identity",
        lambda descriptor: validated.append(descriptor),
    )
    monkeypatch.setattr(
        authority_lock.fcntl,
        "flock",
        lambda descriptor, operation: flocked.append(
            (descriptor, operation)
        ),
    )
    monkeypatch.setattr(
        authority_lock.os,
        "get_inheritable",
        lambda descriptor: inheritable_state[descriptor],
    )

    def _set_inheritable(descriptor: int, value: bool) -> None:
        inheritable.append((descriptor, value))
        inheritable_state[descriptor] = value

    monkeypatch.setattr(
        authority_lock.os,
        "set_inheritable",
        _set_inheritable,
    )

    with authority_lock.authority_activation_lock(require_root=True):
        assert os.environ[authority_lock.INHERITED_LOCK_FD_ENV] == "11"
        with authority_lock.authority_activation_lock(require_root=True):
            assert os.environ[authority_lock.INHERITED_LOCK_FD_ENV] == "11"

    assert authority_lock.INHERITED_LOCK_FD_ENV not in os.environ
    assert validated == [11, 11, 11, 11]
    assert flocked == [(
        11,
        authority_lock.fcntl.LOCK_EX | authority_lock.fcntl.LOCK_NB,
    )]
    assert inheritable == [
        (11, True),
        (11, False),
    ]


def test_authority_lock_serializes_real_kernel_thread_contention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "activation.lock"
    lock_path.write_bytes(b"")
    monkeypatch.delenv(
        authority_lock.INHERITED_LOCK_FD_ENV,
        raising=False,
    )
    monkeypatch.setattr(authority_lock.sys, "platform", "linux")
    monkeypatch.setattr(
        authority_lock,
        "_validate_lock_identity",
        lambda _descriptor: None,
    )

    order: list[str] = []
    order_guard = threading.Lock()

    @contextmanager
    def _kernel_host_lock():
        descriptor = os.open(lock_path, os.O_RDWR)
        try:
            authority_lock.fcntl.flock(
                descriptor,
                authority_lock.fcntl.LOCK_EX
                | authority_lock.fcntl.LOCK_NB,
            )
            with order_guard:
                order.append(
                    f"{threading.current_thread().name}:host-enter"
                )
            yield descriptor
        finally:
            with order_guard:
                order.append(
                    f"{threading.current_thread().name}:host-exit"
                )
            authority_lock.fcntl.flock(
                descriptor,
                authority_lock.fcntl.LOCK_UN,
            )
            os.close(descriptor)

    monkeypatch.setattr(
        authority_lock.activation,
        "_host_activation_lock",
        _kernel_host_lock,
    )

    outer_entered = threading.Event()
    release_outer = threading.Event()
    contender_waiting_at_mutex = threading.Event()
    contender_entered = threading.Event()
    errors: list[BaseException] = []
    active = {"count": 0, "maximum": 0}

    class _ObservedRLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()

        def __enter__(self):
            if threading.current_thread().name == "contender":
                contender_waiting_at_mutex.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()
            return False

    monkeypatch.setattr(
        authority_lock,
        "_PROCESS_ACTIVATION_MUTEX",
        _ObservedRLock(),
    )

    def _enter_body(
        *,
        entered: threading.Event,
        release: threading.Event | None = None,
    ) -> None:
        try:
            with authority_lock.authority_activation_lock(
                require_root=True
            ):
                with order_guard:
                    active["count"] += 1
                    active["maximum"] = max(
                        active["maximum"],
                        active["count"],
                    )
                    order.append(
                        f"{threading.current_thread().name}:body-enter"
                    )
                entered.set()
                if release is not None:
                    assert release.wait(timeout=5)
                with order_guard:
                    order.append(
                        f"{threading.current_thread().name}:body-exit"
                    )
                    active["count"] -= 1
        except BaseException as exc:
            errors.append(exc)

    outer = threading.Thread(
        target=_enter_body,
        name="outer",
        kwargs={
            "entered": outer_entered,
            "release": release_outer,
        },
    )
    contender = threading.Thread(
        target=_enter_body,
        name="contender",
        kwargs={
            "entered": contender_entered,
        },
    )

    outer.start()
    assert outer_entered.wait(timeout=5)
    contender.start()
    assert contender_waiting_at_mutex.wait(timeout=5)
    assert not contender_entered.is_set()

    release_outer.set()
    outer.join(timeout=5)
    contender.join(timeout=5)

    assert not outer.is_alive()
    assert not contender.is_alive()
    assert errors == []
    assert active == {"count": 0, "maximum": 1}
    assert order == [
        "outer:host-enter",
        "outer:body-enter",
        "outer:body-exit",
        "outer:host-exit",
        "contender:host-enter",
        "contender:body-enter",
        "contender:body-exit",
        "contender:host-exit",
    ]
    assert authority_lock.INHERITED_LOCK_FD_ENV not in os.environ


def test_unit_input_rotation_archives_triplet_and_exact_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    unrelated = staged / "freeze-plan.json"
    unrelated.write_bytes(b"unrelated-staged-evidence")
    unrelated.chmod(0o400)
    predecessor_raw = {
        "plan": (staged / "unit-input-plan.json").read_bytes(),
        "approval": (staged / "unit-input-approval.json").read_bytes(),
        "fixed": (staged / "production-unit-inputs.json").read_bytes(),
    }

    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    replay = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )

    assert replay == receipt
    assert (staged / "unit-input-plan.json").read_bytes() == _canonical(
        successor[0]
    )
    assert (staged / "unit-input-approval.json").read_bytes() == _canonical(
        successor[1]
    )
    assert (staged / "production-unit-inputs.json").read_bytes() == (
        _canonical(package._unit_inputs_from_authority(
            successor[0],
            successor[1],
        ))
        + b"\n"
    )
    assert unrelated.read_bytes() == b"unrelated-staged-evidence"
    transaction = Path(receipt["audit_transaction_path"])
    archived = transaction / rotation.PREDECESSOR_DIRECTORY_NAME
    assert (archived / "unit-input-plan.json").read_bytes() == (
        predecessor_raw["plan"]
    )
    assert (archived / "unit-input-approval.json").read_bytes() == (
        predecessor_raw["approval"]
    )
    assert (archived / "production-unit-inputs.json").read_bytes() == (
        predecessor_raw["fixed"]
    )
    assert stat.S_IMODE(transaction.stat().st_mode) == 0o700
    assert receipt["successor_triplet_complete"] is True

    bootstrapped = package.bootstrap_fixed_unit_inputs(
        authority_plan_path=package.STAGED_UNIT_INPUT_PLAN_PATH,
        authority_approval_path=package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        unit_inputs_path=package.FIXED_UNIT_INPUTS_PATH,
        require_root=False,
        now_unix=now,
    )
    assert bootstrapped["release_revision"] == successor[0][
        "release_revision"
    ]
    assert bootstrapped["created"] is False
    assert predecessor[0]["release_revision"] == "a" * 40


def test_owner_rotation_launcher_allows_remote_exact_replay_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    calls: list[tuple[str, str, dict]] = []

    class Transport:
        def invoke(
            self,
            revision: str,
            action: str,
            *,
            publication: dict,
        ) -> dict:
            calls.append((revision, action, publication))
            return dict(receipt)

    observed = owner.rotate_unit_input_authority(
        release_revision=successor[0]["release_revision"],
        remote_stager_revision="c" * 40,
        publication=successor[2],
        transport=Transport(),
        now_unix=now + 4_000,
    )

    assert observed == receipt
    assert calls == [(
        "c" * 40,
        "rotate-unit-input-authority",
        successor[2],
    )]


def test_unit_input_rotation_failure_rolls_back_and_other_successor_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = {
        path.name: path.read_bytes()
        for path in (
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            package.FIXED_UNIT_INPUTS_PATH,
        )
    }

    def fail_after_successor_plan(stage: str) -> None:
        if stage == "successor_plan_staged":
            raise RuntimeError("injected rotation failure")

    monkeypatch.setattr(rotation, "_checkpoint", fail_after_successor_plan)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_failed",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )

    assert {
        path.name: path.read_bytes()
        for path in (
            package.STAGED_UNIT_INPUT_PLAN_PATH,
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            package.FIXED_UNIT_INPUTS_PATH,
        )
    } == before
    different = _unit_input_authority("c" * 40, now)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_successor_conflict",
    ):
        rotation.rotate_unit_input_authority(
            different[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    recovered = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    assert recovered["successor_revision"] == "b" * 40
    assert (staged / "production-unit-inputs.json").exists()
    assert predecessor[0]["release_revision"] == "a" * 40


def test_unit_input_rotation_rejects_a_mixed_predecessor_triplet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    fixed = staged / "production-unit-inputs.json"
    fixed.chmod(0o600)
    fixed.write_bytes(
        _canonical(package._unit_inputs_from_authority(
            successor[0],
            successor[1],
        ))
        + b"\n"
    )
    fixed.chmod(package.FIXED_UNIT_INPUTS_MODE)

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_predecessor_invalid",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )

    audit_root = cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME
    assert audit_root.exists()
    assert list(audit_root.iterdir()) == []


def test_unit_input_rotation_finishes_receipt_after_complete_successor_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    install_exact = rotation._install_exact

    def fail_receipt(path: Path, payload: bytes, **kwargs) -> bool:
        if path.name == rotation.RECEIPT_FILE_NAME:
            raise rotation.UnitInputRotationError(
                "unit_input_rotation_write_failed"
            )
        return install_exact(path, payload, **kwargs)

    monkeypatch.setattr(rotation, "_install_exact", fail_receipt)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_write_failed",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )

    assert (staged / "unit-input-plan.json").read_bytes() == _canonical(
        successor[0]
    )
    assert (staged / "unit-input-approval.json").read_bytes() == _canonical(
        successor[1]
    )
    assert (staged / "production-unit-inputs.json").read_bytes() == (
        _canonical(package._unit_inputs_from_authority(
            successor[0],
            successor[1],
        ))
        + b"\n"
    )
    transactions = list(
        (cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME).iterdir()
    )
    assert len(transactions) == 1
    assert not (transactions[0] / rotation.RECEIPT_FILE_NAME).exists()

    monkeypatch.setattr(rotation, "_install_exact", install_exact)
    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )
    assert receipt["successor_triplet_complete"] is True
    assert (transactions[0] / rotation.RECEIPT_FILE_NAME).exists()


@pytest.mark.parametrize(
    "crash_stage",
    (
        "audit_prepared",
        "predecessor_fixed_inputs_removed",
        "predecessor_approval_removed",
        "predecessor_plan_removed",
        "successor_plan_staged",
        "successor_approval_staged",
        "successor_fixed_inputs_staged",
    ),
)
def test_unit_input_rotation_resumes_after_each_durable_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_stage: str,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )
    transactions = list(
        (cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME).iterdir()
    )
    assert len(transactions) == 1
    assert not (transactions[0] / rotation.RECEIPT_FILE_NAME).exists()

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    assert receipt["successor_revision"] == "b" * 40
    assert (staged / "unit-input-plan.json").read_bytes() == _canonical(
        successor[0]
    )
    assert (staged / "unit-input-approval.json").read_bytes() == _canonical(
        successor[1]
    )
    assert (staged / "production-unit-inputs.json").read_bytes() == (
        _canonical(package._unit_inputs_from_authority(
            successor[0],
            successor[1],
        ))
        + b"\n"
    )


@pytest.mark.parametrize(
    "crash_stage",
    (
        "transaction_authorized",
        "successor_publication_archived",
        "predecessor_plan_archived",
        "predecessor_approval_archived",
        "predecessor_fixed_inputs_archived",
    ),
)
def test_unit_input_rotation_durable_authorization_recovers_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_stage: str,
) -> None:
    now = int(time.time())
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )
    transactions = list(
        (cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME).iterdir()
    )
    assert len(transactions) == 1
    transaction = json.loads(
        (transactions[0] / rotation.TRANSACTION_FILE_NAME).read_bytes()
    )
    assert transaction["authorization_checked_at_unix"] == now

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    if crash_stage == "transaction_authorized":
        different = _unit_input_authority("c" * 40, now + 4_000)
        with pytest.raises(
            rotation.UnitInputRotationError,
            match="unit_input_rotation_successor_conflict",
        ):
            rotation.rotate_unit_input_authority(
                different[2],
                require_root=False,
                now_unix=now + 4_000,
                lock_factory=nullcontext,
            )
    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )
    assert receipt["authorization_checked_at_unix"] == now
    assert receipt["transaction_sha256"] == transaction[
        "transaction_sha256"
    ]
    assert receipt["successor_triplet_complete"] is True


def test_unit_input_rotation_pre_authorization_crash_still_requires_freshness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    }

    def crash(stage: str) -> None:
        if stage == "before_transaction_authorized":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )
    transactions = list(
        (cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME).iterdir()
    )
    assert len(transactions) == 1
    assert not (transactions[0] / rotation.TRANSACTION_FILE_NAME).exists()

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now + 4_000,
            lock_factory=nullcontext,
        )
    assert {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    } == before


def test_unit_input_rotation_rechecks_lease_at_transaction_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    }
    clock = [now]
    monkeypatch.setattr(rotation.time, "time", lambda: clock[0])

    def expire_before_authorization(stage: str) -> None:
        if stage == "before_transaction_authorized":
            clock[0] = now + 4_000

    monkeypatch.setattr(
        rotation,
        "_checkpoint",
        expire_before_authorization,
    )
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            lock_factory=nullcontext,
        )

    transactions = list(
        (cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME).iterdir()
    )
    assert len(transactions) == 1
    assert not (transactions[0] / rotation.TRANSACTION_FILE_NAME).exists()
    assert {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    } == before


def test_unit_input_rotation_recovers_expired_persisted_transaction_only_exactly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )

    def crash(stage: str) -> None:
        if stage == "successor_plan_staged":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )

    different = _unit_input_authority("c" * 40, now + 4_000)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_successor_conflict",
    ):
        rotation.rotate_unit_input_authority(
            different[2],
            require_root=False,
            now_unix=now + 4_000,
            lock_factory=nullcontext,
        )

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )
    assert receipt["successor_revision"] == "b" * 40
    assert (staged / "unit-input-plan.json").read_bytes() == _canonical(
        successor[0]
    )
    assert (staged / "unit-input-approval.json").read_bytes() == _canonical(
        successor[1]
    )
    assert (staged / "production-unit-inputs.json").read_bytes() == (
        _canonical(package._unit_inputs_from_authority(
            successor[0],
            successor[1],
        ))
        + b"\n"
    )


def test_unit_input_rotation_incomplete_successor_precedes_historical_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    next_successor = _unit_input_authority("c" * 40, now + 1)

    def crash(stage: str) -> None:
        if stage == "audit_prepared":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.rotate_unit_input_authority(
            next_successor[2],
            require_root=False,
            now_unix=now + 1,
            lock_factory=nullcontext,
        )

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_successor_conflict",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now + 2,
            lock_factory=nullcontext,
        )
    recovered = rotation.rotate_unit_input_authority(
        next_successor[2],
        require_root=False,
        now_unix=now + 2,
        lock_factory=nullcontext,
    )
    assert recovered["successor_revision"] == "c" * 40


def test_unit_input_rotation_lock_contention_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    }

    class Busy:
        def __enter__(self):
            raise RuntimeError("another writer activation lifecycle is running")

        def __exit__(self, *_args):
            return False

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_failed",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=Busy,
        )

    assert {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    } == before
    assert not (
        cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME
    ).exists()


def test_unit_input_rotation_replay_rejects_missing_terminal_triplet_member(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    package.FIXED_UNIT_INPUTS_PATH.unlink()

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_file_unavailable",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now + 4_000,
            lock_factory=nullcontext,
        )


def test_unit_input_rotation_replay_binds_receipt_to_archived_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    receipt_path = (
        Path(receipt["audit_transaction_path"])
        / rotation.RECEIPT_FILE_NAME
    )
    changed = {**receipt, "predecessor_approval_sha256": "f" * 64}
    changed["receipt_sha256"] = hashlib.sha256(
        _canonical({
            name: item
            for name, item in changed.items()
            if name != "receipt_sha256"
        })
    ).hexdigest()
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(_canonical(changed))
    receipt_path.chmod(0o400)

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_receipt_invalid",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            now_unix=now + 4_000,
            lock_factory=nullcontext,
        )


def test_unit_input_rotation_checks_approval_freshness_after_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    }
    clock = [now]
    monkeypatch.setattr(rotation.time, "time", lambda: clock[0])

    class ExpireWhileWaiting:
        def __enter__(self):
            clock[0] = now + 901

        def __exit__(self, *_args):
            return False

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        rotation.rotate_unit_input_authority(
            successor[2],
            require_root=False,
            lock_factory=ExpireWhileWaiting,
        )

    assert {
        path.name: path.read_bytes()
        for path in staged.iterdir()
    } == before
    audit_root = cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME
    assert not audit_root.exists() or list(audit_root.iterdir()) == []


def test_unit_input_authority_is_signed_locally_and_staged_before_release(
    monkeypatch,
    tmp_path: Path,
) -> None:
    now = int(time.time())
    operational_identities, operational_socket_groups = (
        _operational_identity_inputs()
    )
    value = {
        "schema": package.UNIT_INPUT_PAYLOAD_SCHEMA,
        "database_ip": "10.20.30.40",
        "target": {
            "project": "adventico-ai-platform",
            "zone": "europe-west3-a",
            "vm": "ai-platform-runtime-01",
            "database": "ai_platform_brain",
            "sql_instance": "production-pg18",
            "sql_host": "10.20.30.40",
            "tls_server_name": "production.example.internal",
            "port": 5432,
            "writer_login": "muncho_production_writer_login",
        },
        "gateway": {
            "user": "ai-platform-brain",
            "group": "ai-platform-brain",
            "uid": 1000,
            "gid": 1000,
        },
        "writer": {
            "user": "muncho-canonical-writer",
            "group": "muncho-canonical-writer", "uid": 2000, "gid": 2000,
        },
        "projector": {
            "user": "muncho-projector", "group": "muncho-projector",
            "uid": 2004, "gid": 2004,
        },
        "routeback": {
            "user": "muncho-discord-egress",
            "group": "muncho-discord-egress",
            "uid": 2002,
            "gid": 2002,
        },
        "connector": {
            "user": "muncho-discord-connector",
            "group": "muncho-discord-connector", "uid": 2001, "gid": 2001,
        },
        "mac_ops": {
            "user": "muncho-mac-ops-edge",
            "group": "muncho-mac-ops-edge",
            "uid": 2003,
            "gid": 2003,
        },
        "browser": {
            "user": "muncho-capability-browser",
            "group": "muncho-capability-browser",
            "uid": 2006,
            "gid": 2006,
        },
        "worker": {
            "user": "muncho-worker",
            "group": "muncho-worker",
            "uid": 2007,
            "gid": 2007,
        },
        "writer_client_group": {"group": "muncho-writer-client", "gid": 2005},
        "worker_client_group": {"group": "muncho-worker-clients", "gid": 2008},
        "operational_edge_identities": operational_identities,
        "operational_edge_socket_groups": operational_socket_groups,
        "writer_capability_public_key_id": "c" * 64,
        "discord_edge_receipt_public_key_id": "a" * 64,
        "operational_edge_key_foundation_sha256": "d" * 64,
        "operational_edge_receipt_public_key_ids": (
            _operational_receipt_key_ids()
        ),
        "discord_reconciliation_intent": {
            "schema": package.DISCORD_RECONCILIATION_INTENT_SCHEMA,
            "purpose": package.DISCORD_RECONCILIATION_INTENT_PURPOSE,
            "release_revision": REVISION,
            "legacy_public_policy_sha256": "1" * 64,
            "target_public_policy_sha256": "2" * 64,
            "reviewed_reconciliation": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "release_owner_uid": 1000,
        "release_owner_gid": 1000,
        "bwrap_sha256": "6" * 64,
        "shell_sha256": "7" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    plan, approval, publication = owner.build_unit_input_authority(
        release_revision=REVISION,
        unit_inputs=value,
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        owner_runtime_attestation=_runtime_attestation(),
        now_unix=now,
    )
    staged = (tmp_path / "staged").resolve()
    _patch_staged_paths(monkeypatch, staged)
    monkeypatch.setattr(cutover, "EVIDENCE_ROOT", tmp_path.resolve())

    class InstalledStager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict]] = []

        def invoke(
            self,
            revision: str,
            action: str,
            *,
            publication: dict,
        ) -> dict:
            self.calls.append((revision, action, copy.deepcopy(publication)))
            return stager.stage_publication(
                publication,
                require_root=False,
                now_unix=now,
            )

    transport = InstalledStager()
    changed = {
        **publication,
        "secret_material_recorded": True,
    }
    changed["publication_sha256"] = hashlib.sha256(_canonical({
        name: item
        for name, item in changed.items()
        if name != "publication_sha256"
    })).hexdigest()
    with pytest.raises(
        owner.OwnerCutoverError,
        match="owner_cutover_unit_input_stage_invalid",
    ):
        owner.stage_unit_input_authority(
            release_revision=REVISION,
            remote_stager_revision="b" * 40,
            publication=changed,
            transport=transport,
            now_unix=now,
        )
    assert transport.calls == []

    receipt = owner.stage_unit_input_authority(
        release_revision=REVISION,
        remote_stager_revision="b" * 40,
        publication=publication,
        transport=transport,
        now_unix=now,
    )

    assert receipt["action"] == "unit-input-authority"
    assert transport.calls == [
        ("b" * 40, "stage-publication", publication),
    ]
    assert plan["plan_sha256"] == approval["plan_sha256"]
    assert (staged / "unit-input-plan.json").exists()
    assert (staged / "unit-input-approval.json").exists()
    assert not (staged / "production-unit-inputs.json").exists()


def test_owner_cli_authors_unit_input_publication_without_exporting_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: _runtime_attestation(revision),
    )
    private = Ed25519PrivateKey.generate()
    key_path = _private_key_file(tmp_path, private)
    operational_identities, operational_socket_groups = (
        _operational_identity_inputs()
    )
    value = {
        "schema": package.UNIT_INPUT_PAYLOAD_SCHEMA,
        "database_ip": "10.20.30.40",
        "target": {
            "project": "adventico-ai-platform",
            "zone": "europe-west3-a",
            "vm": "ai-platform-runtime-01",
            "database": "ai_platform_brain",
            "sql_instance": "production-pg18",
            "sql_host": "10.20.30.40",
            "tls_server_name": "production.example.internal",
            "port": 5432,
            "writer_login": "muncho_production_writer_login",
        },
        "gateway": {
            "user": "ai-platform-brain",
            "group": "ai-platform-brain",
            "uid": 1000,
            "gid": 1000,
        },
        "writer": {
            "user": "muncho-canonical-writer",
            "group": "muncho-canonical-writer", "uid": 2000, "gid": 2000,
        },
        "projector": {
            "user": "muncho-projector", "group": "muncho-projector",
            "uid": 2004, "gid": 2004,
        },
        "routeback": {
            "user": "muncho-discord-egress",
            "group": "muncho-discord-egress",
            "uid": 2002,
            "gid": 2002,
        },
        "connector": {
            "user": "muncho-discord-connector",
            "group": "muncho-discord-connector", "uid": 2001, "gid": 2001,
        },
        "mac_ops": {
            "user": "muncho-mac-ops-edge",
            "group": "muncho-mac-ops-edge",
            "uid": 2003,
            "gid": 2003,
        },
        "browser": {
            "user": "muncho-capability-browser",
            "group": "muncho-capability-browser",
            "uid": 2006,
            "gid": 2006,
        },
        "worker": {
            "user": "muncho-worker",
            "group": "muncho-worker",
            "uid": 2007,
            "gid": 2007,
        },
        "writer_client_group": {"group": "muncho-writer-client", "gid": 2005},
        "worker_client_group": {"group": "muncho-worker-clients", "gid": 2008},
        "operational_edge_identities": operational_identities,
        "operational_edge_socket_groups": operational_socket_groups,
        "writer_capability_public_key_id": "c" * 64,
        "discord_edge_receipt_public_key_id": "a" * 64,
        "operational_edge_key_foundation_sha256": "d" * 64,
        "operational_edge_receipt_public_key_ids": (
            _operational_receipt_key_ids()
        ),
        "discord_reconciliation_intent": {
            "schema": package.DISCORD_RECONCILIATION_INTENT_SCHEMA,
            "purpose": package.DISCORD_RECONCILIATION_INTENT_PURPOSE,
            "release_revision": REVISION,
            "legacy_public_policy_sha256": "1" * 64,
            "target_public_policy_sha256": "2" * 64,
            "reviewed_reconciliation": True,
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        },
        "release_owner_uid": 1000,
        "release_owner_gid": 1000,
        "bwrap_sha256": "6" * 64,
        "shell_sha256": "7" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    input_path = (tmp_path / "unit-inputs.json").resolve()
    input_path.write_bytes(_canonical(value))
    output = (tmp_path / "publication.json").resolve()

    assert owner.main([
        "author-unit-inputs",
        "--revision",
        REVISION,
        "--unit-inputs",
        str(input_path),
        "--owner-private-key",
        str(key_path),
        "--owner-subject-sha256",
        "a" * 64,
        "--output",
        str(output),
    ]) == 0

    publication = json.loads(output.read_bytes())
    assert publication["action"] == "unit-input-authority"
    assert b"PRIVATE KEY" not in output.read_bytes()
    assert key_path.exists()


def test_owner_cli_stages_successor_unit_inputs_through_installed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_revision = "b" * 40
    publication = {"canonical": "successor-unit-input-authority"}
    publication_path = (tmp_path / "unit-input-publication.json").resolve()
    publication_path.write_bytes(_canonical(publication))
    output = (tmp_path / "unit-input-stage-receipt.json").resolve()
    runtime_calls: list[str] = []
    identity_calls: list[str] = []
    staged: list[dict] = []
    transport = object()
    receipt = {"receipt_sha256": "c" * 64}

    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: (
            runtime_calls.append(revision)
            or _runtime_attestation(revision)
        ),
    )
    monkeypatch.setattr(
        owner,
        "build_production_cutover_owner_identity",
        lambda revision: (
            identity_calls.append(revision) or (object(), object(), object())
        ),
    )
    monkeypatch.setattr(
        owner,
        "ProductionCutoverTransport",
        lambda *_args, **_kwargs: transport,
    )

    def stage(**kwargs) -> dict:
        staged.append(kwargs)
        return receipt

    monkeypatch.setattr(owner, "stage_unit_input_authority", stage)

    assert owner.main([
        "stage-unit-inputs",
        "--revision",
        REVISION,
        "--remote-stager-revision",
        old_revision,
        "--publication",
        str(publication_path),
        "--output",
        str(output),
    ]) == 0

    assert runtime_calls == [REVISION]
    assert identity_calls == [REVISION]
    assert staged == [{
        "release_revision": REVISION,
        "remote_stager_revision": old_revision,
        "publication": publication,
        "transport": transport,
    }]
    assert json.loads(output.read_bytes()) == receipt


def test_owner_cli_rotates_successor_unit_inputs_through_installed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_revision = "b" * 40
    publication = {"canonical": "successor-unit-input-authority"}
    publication_path = (tmp_path / "unit-input-publication.json").resolve()
    publication_path.write_bytes(_canonical(publication))
    output = (tmp_path / "unit-input-rotation-receipt.json").resolve()
    runtime_calls: list[str] = []
    rotated: list[dict] = []
    transport = object()
    receipt = {"receipt_sha256": "c" * 64}

    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: (
            runtime_calls.append(revision)
            or _runtime_attestation(revision)
        ),
    )
    monkeypatch.setattr(
        owner,
        "build_production_cutover_owner_identity",
        lambda _revision: (object(), object(), object()),
    )
    monkeypatch.setattr(
        owner,
        "ProductionCutoverTransport",
        lambda *_args, **_kwargs: transport,
    )

    def rotate(**kwargs) -> dict:
        rotated.append(kwargs)
        return receipt

    monkeypatch.setattr(owner, "rotate_unit_input_authority", rotate)

    assert owner.main([
        "rotate-unit-inputs",
        "--revision",
        REVISION,
        "--remote-stager-revision",
        old_revision,
        "--publication",
        str(publication_path),
        "--output",
        str(output),
    ]) == 0

    assert runtime_calls == [REVISION]
    assert rotated == [{
        "release_revision": REVISION,
        "remote_stager_revision": old_revision,
        "publication": publication,
        "transport": transport,
    }]
    assert json.loads(output.read_bytes()) == receipt


def test_sealed_cli_exposes_only_prepare_and_resume_cutover_workflow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    key_path = _private_key_file(tmp_path, private)
    canary_prerequisite = _isolated_canary_goal_prerequisite()
    canary_prerequisite_path = (
        tmp_path / "isolated-canary-goal-prerequisite.json"
    ).resolve()
    canary_prerequisite_path.write_bytes(_canonical(canary_prerequisite))
    output = (tmp_path / "workflow-receipt.json").resolve()
    calls: list[dict] = []

    class Identity:
        owner_subject_sha256 = "a" * 64

    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: _runtime_attestation(revision),
    )
    monkeypatch.setattr(
        owner,
        "build_production_cutover_owner_identity",
        lambda revision: (Identity(), object(), object()),
    )
    monkeypatch.setattr(
        owner,
        "build_production_cutover_passkey_boundary",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        owner.canary_transport,
        "harden_owner_secret_process",
        lambda: None,
    )

    def execute(**kwargs):
        calls.append(kwargs)
        return {
            "schema": owner.WORKFLOW_RECEIPT_SCHEMA,
            "receipt_sha256": "f" * 64,
            "secret_material_recorded": False,
        }

    monkeypatch.setattr(owner, "execute_production_cutover_workflow", execute)

    common = [
        "--revision",
        REVISION,
        "--legacy-predecessor-revision",
        owner.LEGACY_F5_PREDECESSOR_REVISION,
        "--isolated-canary-goal-prerequisite",
        str(canary_prerequisite_path),
        "--owner-private-key",
        str(key_path),
        "--truth-mode",
        "start_new_truth_epoch",
        "--output",
        str(output),
    ]
    assert owner.main(["prepare-cutover", *common]) == 0

    assert len(calls) == 1
    assert calls[0]["release_revision"] == REVISION
    assert calls[0]["owner_subject_sha256"] == "a" * 64
    assert "host_authority_plan" not in calls[0]
    assert calls[0]["isolated_canary_goal_prerequisite"] == canary_prerequisite
    assert calls[0]["prepare_only"] is True
    assert callable(calls[0]["transport_factory"])
    assert output.exists()
    assert b"PRIVATE KEY" not in output.read_bytes()

    for forbidden in ("execute-cutover", "author-freeze", "author-cutover"):
        with pytest.raises(SystemExit):
            owner.main([forbidden, *common])


def test_sealed_cli_builds_exact_owner_signed_direct_mvp_waiver(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private = Ed25519PrivateKey.generate()
    key_path = _private_key_file(tmp_path, private)
    output = (tmp_path / "workflow-receipt.json").resolve()
    calls: list[dict] = []

    class Identity:
        owner_subject_sha256 = "a" * 64

    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: _runtime_attestation(revision),
    )
    monkeypatch.setattr(
        owner,
        "build_production_cutover_owner_identity",
        lambda revision: (Identity(), object(), object()),
    )
    monkeypatch.setattr(
        owner,
        "build_production_cutover_passkey_boundary",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        owner.canary_transport,
        "harden_owner_secret_process",
        lambda: None,
    )

    def execute(**kwargs):
        calls.append(kwargs)
        return {
            "schema": owner.WORKFLOW_RECEIPT_SCHEMA,
            "receipt_sha256": "f" * 64,
            "secret_material_recorded": False,
        }

    monkeypatch.setattr(owner, "execute_production_cutover_workflow", execute)

    assert owner.main([
        "prepare-cutover",
        "--revision",
        REVISION,
        "--legacy-predecessor-revision",
        owner.LEGACY_F5_PREDECESSOR_REVISION,
        "--owner-approved-direct-mvp-no-canary",
        "--owner-private-key",
        str(key_path),
        "--truth-mode",
        "start_new_truth_epoch",
        "--output",
        str(output),
    ]) == 0

    waiver = calls[0]["isolated_canary_goal_prerequisite"]
    assert waiver == cutover.build_owner_direct_mvp_no_canary_waiver(
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        owner_public_key_ed25519_hex=private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ).hex(),
    )
    assert calls[0]["prepare_only"] is True
    assert output.exists()
    assert b"PRIVATE KEY" not in output.read_bytes()


def test_direct_mvp_terminal_is_truthful_and_waiver_bound() -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    ).hex()
    waiver = cutover.build_owner_direct_mvp_no_canary_waiver(
        release_revision=REVISION,
        owner_subject_sha256="a" * 64,
        owner_public_key_ed25519_hex=public,
    )
    plan = _cutover_plan(
        private,
        Services(),
        release_validation_authority=waiver,
    )
    unsigned = {
        "schema": cutover.DIRECT_MVP_TERMINAL_SCHEMA,
        "plan_sha256": plan.sha256,
        "freeze_plan_sha256": plan.value["freeze_plan_sha256"],
        "freeze_approval_sha256": plan.value["freeze_approval_sha256"],
        "approval_sha256": "1" * 64,
        "final_tail_receipt_sha256": plan.value[
            "final_tail_receipt_sha256"
        ],
        "capability_prerequisite_receipt_sha256": "2" * 64,
        "capability_prerequisite_file_sha256": "3" * 64,
        "release_validation_mode": cutover.OWNER_DIRECT_MVP_VALIDATION_MODE,
        "release_validation_authority_sha256": waiver["waiver_sha256"],
        "isolated_canary_proof_present": False,
        "owner_approved_direct_mvp_no_canary": True,
        "live_production_prerequisite_validated": True,
        "zero_canonical_database_mutation_observed": True,
        "pre_db_zero_write_observation_sha256": "4" * 64,
        "capability_topology_identity_sha256": "5" * 64,
        "database_apply_receipt_sha256": "6" * 64,
        "host_apply_receipt_sha256": "7" * 64,
        "host_boot_commit_receipt_sha256": "8" * 64,
        "activation_commit_intent_receipt_sha256": "9" * 64,
        "alias_projection_package_sha256": "e" * 64,
        "alias_projection_activation_authority_sha256": "f" * 64,
        "alias_projection_activation_receipt_sha256": "0" * 64,
        "database_postflight_receipt_sha256": "a" * 64,
        "gateway_observation_sha256": "b" * 64,
        "writer_observation_sha256": "c" * 64,
        "connector_observation_sha256": "d" * 64,
        "direct_discord_disabled": True,
        "discord_dm_allowed": False,
        "rollback_used": False,
        "secret_material_recorded": False,
        "completed_at_unix": NOW,
    }
    terminal = {
        **unsigned,
        "receipt_sha256": cutover._sha256_json(unsigned),
    }
    assert owner._validate_terminal_receipt(
        terminal,
        plan=plan,
    ) == terminal

    tampered = copy.deepcopy(terminal)
    tampered["isolated_canary_proof_present"] = True
    tampered["receipt_sha256"] = cutover._sha256_json({
        key: item
        for key, item in tampered.items()
        if key != "receipt_sha256"
    })
    with pytest.raises(
        owner.OwnerCutoverError,
        match="owner_cutover_terminal_receipt_invalid",
    ):
        owner._validate_terminal_receipt(tampered, plan=plan)


def test_cloud_transport_commands_are_fixed_to_target_release() -> None:
    initial = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "collect-initial",
    )
    stage = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "stage-publication",
    )
    cron_stage = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "stage-cron-continuity",
    )
    apply = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "apply-cutover",
    )
    caddy_prepare = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "prepare-caddy-cutover",
    )
    caddy_commit = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "commit-caddy-cutover",
    )
    converge = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "converge-cutover",
    )
    interpreter = (
        "/opt/adventico-ai-platform/hermes-agent-releases/"
        f"hermes-agent-{REVISION[:12]}/.venv/bin/python"
    )

    assert interpreter in initial
    assert interpreter in stage
    assert interpreter in cron_stage
    assert interpreter in apply
    assert interpreter in caddy_prepare
    assert interpreter in caddy_commit
    assert interpreter in converge
    assert "scripts.canary.production_cutover_initial_collector" in initial
    assert "scripts.canary.production_cutover_public_stager" in stage
    assert "scripts.canary.stage_production_cron_continuity" in cron_stage
    assert "gateway.canonical_writer_production_cutover" in apply
    assert "scripts.canary.owner_gate_caddy_cutover" in caddy_prepare
    assert "scripts.canary.owner_gate_caddy_cutover" in caddy_commit
    assert "scripts.canary.owner_gate_caddy_cutover" in converge
    assert initial[-3:] == ("initial", "--revision", REVISION)
    assert cron_stage[-3:] == ("stage", "--revision", REVISION)
    assert apply[-1] == "apply-cutover"
    assert caddy_prepare[-1] == "prepare"
    assert caddy_commit[-1] == "commit"
    assert converge[-1] == "converge"


def _durable_workflow_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    convergence_tamper: bool = False,
):
    from tests.scripts.canary import (
        test_production_cutover_host_authority as workflow,
    )

    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: _runtime_attestation(revision),
    )
    monkeypatch.setattr(
        workflow.owner_gate_trust,
        "PINNED_RELEASE_TRUST_PUBLIC_KEY_SHA256",
        workflow._RELEASE_TRUST_KEY_ID,
    )
    monkeypatch.setattr(owner.secrets, "token_bytes", lambda _size: b"n" * 32)
    monkeypatch.setattr(
        owner.uuid,
        "uuid4",
        lambda: "11111111-1111-4111-8111-111111111111",
    )
    monkeypatch.setattr(
        "scripts.canary.production_cutover_public_stager.time.time",
        lambda: NOW,
    )
    monkeypatch.setattr(
        "gateway.canonical_writer_production_cutover.time.time",
        lambda: NOW,
    )
    services, initial, host, _host_plan = workflow._workflow_inputs()

    def bootstrap_activation(**kwargs) -> dict:
        freeze_plan = kwargs["freeze_plan"]
        unsigned = {
            "schema": owner.initial_bootstrap.ACTIVATION_RECEIPT_SCHEMA,
            "legacy_predecessor_revision": kwargs[
                "legacy_predecessor_revision"
            ],
            "release_revision": kwargs["release_revision"],
            "freeze_plan_sha256": freeze_plan["plan_sha256"],
            "freeze_approval_sha256": kwargs["freeze_approval"][
                "approval_sha256"
            ],
            "unit_input_authority_plan_sha256": "1" * 64,
            "unit_input_authority_approval_sha256": "2" * 64,
            "fixed_unit_inputs_sha256": "3" * 64,
            "owner_subject_sha256": freeze_plan["owner_subject_sha256"],
            "owner_public_key_ed25519_hex": freeze_plan[
                "owner_public_key_ed25519_hex"
            ],
            "owner_key_id": freeze_plan["owner_key_id"],
            "secret_material_recorded": False,
            "secret_digest_recorded": False,
        }
        return {
            **unsigned,
            "receipt_sha256": hashlib.sha256(_canonical(unsigned)).hexdigest(),
        }

    monkeypatch.setattr(
        owner.initial_bootstrap,
        "build_terminal_activation_receipt",
        bootstrap_activation,
    )

    class PasskeyBoundary:
        def __init__(self) -> None:
            self.request_id: str | None = None
            self.proof: dict | None = None
            self.consume_calls = 0

        def request(self, publication: dict) -> dict:
            action, self.proof = workflow._workflow_passkey_exchange(
                publication
            )
            self.request_id = action["request_id"]
            return {
                "request_id": self.request_id,
                "action_envelope_sha256": action["envelope_sha256"],
                "challenge_record_sha256": self.proof[
                    "challenge_record"
                ]["challenge_record_sha256"],
                "expires_at_unix": action["expires_at_unix"],
                "release_sha": REVISION,
                "plan_sha256": publication["documents"]["plan"][
                    "plan_sha256"
                ],
                "freeze_publication_sha256": publication[
                    "publication_sha256"
                ],
                "action_payload_sha256": action["action_payload_sha256"],
                "transaction_id": action["transaction_id"],
                "approval_url": (
                    f"{owner.cutover_passkey.protocol.PRODUCTION_ORIGIN}/"
                    f"approve/{self.request_id}"
                ),
                "passkey_only": True,
                "single_use": True,
                "control_plane_mutation_performed": True,
                "source_data_mutation_performed": False,
                "production_host_mutation_performed": False,
            }

        def consume(
            self,
            *,
            freeze_publication: dict,
            request_id: str,
            consume_attempt_id: str,
        ) -> dict:
            self.consume_calls += 1
            assert request_id == self.request_id
            assert self.proof is not None
            return {
                "request_id": request_id,
                "consume_attempt_id": consume_attempt_id,
                "disposition": "authorized_once",
                "passkey_proof": copy.deepcopy(self.proof),
                "release_sha": REVISION,
                "plan_sha256": freeze_publication["documents"]["plan"][
                    "plan_sha256"
                ],
                "single_use": True,
                "control_plane_mutation_performed": True,
                "source_data_mutation_performed": False,
                "production_host_mutation_performed": False,
            }

    class BridgeBootstrap:
        def __init__(self) -> None:
            self.request: dict | None = None

        def prepare(self, document: dict) -> dict:
            self.request = workflow._bridge_request(document)
            return self.request

        def consume_and_install(self, document: dict) -> dict:
            assert self.request is not None
            return workflow._bridge_receipt(document, self.request)

    class WorkflowTransport(workflow._WorkflowTransport):
        cutover_terminal: dict | None = None

        def invoke(self, revision: str, action: str, **kwargs) -> dict:
            if action == "collect-bootstrap-target":
                self.calls.append(action)
                assert self.cutover_terminal is not None
                assert self.cutover_plan is not None
                freeze = self.cutover_plan.value["freeze_plan"]
                unit_plan = {
                    "schema": owner.initial_bootstrap.unit_inputs_v4.PLAN_SCHEMA,
                    "release_revision": REVISION,
                    "plan_sha256": "1" * 64,
                    "owner_subject_sha256": freeze["owner_subject_sha256"],
                    "owner_public_key_ed25519_hex": freeze[
                        "owner_public_key_ed25519_hex"
                    ],
                    "owner_key_id": freeze["owner_key_id"],
                }
                unit_approval = {
                    "schema": (
                        owner.initial_bootstrap.unit_inputs_v4.APPROVAL_SCHEMA
                    ),
                    "release_revision": REVISION,
                    "approval_sha256": "2" * 64,
                }
                fixed_inputs = {
                    "schema": (
                        owner.initial_bootstrap.unit_inputs_v4.FIXED_INPUTS_SCHEMA
                    ),
                    "release_revision": REVISION,
                    "unit_input_authority_plan_sha256": "1" * 64,
                    "unit_input_authority_approval_sha256": "2" * 64,
                    "fixed_inputs_sha256": "3" * 64,
                }
                unsigned_host = {
                    "schema": owner.initial_bootstrap.HOST_RECEIPT_SCHEMA,
                    "legacy_predecessor_revision": (
                        owner.LEGACY_F5_PREDECESSOR_REVISION
                    ),
                    "release_revision": REVISION,
                    "consumer_unit_count": 79,
                    "fixed_unit_inputs_sha256": "3" * 64,
                    "unit_input_authority": {
                        "plan": unit_plan,
                        "approval": unit_approval,
                        "fixed_inputs": fixed_inputs,
                    },
                    "release_publication_receipt_sha256": "4" * 64,
                    "release_payload_tree_sha256": "5" * 64,
                    "release_consumer_set_sha256": "6" * 64,
                    "consumer_catalog_sha256": "7" * 64,
                    "immutable_unit_paths_sha256": "8" * 64,
                    "runtime_safety_plan": {
                        "runtime_safety_plan_sha256": "9" * 64,
                    },
                    "target_active_observation": {
                        "receipt_sha256": "0" * 64,
                    },
                    "trust_anchors": {
                        "alias_projection_package_sha256": "a" * 64,
                    },
                    "cutover_terminal_receipt": copy.deepcopy(
                        self.cutover_terminal
                    ),
                }
                return {
                    **unsigned_host,
                    "receipt_sha256": hashlib.sha256(
                        _canonical(unsigned_host)
                    ).hexdigest(),
                }
            if action != "converge-cutover":
                return super().invoke(revision, action, **kwargs)
            self.calls.append(action)
            assert self.cutover_plan is not None
            validation_mode, validation_authority = (
                cutover._validate_release_validation_authority(
                    self.cutover_plan.value["freeze_plan"][
                        "cutover_authority"
                    ]["isolated_canary_goal_prerequisite"],
                    revision=REVISION,
                )
            )
            assert validation_mode == "release_bound_isolated_canary"
            equivalence = cutover._build_production_isolation_equivalence(
                plan=self.cutover_plan,
                evidence=validation_authority,
            )
            terminal_unsigned = {
                "schema": cutover.TERMINAL_SCHEMA,
                "plan_sha256": self.cutover_plan.sha256,
                "freeze_plan_sha256": self.cutover_plan.value[
                    "freeze_plan_sha256"
                ],
                "freeze_approval_sha256": self.cutover_plan.value[
                    "freeze_approval_sha256"
                ],
                "approval_sha256": "1" * 64,
                "final_tail_receipt_sha256": self.cutover_plan.value[
                    "final_tail_receipt_sha256"
                ],
                "capability_prerequisite_receipt_sha256": "2" * 64,
                "capability_prerequisite_file_sha256": "3" * 64,
                "zero_canonical_database_mutation_observed": True,
                "pre_db_zero_write_observation_sha256": "4" * 64,
                "capability_topology_identity_sha256": "5" * 64,
                "database_apply_receipt_sha256": "6" * 64,
                "host_apply_receipt_sha256": "7" * 64,
                "host_boot_commit_receipt_sha256": "8" * 64,
                "activation_commit_intent_receipt_sha256": "9" * 64,
                "alias_projection_package_sha256": "a" * 64,
                "alias_projection_activation_authority_sha256": "b" * 64,
                "alias_projection_activation_receipt_sha256": "c" * 64,
                "database_postflight_receipt_sha256": "d" * 64,
                "gateway_observation_sha256": "e" * 64,
                "writer_observation_sha256": "f" * 64,
                "connector_observation_sha256": "0" * 64,
                "direct_discord_disabled": True,
                "discord_dm_allowed": False,
                "rollback_used": False,
                "secret_material_recorded": False,
                "completed_at_unix": NOW,
                "isolated_canary_goal_continuation_terminal_sha256": (
                    validation_authority[
                        "goal_continuation_terminal_sha256"
                    ]
                ),
                "isolated_canary_workspace_gateway_receipt_sha256": (
                    validation_authority["workspace_gateway_receipt_sha256"]
                ),
                "isolation_equivalence_projection_sha256": equivalence[
                    "projection_sha256"
                ],
            }
            self.cutover_terminal = {
                **terminal_unsigned,
                "receipt_sha256": hashlib.sha256(
                    _canonical(terminal_unsigned)
                ).hexdigest(),
            }
            unsigned = {
                "schema": owner.CONVERGENCE_SCHEMA,
                "release_revision": REVISION,
                "freeze_plan_sha256": self.cutover_plan.value[
                    "freeze_plan_sha256"
                ],
                "cutover_plan_sha256": self.cutover_plan.sha256,
                "preflight_receipt_sha256": "1" * 64,
                "caddy_prepare_receipt_sha256": "2" * 64,
                "maintenance_arm_receipt_sha256": "3" * 64,
                "cutover_terminal_receipt_sha256": self.cutover_terminal[
                    "receipt_sha256"
                ],
                "caddy_terminal_receipt_sha256": "5" * 64,
                "caddy_outcome": "private_v2_active",
                "legacy_service_retirement_receipt_sha256": "6" * 64,
                "control_plane_mutation_performed": True,
                "source_data_mutation_performed": True,
                "production_host_mutation_performed": True,
                "secret_material_recorded": False,
                "secret_digest_recorded": False,
            }
            if convergence_tamper:
                unsigned["source_data_mutation_performed"] = False
            receipt = {
                **unsigned,
                "receipt_sha256": hashlib.sha256(
                    _canonical(unsigned)
                ).hexdigest(),
            }
            self.convergence_receipt = copy.deepcopy(receipt)
            return receipt

    boundary = PasskeyBoundary()
    bridge = BridgeBootstrap()
    prepare_transport = workflow._WorkflowTransport(initial, host, services)
    transport = WorkflowTransport(initial, host, services)
    workspace = owner.execute_production_cutover_workflow(
        release_revision=REVISION,
        legacy_predecessor_revision=owner.LEGACY_F5_PREDECESSOR_REVISION,
        owner_identity=object(),
        owner_subject_sha256="a" * 64,
        private_key=Ed25519PrivateKey.generate(),
        isolated_canary_goal_prerequisite=(
            _isolated_canary_goal_prerequisite()
        ),
        truth_mode="start_new_truth_epoch",
        revision_ancestry_checker=lambda predecessor, target: (
            predecessor == owner.LEGACY_F5_PREDECESSOR_REVISION
            and target == REVISION
        ),
        passkey_boundary=boundary,
        prepare_only=True,
        transport_factory=lambda _identity: prepare_transport,
        database_recovery_gate_runner=workflow._recovery_gate_runner,
        now_unix=NOW,
    )
    for _expected_state in (
        "awaiting_bridge_passkey",
        "awaiting_cutover_passkey",
        "passkey_claim_recorded",
        "cutover_staged",
    ):
        workspace = owner.resume_prepared_production_cutover_workflow(
            workspace=workspace,
            owner_identity=object(),
            passkey_boundary=boundary,
            bridge_bootstrap=bridge,
            transport_factory=lambda _identity: transport,
            now_unix=NOW,
        )
        assert workspace["state"] == _expected_state
    return workspace, transport, boundary, bridge


def test_durable_cutover_workspace_stops_before_convergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, transport, boundary, bridge = _durable_workflow_fixture(
        monkeypatch
    )

    assert workspace["state"] == "cutover_staged"
    assert all(
        isinstance(workspace[name], dict)
        for name in (
            "final_tail_receipt",
            "stopped_collector_receipt",
            "cron_continuity_stage_receipt",
            "cutover_plan",
            "cutover_publication",
            "cutover_stage_receipt",
        )
    )
    assert transport.calls == [
        "stage-publication",
        "capture-final-tail",
        "collect-stopped",
        "stage-cron-continuity",
        "stage-publication",
    ]
    assert "phase-b-preflight" not in transport.calls
    assert "prepare-caddy-cutover" not in transport.calls
    assert "apply-cutover" not in transport.calls
    assert "commit-caddy-cutover" not in transport.calls

    receipt = owner.resume_prepared_production_cutover_workflow(
        workspace=workspace,
        owner_identity=object(),
        passkey_boundary=boundary,
        bridge_bootstrap=bridge,
        transport_factory=lambda _identity: transport,
        now_unix=NOW + 3_600,
    )

    assert transport.calls[-2:] == [
        "converge-cutover",
        "collect-bootstrap-target",
    ]
    assert transport.calls.count("converge-cutover") == 1
    assert receipt["schema"] == owner.initial_bootstrap.TERMINAL_ENVELOPE_SCHEMA
    workflow_receipt = receipt["workflow_receipt"]
    assert workflow_receipt["schema"] == owner.WORKFLOW_RECEIPT_SCHEMA
    assert workflow_receipt["convergence_receipt_sha256"] == (
        transport.convergence_receipt["receipt_sha256"]
    )
    assert workflow_receipt["terminal_receipt_sha256"] == (
        transport.cutover_terminal["receipt_sha256"]
    )
    assert workflow_receipt["legacy_service_retirement_receipt_sha256"] == "6" * 64
    assert workflow_receipt["caddy_outcome"] == "private_v2_active"
    assert workflow_receipt["gates"][-1]["stage"] == "cutover_convergence_accepted"
    assert receipt["predecessor_trust"]["activation_receipt_sha256"] == (
        receipt["activation_receipt"]["receipt_sha256"]
    )
    assert boundary.consume_calls == 1

    replayed = owner.resume_prepared_production_cutover_workflow(
        workspace=workspace,
        owner_identity=object(),
        passkey_boundary=boundary,
        bridge_bootstrap=bridge,
        transport_factory=lambda _identity: transport,
        now_unix=NOW + 7_200,
    )

    assert replayed == receipt
    assert transport.calls.count("converge-cutover") == 2
    assert boundary.consume_calls == 1


def test_terminal_workflow_receipt_rejects_rebound_retirement_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, transport, boundary, bridge = _durable_workflow_fixture(
        monkeypatch
    )
    receipt = owner.resume_prepared_production_cutover_workflow(
        workspace=workspace,
        owner_identity=object(),
        passkey_boundary=boundary,
        bridge_bootstrap=bridge,
        transport_factory=lambda _identity: transport,
        now_unix=NOW,
    )
    changed = copy.deepcopy(receipt["workflow_receipt"])
    changed["legacy_service_retirement_receipt_sha256"] = "f" * 64
    changed["receipt_sha256"] = hashlib.sha256(_canonical({
        name: item
        for name, item in changed.items()
        if name != "receipt_sha256"
    })).hexdigest()

    with pytest.raises(
        owner.OwnerCutoverError,
        match="workflow_receipt_invalid",
    ):
        owner._validate_workflow_receipt(
            changed,
            release_revision=REVISION,
            freeze_plan_sha256=workspace["freeze_plan"]["plan_sha256"],
            freeze_approval_sha256=workspace["freeze_approval"][
                "approval_sha256"
            ],
            cutover_plan_sha256=workspace["cutover_plan"]["plan_sha256"],
            convergence=transport.convergence_receipt,
            gates=receipt["workflow_receipt"]["gates"],
        )


@pytest.mark.parametrize(
    ("override", "ancestry"),
    (
        ({"legacy_predecessor_revision": "0" * 40}, True),
        ({"truth_mode": "reseed_accepted_events"}, True),
        ({"accepted_event_receipts": []}, True),
        ({}, False),
    ),
)
def test_initial_bootstrap_rejects_non_exact_semantic_authority(
    override: dict,
    ancestry: bool,
) -> None:
    arguments = {
        "release_revision": REVISION,
        "legacy_predecessor_revision": (
            owner.LEGACY_F5_PREDECESSOR_REVISION
        ),
        "owner_identity": object(),
        "owner_subject_sha256": "a" * 64,
        "private_key": Ed25519PrivateKey.generate(),
        "isolated_canary_goal_prerequisite": {},
        "truth_mode": "start_new_truth_epoch",
        "accepted_event_receipts": None,
        "transport_factory": lambda _identity: object(),
        "database_recovery_gate_runner": lambda **_kwargs: {},
        "revision_ancestry_checker": (
            lambda _predecessor, _target: ancestry
        ),
    }
    arguments.update(override)

    with pytest.raises(
        owner.OwnerCutoverError,
        match="owner_cutover_workflow_input_invalid",
    ):
        owner.execute_production_cutover_workflow(**arguments)


def test_cutover_staged_tamper_rejects_before_remote_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, transport, boundary, bridge = _durable_workflow_fixture(
        monkeypatch
    )
    changed = copy.deepcopy(workspace)
    changed["cutover_plan"]["freeze_plan_sha256"] = "f" * 64
    changed["workspace_sha256"] = hashlib.sha256(_canonical({
        name: item
        for name, item in changed.items()
        if name != "workspace_sha256"
    })).hexdigest()
    before = list(transport.calls)

    with pytest.raises(owner.OwnerCutoverError, match="workspace_invalid"):
        owner.resume_prepared_production_cutover_workflow(
            workspace=changed,
            owner_identity=object(),
            passkey_boundary=boundary,
            bridge_bootstrap=bridge,
            transport_factory=lambda _identity: transport,
            now_unix=NOW,
        )

    assert transport.calls == before


def test_combined_convergence_tamper_fails_closed_after_one_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, transport, boundary, bridge = _durable_workflow_fixture(
        monkeypatch,
        convergence_tamper=True,
    )

    with pytest.raises(
        owner.OwnerCutoverError,
        match="convergence_receipt_invalid",
    ):
        owner.resume_prepared_production_cutover_workflow(
            workspace=workspace,
            owner_identity=object(),
            passkey_boundary=boundary,
            bridge_bootstrap=bridge,
            transport_factory=lambda _identity: transport,
            now_unix=NOW,
        )

    assert transport.calls.count("converge-cutover") == 1


class _TrustedGcloud:
    def trusted_command_prefix(self) -> tuple[str, ...]:
        return (
            "/trusted/python",
            *canary_transport._GCLOUD_PYTHON_ISOLATION_ARGS,
            "/trusted/gcloud.py",
        )


class _StableConfiguration:
    def assert_stable(self) -> None:
        return None

    def environment_values(self) -> dict[str, str]:
        return {
            "HOME": "/trusted/home",
            "CLOUDSDK_CONFIG": "/trusted/gcloud-config",
        }


class _ProductionKnownHosts:
    public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEZha2VLZXlGb3JUZXN0c09ubHkxMjM0NTY owner"

    def absolute_path(self) -> str:
        return "/trusted/google_compute_known_hosts"

    def private_key_path(self) -> str:
        return "/trusted/google_compute_engine"

    def public_key_line(self) -> str:
        return self.public_key


class _OwnerIdentity:
    def __init__(self) -> None:
        self.checks = 0

    def account_for_read_only_preflight(self) -> str:
        self.checks += 1
        return "owner@example.com"

    def require_stable(self) -> None:
        self.checks += 1


def _production_transport(*, runner=subprocess.run):
    return owner.ProductionCutoverTransport(
        _OwnerIdentity(),
        gcloud_executable=_TrustedGcloud(),
        gcloud_configuration=_StableConfiguration(),
        known_hosts=_ProductionKnownHosts(),
        preflight_runner=runner,
    )


def test_production_transport_exposes_fixed_unit_input_rotation_edge() -> None:
    remote = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "rotate-unit-input-authority",
    )

    assert remote[-3:] == (
        owner.ProductionCutoverTransport._ROTATION_STAGER_WRAPPER,
        REVISION,
        "rotate-unit-input-authority",
    )
    assert "muncho-canary-v2-01" not in " ".join(remote)


@pytest.mark.parametrize(
    "action",
    (
        "prepare-release-unit-inputs",
        "preauthorize-release-unit-inputs",
    ),
)
def test_production_transport_release_rotation_phases_use_fixed_stager(
    action: str,
) -> None:
    remote = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        action,
    )

    assert remote[-3:] == (
        owner.ProductionCutoverTransport._ROTATION_STAGER_WRAPPER,
        REVISION,
        action,
    )
    assert not any(
        item.endswith("/.venv/bin/python") for item in remote
    )


def test_production_transport_full_argv_is_sealed_away_from_canary() -> None:
    transport = _production_transport()
    remote = owner.ProductionCutoverTransport._remote_command(
        REVISION,
        "stage-publication",
    )
    actual = transport._remote_argv(remote, account="owner@example.com")
    prefix = _TrustedGcloud().trusted_command_prefix()
    expected = (
        *prefix,
        "compute",
        "ssh",
        "lomliev_adventico_com@ai-platform-runtime-01",
        "--project=adventico-ai-platform",
        "--zone=europe-west3-a",
        "--account=owner@example.com",
        "--plain",
        "--tunnel-through-iap",
        "--quiet",
        "--command="
        + shlex.join(("/usr/bin/sudo", "--non-interactive", "--", *remote)),
        *owner.ProductionCutoverTransport._ssh_flags(
            "/trusted/google_compute_known_hosts",
            "/trusted/google_compute_engine",
        ),
    )

    assert actual == expected
    joined = " ".join(actual)
    assert "ai-platform-runtime-01" in joined
    assert owner.PRODUCTION_VM_INSTANCE_ID not in joined
    assert "muncho-canary-v2-01" not in joined
    assert canary_transport.VM_INSTANCE_ID not in joined


def test_every_production_remote_action_keeps_the_same_sealed_target() -> None:
    transport = _production_transport()
    for action in sorted(owner.ProductionCutoverTransport._ACTIONS):
        remote = transport._remote_command(REVISION, action)
        argv = transport._remote_argv(remote, account="owner@example.com")
        assert argv.count(
            "lomliev_adventico_com@ai-platform-runtime-01"
        ) == 1
        assert argv.count("--project=adventico-ai-platform") == 1
        assert argv.count("--zone=europe-west3-a") == 1
        assert "muncho-canary-v2-01" not in " ".join(argv)


def _known_host_line(instance_id: str) -> bytes:
    algorithm = b"ssh-ed25519"
    blob = (
        struct.pack(">I", len(algorithm))
        + algorithm
        + struct.pack(">I", 32)
        + b"k" * 32
    )
    return (
        f"compute.{instance_id} ssh-ed25519 ".encode("ascii")
        + base64.b64encode(blob)
        + b"\n"
    )


def test_production_known_hosts_requires_exact_production_instance_id(
    tmp_path: Path,
) -> None:
    ssh_root = tmp_path / ".ssh"
    ssh_root.mkdir(mode=0o700)
    known_hosts = ssh_root / "google_compute_known_hosts"
    private_key = ssh_root / "google_compute_engine"
    public_key = ssh_root / "google_compute_engine.pub"
    production_line = _known_host_line(owner.PRODUCTION_VM_INSTANCE_ID)
    known_hosts.write_bytes(production_line)
    known_hosts.chmod(0o644)
    private_key.write_bytes(b"private-key-fixture")
    private_key.chmod(0o600)
    public_key.write_bytes(b"ssh-ed25519 public-key-fixture\n")
    public_key.chmod(0o644)

    pinned = owner.PinnedProductionGoogleComputeKnownHosts(
        path=known_hosts,
        private_key=private_key,
        public_key=public_key,
    )
    assert pinned.server_host_key_line(owner.PRODUCTION_VM_INSTANCE_ID) == (
        production_line.decode("ascii").rstrip("\n")
    )
    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="trusted_known_hosts_instance_changed",
    ):
        pinned.server_host_key_line(canary_transport.VM_INSTANCE_ID)

    known_hosts.write_bytes(_known_host_line(canary_transport.VM_INSTANCE_ID))
    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="trusted_known_hosts_invalid",
    ):
        owner.PinnedProductionGoogleComputeKnownHosts(
            path=known_hosts,
            private_key=private_key,
            public_key=public_key,
        )


def _identity_responses(
    *,
    instance_id: str = owner.PRODUCTION_VM_INSTANCE_ID,
    enable_oslogin: str = "TRUE",
    include_metadata_key: bool = False,
) -> tuple[dict, dict, dict]:
    metadata = [{"key": "enable-oslogin", "value": enable_oslogin}]
    if include_metadata_key:
        metadata.append({"key": "ssh-keys", "value": "legacy-key"})
    fingerprint = "a" * 64
    instance = {
        "id": instance_id,
        "name": owner.PRODUCTION_VM_NAME,
        "zone": (
            "https://www.googleapis.com/compute/v1/projects/"
            f"{owner.PRODUCTION_PROJECT}/zones/{owner.PRODUCTION_ZONE}"
        ),
        "metadata": {"items": metadata},
    }
    project = {
        "name": owner.PRODUCTION_PROJECT,
        "commonInstanceMetadata": {"items": []},
    }
    profile = {
        "name": owner.PRODUCTION_OS_LOGIN_PROFILE_ID,
        "posixAccounts": [{
            "uid": "718639321",
            "gid": "718639321",
            "username": owner.PRODUCTION_OS_LOGIN_USERNAME,
            "homeDirectory": f"/home/{owner.PRODUCTION_OS_LOGIN_USERNAME}",
            "operatingSystemType": "LINUX",
            "primary": True,
        }],
        "sshPublicKeys": {
            fingerprint: {
                "fingerprint": fingerprint,
                "key": _ProductionKnownHosts.public_key,
            }
        },
    }
    return instance, project, profile


def _identity_runner(responses: tuple[dict, dict, dict], calls: list[tuple[str, ...]]):
    instance, project, profile = responses

    def run(argv, **_kwargs):
        exact = tuple(argv)
        calls.append(exact)
        if "instances" in exact and "describe" in exact:
            value = instance
        elif "project-info" in exact:
            value = project
        elif "os-login" in exact:
            value = profile
        else:
            raise AssertionError(f"unexpected read-only preflight: {exact!r}")
        return subprocess.CompletedProcess(
            exact,
            0,
            stdout=_canonical(value),
            stderr=b"",
        )

    return run


def test_production_identity_preflight_binds_instance_os_login_and_profile() -> None:
    calls: list[tuple[str, ...]] = []
    transport = _production_transport(
        runner=_identity_runner(_identity_responses(), calls)
    )

    snapshot = transport._authorization_snapshot("owner@example.com")

    assert len(snapshot) == 3
    assert all(len(item) == 64 for item in snapshot)
    assert len(calls) == 3
    joined = "\n".join(" ".join(call) for call in calls)
    assert "ai-platform-runtime-01" in joined
    assert "--project=adventico-ai-platform" in joined
    assert "--zone=europe-west3-a" in joined
    assert "muncho-canary-v2-01" not in joined


def test_production_identity_parallelism_is_exact_process_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _identity_responses()
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    calls: list[tuple[str, ...]] = []
    worker_ids: set[int] = set()

    def exact_process_run(argv, **_kwargs):
        exact = tuple(argv)
        with lock:
            calls.append(exact)
            worker_ids.add(threading.get_ident())
        barrier.wait(timeout=2.0)
        if "instances" in exact:
            value = responses[0]
        elif "project-info" in exact:
            value = responses[1]
        else:
            value = responses[2]
        return subprocess.CompletedProcess(
            exact,
            0,
            stdout=_canonical(value),
            stderr=b"",
        )

    monkeypatch.setattr(owner.subprocess, "run", exact_process_run)
    transport = _production_transport(runner=owner.subprocess.run)

    snapshot = transport._authorization_snapshot("owner@example.com")

    assert len(snapshot) == 3
    assert len(calls) == 3
    assert len(worker_ids) == 3
    assert transport._process_isolated_preflight_runner is True

    injected_calls: list[tuple[str, ...]] = []
    injected_threads: list[int] = []

    def injected_runner(argv, **kwargs):
        injected_threads.append(threading.get_ident())
        return _identity_runner(
            responses,
            injected_calls,
        )(argv, **kwargs)

    injected = _production_transport(runner=injected_runner)
    injected._authorization_snapshot("owner@example.com")
    assert injected._process_isolated_preflight_runner is False
    assert injected_threads == [threading.get_ident()] * 3
    assert [
        "instance"
        if "instances" in call
        else "project"
        if "project-info" in call
        else "profile"
        for call in injected_calls
    ] == ["instance", "project", "profile"]


def test_production_identity_parallel_failure_joins_every_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = _identity_responses()
    barrier = threading.Barrier(3)
    lock = threading.Lock()
    active = 0
    calls = 0

    def exact_process_run(argv, **_kwargs):
        nonlocal active, calls
        exact = tuple(argv)
        with lock:
            active += 1
            calls += 1
        try:
            barrier.wait(timeout=2.0)
            if "project-info" in exact:
                raise OSError("bounded fixture failure")
            value = responses[0] if "instances" in exact else responses[2]
            return subprocess.CompletedProcess(
                exact,
                0,
                stdout=_canonical(value),
                stderr=b"",
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(owner.subprocess, "run", exact_process_run)
    transport = _production_transport(runner=owner.subprocess.run)

    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="iap_ssh_preflight_unavailable",
    ):
        transport._authorization_snapshot("owner@example.com")

    assert calls == 3
    assert active == 0


@pytest.mark.parametrize(
    "responses",
    (
        _identity_responses(instance_id="9153645328899914617"),
        _identity_responses(enable_oslogin="FALSE"),
        _identity_responses(include_metadata_key=True),
    ),
)
def test_production_identity_preflight_fails_closed(responses) -> None:
    transport = _production_transport(runner=_identity_runner(responses, []))

    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="iap_ssh_authorization_invalid",
    ):
        transport._authorization_snapshot("owner@example.com")


def _production_ingress_remote(
    transport: owner.ProductionCutoverTransport,
) -> tuple[str, ...]:
    return (
        *transport._fixed_remote_environment(chdir="/"),
        "/usr/bin/python3",
        "-B",
        "-I",
        "-",
        "inert",
        "--release-revision",
        REVISION,
        "--plan-sha256",
        "1" * 64,
    )


def test_production_ingress_bound_transport_keeps_three_distinct_fences() -> None:
    transport = _production_transport()
    events: list[str] = []
    snapshot = ("1" * 64, "2" * 64, "3" * 64)
    transport._owner_identity.require_stable = lambda: events.append("stable")
    transport._authorization_snapshot = lambda _account: (
        events.append("authorization") or snapshot
    )
    transport._remote_argv = lambda _remote, *, account: (
        events.append(f"argv:{account}") or ("sealed-gcloud", "ssh")
    )
    transport._validate_dry_run = lambda _argv: events.append("dry-run")
    transport._postflight = lambda: events.append("postflight")

    def observe(argv, **kwargs):
        assert kwargs["input"] == b"reviewed-observer"
        assert kwargs["timeout"] == 120.0
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        events.append("remote-observer")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=b'{"ok":true}\n',
            stderr=b"",
        )

    transport._preflight_runner = observe
    completed, authority = transport._run_production_ingress_observer_input(
        _production_ingress_remote(transport),
        account="owner@example.com",
        input_bytes=b"reviewed-observer",
    )

    assert completed.returncode == 0
    assert authority == snapshot
    assert events == [
        "stable",
        "authorization",
        "argv:owner@example.com",
        "dry-run",
        "authorization",
        "remote-observer",
        "postflight",
        "stable",
        "authorization",
    ]


def test_production_ingress_bound_transport_rejects_pre_remote_drift() -> None:
    transport = _production_transport()
    snapshots = iter(
        [
            ("1" * 64, "2" * 64, "3" * 64),
            ("4" * 64, "5" * 64, "6" * 64),
        ]
    )
    remote_calls = 0
    transport._owner_identity.require_stable = lambda: None
    transport._authorization_snapshot = lambda _account: next(snapshots)
    transport._remote_argv = lambda _remote, *, account: ("gcloud", account)
    transport._validate_dry_run = lambda _argv: None

    def reject_remote(*_args, **_kwargs):
        nonlocal remote_calls
        remote_calls += 1
        raise AssertionError("remote observer must not run")

    transport._preflight_runner = reject_remote
    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="iap_ssh_authorization_changed",
    ):
        transport._run_production_ingress_observer_input(
            _production_ingress_remote(transport),
            account="owner@example.com",
            input_bytes=b"reviewed-observer",
        )
    assert remote_calls == 0


def test_production_ingress_bound_transport_rejects_post_remote_drift() -> None:
    transport = _production_transport()
    snapshots = iter(
        [
            ("1" * 64, "2" * 64, "3" * 64),
            ("1" * 64, "2" * 64, "3" * 64),
            ("4" * 64, "5" * 64, "6" * 64),
        ]
    )
    events: list[str] = []
    transport._owner_identity.require_stable = lambda: events.append("stable")
    transport._authorization_snapshot = lambda _account: next(snapshots)
    transport._remote_argv = lambda _remote, *, account: ("gcloud", account)
    transport._validate_dry_run = lambda _argv: None
    transport._postflight = lambda: events.append("postflight")

    def observe(argv, **_kwargs):
        events.append("remote-observer")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=b'{"ok":true}\n',
            stderr=b"",
        )

    transport._preflight_runner = observe
    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="iap_ssh_authorization_changed",
    ):
        transport._run_production_ingress_observer_input(
            _production_ingress_remote(transport),
            account="owner@example.com",
            input_bytes=b"reviewed-observer",
        )
    assert events == [
        "stable",
        "remote-observer",
        "postflight",
        "stable",
    ]


def test_production_ingress_bound_transport_rejects_oversized_input() -> None:
    transport = _production_transport()
    transport._owner_identity.require_stable = lambda: pytest.fail(
        "invalid input must fail before any authority read"
    )
    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="production_ingress_observer_input_invalid",
    ):
        transport._run_production_ingress_observer_input(
            _production_ingress_remote(transport),
            account="owner@example.com",
            input_bytes=b"x"
            * (transport._PRODUCTION_INGRESS_INPUT_MAX_BYTES + 1),
        )


def test_production_transport_performs_identity_preflight_before_mutation() -> None:
    transport = _production_transport()
    events: list[str] = []
    snapshot = ("1" * 64, "2" * 64, "3" * 64)
    transport._authorization_snapshot = lambda _account: (
        events.append("identity") or snapshot
    )
    transport._validate_dry_run = lambda _argv: events.append("dry-run")
    transport._postflight = lambda: events.append("postflight")

    def mutate(argv, **_kwargs):
        events.append("remote-mutation")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=b'{"ok":true}\n',
            stderr=b"",
        )

    transport._preflight_runner = mutate
    remote = transport._remote_command(REVISION, "stage-publication")
    completed = transport._run_remote_input(
        remote,
        account="owner@example.com",
        input_bytes=b'{"action":"freeze"}',
    )

    assert completed.returncode == 0
    assert events == [
        "identity",
        "dry-run",
        "identity",
        "remote-mutation",
        "postflight",
        "identity",
    ]


def test_production_transport_invoke_uses_supported_remote_bounds() -> None:
    transport = _production_transport()
    captured: dict[str, object] = {}

    def run_remote_input(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"ok":true}\n',
            stderr=b"",
        )

    transport._run_remote_input = run_remote_input

    assert transport.invoke(
        REVISION,
        "stage-publication",
        publication={"action": "unit-input-authority"},
    ) == {"ok": True}
    assert captured["maximum_input_bytes"] == (
        canary_transport._STOPPED_RELEASE_REMOTE_INPUT_MAX_BYTES
    )
    assert captured["maximum_output_bytes"] == (
        canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
    )
    assert len(captured["input_bytes"]) < captured["maximum_input_bytes"]


def test_production_transport_installs_exact_root_wrapped_guest_without_sudoers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _production_transport()
    request = storage_installer.build_guest_install_request(REVISION)
    captured: dict[str, object] = {}
    source_root = "/opt/muncho/releases/" + REVISION

    monkeypatch.setattr(
        transport,
        "_prepare_source",
        lambda revision, *, account: (
            captured.update(revision=revision, account=account) or source_root
        ),
    )
    unsigned = {
        "schema": storage_installer.GUEST_READINESS_SCHEMA,
        "release_sha": REVISION,
        "entrypoint": str(storage_installer.GUEST_ENTRYPOINT),
        "entrypoint_sha256": request["guest_source_sha256"],
        "entrypoint_uid": 0,
        "entrypoint_gid": 0,
        "entrypoint_mode": "0755",
        "entrypoint_link_count": 1,
        "installer_sha256": request["installer_sha256"],
        "interpreter_path": "/usr/bin/python3",
        "interpreter_resolved_path": "/usr/bin/python3.11",
        "interpreter_sha256": "8" * 64,
        "installation_receipt_sha256": "1" * 64,
        "sudoers_path": str(storage_installer.GUEST_SUDOERS_PATH),
        "sudoers_required": False,
        "sudoers_absent": True,
        "root_transport_required": True,
        "ready": True,
    }
    readiness = {
        **unsigned,
        "readiness_sha256": storage_installer.sha256_json(unsigned),
    }

    def run_remote_input(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=storage_installer.canonical_json_bytes(readiness) + b"\n",
            stderr=b"",
        )

    monkeypatch.setattr(transport, "_run_remote_input", run_remote_input)

    assert transport.install_production_storage_growth_guest(
        release_sha=REVISION,
        request=request,
        account="owner@example.com",
    ) == readiness
    assert captured["revision"] == REVISION
    assert captured["account"] == "owner@example.com"
    assert captured["command"] == (
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        f"{source_root}/scripts/canary/production_storage_growth_installer.py",
        "install-guest",
    )
    assert captured["input_bytes"] == storage_installer.canonical_json_bytes(
        request
    )
    assert captured["maximum_input_bytes"] == 64 * 1024
    assert captured["maximum_output_bytes"] == 64 * 1024
    assert captured["timeout_seconds"] == 300.0
    assert "sudoers" not in " ".join(captured["command"])


def test_production_transport_non_input_uses_supported_remote_bounds() -> None:
    transport = _production_transport()
    captured: dict[str, object] = {}

    def run_remote(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"ok":true}\n',
            stderr=b"",
        )

    transport._run_remote = run_remote

    assert transport.invoke(REVISION, "collect-initial") == {"ok": True}
    assert captured["timeout_seconds"] == 2_400
    assert captured["maximum_output_bytes"] == (
        canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
    )


def test_stopped_release_transport_accepts_opt_in_live_sized_response() -> None:
    transport = _production_transport()
    snapshot = ("1" * 64, "2" * 64, "3" * 64)
    transport._authorization_snapshot = lambda _account: snapshot
    transport._validate_dry_run = lambda _argv: None
    transport._postflight = lambda: None
    response = _canonical({"data": "x" * 2_700_000}) + b"\n"
    calls: list[tuple[tuple[str, ...], dict]] = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=response,
            stderr=b"",
        )

    transport._preflight_runner = run
    remote = transport._remote_command(REVISION, "collect-initial")
    completed = transport._run_remote(
        remote,
        account="owner@example.com",
        maximum_output_bytes=(
            canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
        ),
    )

    assert (
        canary_transport._HTTP_RESPONSE_MAX_BYTES
        < len(response)
        <= canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
    )
    assert completed.stdout == response
    assert len(calls) == 1


def test_stopped_release_transport_accepts_opt_in_live_sized_iam_response() -> None:
    transport = _production_transport()
    snapshot = ("1" * 64, "2" * 64, "3" * 64)
    transport._authorization_snapshot = lambda _account: snapshot
    transport._validate_dry_run = lambda _argv: None
    transport._postflight = lambda: None
    response = _canonical({"data": "x" * 2_700_000}) + b"\n"
    calls: list[tuple[tuple[str, ...], dict]] = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=response,
            stderr=b"",
        )

    transport._preflight_runner = run
    remote = transport._remote_command(REVISION, "stage-publication")
    completed = transport._run_remote_input(
        remote,
        account="owner@example.com",
        input_bytes=b'{"action":"freeze"}',
        maximum_output_bytes=(
            canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
        ),
    )

    assert (
        canary_transport._HTTP_RESPONSE_MAX_BYTES
        < len(response)
        <= canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES
    )
    assert completed.stdout == response
    assert len(calls) == 1
    assert calls[0][1]["input"] == b'{"action":"freeze"}'


def test_stopped_release_transport_rejects_excessive_output_limit_before_runner() -> None:
    transport = _production_transport()
    runner_calls: list[tuple[str, ...]] = []

    def run(argv, **_kwargs):
        runner_calls.append(tuple(argv))
        raise AssertionError("runner must not be called")

    transport._preflight_runner = run
    remote = transport._remote_command(REVISION, "stage-publication")

    with pytest.raises(
        canary_transport.OwnerLauncherError,
        match="stopped_release_remote_input_invalid",
    ):
        transport._run_remote_input(
            remote,
            account="owner@example.com",
            input_bytes=b'{"action":"freeze"}',
            maximum_output_bytes=(
                canary_transport._STOPPED_RELEASE_REMOTE_OUTPUT_MAX_BYTES + 1
            ),
        )

    assert runner_calls == []


def test_successor_rebind_owner_action_is_closed_and_runtime_gated(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    launch_authority_sha256 = "5" * 64
    calls: list[tuple[str, str]] = []
    result = {"schema": "fixed-successor-result", "ok": True}
    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: calls.append(("runtime", revision)) or {"ok": True},
    )
    monkeypatch.setattr(
        successor_rebind,
        "owner_apply_framed_stdin",
        lambda *, expected_launch_authority_sha256: (
            calls.append(("dispatch", expected_launch_authority_sha256))
            or result
        ),
    )

    assert owner.main(
        (
            "upstream-sync-successor-owner-apply",
            "--revision",
            REVISION,
            "--launch-authority-sha256",
            launch_authority_sha256,
        )
    ) == 0

    assert calls == [
        ("runtime", REVISION),
        ("dispatch", launch_authority_sha256),
    ]
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize(
    "extra",
    (
        (),
        ("--launch-authority-sha256", "not-a-sha256"),
        ("--launch-authority-sha256", "A" * 64),
    ),
    ids=("missing", "malformed", "uppercase"),
)
def test_successor_rebind_owner_action_requires_exact_launch_authority(
    monkeypatch: pytest.MonkeyPatch,
    extra: tuple[str, ...],
) -> None:
    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda _revision: pytest.fail("runtime gate must not run"),
    )
    with pytest.raises(SystemExit):
        owner.main(
            (
                "upstream-sync-successor-owner-apply",
                "--revision",
                REVISION,
                *extra,
            )
        )


def test_successor_rebind_owner_action_rejects_any_extra_argument() -> None:
    with pytest.raises(SystemExit):
        owner.main(
            (
                "upstream-sync-successor-owner-apply",
                "--revision",
                REVISION,
                "--launch-authority-sha256",
                "5" * 64,
                "--path",
                "/tmp/forbidden",
            )
        )
