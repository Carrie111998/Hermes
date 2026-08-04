from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.canary import stopped_writer_residue_recovery as recovery


TARGET_REVISION = "a" * 40
SOURCE_REVISION = "b" * 40
COLLECTOR_SHA256 = "c" * 64


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_host_identity_snapshot() -> dict[str, Any]:
    return {
        "gateway": {
            "name": "muncho-gateway",
            "uid": 993,
            "gid": 992,
            "home": "/var/lib/hermes-gateway",
            "shell": "/usr/sbin/nologin",
            "groups": [990, 992],
        },
        "writer": {
            "name": "muncho-canonical-writer",
            "uid": 999,
            "gid": 994,
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "groups": [991, 994],
        },
        "projector": {
            "name": "muncho-projector",
            "uid": 992,
            "gid": 991,
            "home": "/nonexistent",
            "shell": "/usr/sbin/nologin",
            "groups": [991],
        },
        "groups": {
            "muncho-gateway": {"gid": 992, "members": []},
            "muncho-canonical-writer": {"gid": 994, "members": []},
            "muncho-writer-client": {
                "gid": 990,
                "members": ["muncho-gateway"],
            },
            "muncho-projector": {
                "gid": 991,
                "members": ["muncho-canonical-writer"],
            },
        },
        "effective_gid_members": {
            "990": ["muncho-gateway"],
            "991": ["muncho-canonical-writer", "muncho-projector"],
            "992": ["muncho-gateway"],
            "994": ["muncho-canonical-writer"],
        },
    }


def test_exact_host_validation_imports_without_activation_runtime() -> None:
    repository = Path(__file__).resolve().parents[3]
    script = f"""
import builtins
import sys

sys.path.insert(0, {str(repository)!r})
original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "gateway.canonical_writer_activation" or name.startswith("cryptography"):
        raise ModuleNotFoundError(name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from scripts.canary import stopped_writer_residue_recovery as recovery

assert recovery._host_identities_are_exact({repr(_exact_host_identity_snapshot())})
assert "gateway.canonical_writer_activation" not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd="/",
        env={
            "HOME": "/nonexistent",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def _collector_receipt() -> dict[str, Any]:
    digest = "d" * 64
    unsigned: dict[str, Any] = {
        "schema": "muncho-writer-config-collector-receipt.v1",
        "release_revision": SOURCE_REVISION,
        "release_artifact_sha256": digest,
        "release_manifest_path": (
            f"/opt/muncho-canary-releases/{SOURCE_REVISION}/release-manifest.json"
        ),
        "release_manifest_file_sha256": digest,
        "writer_config_path": "/etc/muncho/writer-activation/staged/writer.json",
        "writer_config_sha256": digest,
        "gateway_config_path": "/etc/muncho/writer-activation/staged/gateway.yaml",
        "gateway_config_sha256": digest,
        "database": {
            "host": "10.91.0.3",
            "tls_server_name": (
                "14-0d81ef63-2cac-4a64-84ad-c4f58c0cfd56.europe-west3.sql.goog"
            ),
            "port": 5432,
            "database": "muncho_canary_brain",
            "user": "muncho_canary_writer_login",
            "ca_path": "/etc/muncho/trust/cloudsql-server-ca.pem",
            "ca_sha256": digest,
        },
        "credential_provenance": {
            "path": "/etc/muncho/credentials/canonical-writer-db-password",
            "device": 1,
            "inode": 2,
            "owner_uid": 999,
            "group_gid": 994,
            "mode": "0400",
            "link_count": 1,
            "modification_time_ns": 3,
            "change_time_ns": 4,
            "content_or_digest_recorded": False,
        },
        "catalog_attestation_sha256": digest,
        "public_routine_count": 1,
        "helper_routine_count": 1,
        "private_schema_identity_sha256": digest,
        "managed_hba_receipt_sha256": digest,
        "server_certificate_sha256": digest,
        "hba_observed_at_unix": 100,
        "hba_expires_at_unix": 400,
        "discord_edge_enabled": False,
        "credential_content_or_digest_recorded": False,
        "collected_at_unix": 200,
    }
    return {**unsigned, "receipt_sha256": recovery._sha256_json(unsigned)}


def _native_plan_mapping(
    *,
    writer_sha256: str,
    gateway_sha256: str,
    writer_unit_sha256: str,
    gateway_unit_sha256: str,
    collector_sha256: str = COLLECTOR_SHA256,
    external_iam_policy_sha256: str = "e" * 64,
) -> dict[str, Any]:
    root = f"/opt/muncho-canary-releases/{SOURCE_REVISION}"
    interpreter = f"{root}/venv/bin/python"
    return {
        "schema": "muncho-writer-native-observation-plan.v2",
        "boot_id_sha256": "b" * 64,
        "host_identity_sha256": "c" * 64,
        "observation_id": "11111111-1111-4111-8111-111111111111",
        "revision": SOURCE_REVISION,
        "artifact_root": root,
        "artifact_sha256": "d" * 64,
        "release_manifest_file_sha256": "d" * 64,
        "config_collector_receipt_sha256": collector_sha256,
        "gateway_unit": {
            "name": "hermes-cloud-gateway.service",
            "path": "/etc/systemd/system/hermes-cloud-gateway.service",
            "sha256": gateway_unit_sha256,
        },
        "writer_unit": {
            "name": "muncho-canonical-writer.service",
            "path": "/etc/systemd/system/muncho-canonical-writer.service",
            "sha256": writer_unit_sha256,
        },
        "gateway_argv": [
            interpreter,
            "-B",
            "-I",
            "-m",
            "gateway.canonical_writer_gateway_bootstrap",
        ],
        "writer_argv": [
            interpreter,
            "-B",
            "-I",
            "-m",
            "gateway.canonical_writer_bootstrap",
            "--config",
            "/etc/muncho-canonical-writer/writer.json",
        ],
        "gateway_config": {
            "path": "/etc/hermes/config.yaml",
            "sha256": gateway_sha256,
        },
        "writer_config": {
            "path": "/etc/muncho-canonical-writer/writer.json",
            "sha256": writer_sha256,
        },
        "identities": {
            "gateway_uid": 993,
            "gateway_gid": 992,
            "gateway_supplementary_gids": [990, 992],
            "writer_uid": 999,
            "writer_gid": 994,
            "writer_supplementary_gids": [991, 994],
            "socket_group_gid": 990,
            "projector_uid": 992,
            "projector_gid": 991,
            "gateway_home": "/var/lib/hermes-gateway",
            "writer_home": "/nonexistent",
            "projector_home": "/nonexistent",
        },
        "database": {
            "ip_network": "10.91.0.3/32",
            "tls_server_name": (
                "14-0d81ef63-2cac-4a64-84ad-c4f58c0cfd56.europe-west3.sql.goog"
            ),
            "ca_path": "/etc/muncho/trust/cloudsql-server-ca.pem",
            "ca_sha256": "d" * 64,
        },
        "discord": {
            "unit_name": "muncho-discord-egress.service",
            "config_path": "/etc/muncho/discord-edge.json",
            "token_path": "/etc/muncho/discord-edge-credentials/bot-token",
            "socket_path": "/run/muncho-discord-egress/edge.sock",
            "required_absent": True,
        },
        "native_discovery_policy": {
            "allowed_roots": ["/usr/lib"],
            "allowed_kernel_executable_mappings": ["[vdso]", "[vsyscall]"],
            "maximum_mappings": 256,
            "required_owner_uid": 0,
            "required_owner_gid": 0,
            "require_regular": True,
            "require_single_link": True,
            "forbid_symlink": True,
            "forbid_acl": True,
            "forbid_xattrs": True,
            "forbid_writable": True,
            "forbid_deleted": True,
            "exclude_artifact_root": True,
            "digest_algorithm": "sha256",
        },
        "legacy_helper_path": (
            "/opt/adventico-ai-platform/canonical-brain/bin/"
            "cloud_sql_synthetic_write_gate.py"
        ),
        "external_iam_policy_sha256": external_iam_policy_sha256,
    }


def _legacy_plan(current: dict[str, Any]) -> dict[str, Any]:
    unsigned = {
        name: item
        for name, item in current.items()
        if name not in {"plan_sha256", "staged_artifacts"}
    }
    unsigned["schema"] = recovery.LEGACY_PLAN_SCHEMA
    unsigned["invariants"] = {
        name: item
        for name, item in current["invariants"].items()
        if name != "staged_artifacts_deleted"
    }
    return {**unsigned, "plan_sha256": recovery._sha256_json(unsigned)}


def test_cli_imports_under_remote_minimal_python() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            "-B",
            "-m",
            recovery.__name__,
            "--help",
        ],
        cwd=Path(__file__).parents[3],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert b"Quarantine one exact stopped writer staging residue" in completed.stdout


def test_lightweight_collector_receipt_loader_preserves_exact_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _collector_receipt()
    raw = recovery._canonical_bytes(value)
    monkeypatch.setattr(recovery, "CONFIG_COLLECTOR_EVIDENCE_ROOT", tmp_path)
    monkeypatch.setattr(
        recovery,
        "_trusted_publication",
        lambda _path, **_kwargs: raw,
    )

    receipt = recovery._load_collector_receipt(
        revision=SOURCE_REVISION,
        receipt_sha256=value["receipt_sha256"],
    )

    assert receipt.value == value
    assert receipt.sha256 == value["receipt_sha256"]

    drifted = json.loads(raw)
    drifted["database"]["unexpected"] = "field"
    drifted_unsigned = {
        name: item for name, item in drifted.items() if name != "receipt_sha256"
    }
    drifted["receipt_sha256"] = recovery._sha256_json(drifted_unsigned)
    monkeypatch.setattr(
        recovery,
        "_trusted_publication",
        lambda _path, **_kwargs: recovery._canonical_bytes(drifted),
    )
    with pytest.raises(ValueError, match="database identity drifted"):
        recovery._load_collector_receipt(
            revision=SOURCE_REVISION,
            receipt_sha256=drifted["receipt_sha256"],
        )


@pytest.fixture()
def recovery_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    activation_root = tmp_path / "writer-activation"
    staging_root = activation_root / "staged"
    staging_root.mkdir(parents=True)
    writer_path = staging_root / "writer.json"
    gateway_path = staging_root / "gateway.yaml"
    native_plan_path = staging_root / "native-observation-plan.json"
    installed_native_plan_path = activation_root / "native-observation-plan.json"
    writer_unit_path = staging_root / "muncho-canonical-writer.service"
    phase_b_unit_path = (
        staging_root / "muncho-canonical-writer-phase-b-readiness.service"
    )
    gateway_unit_path = staging_root / "hermes-cloud-gateway.service"
    owner_approval_path = staging_root / "owner-approval.json"
    external_iam_path = staging_root / "external-iam-receipt.json"
    quarantine_path = tmp_path / "writer-failure" / "quarantine.json"
    native_failure_root = tmp_path / "native-failures"
    native_evidence_root = tmp_path / "native-evidence"
    writer_raw = b'{"writer":"stopped"}'
    gateway_raw = b"gateway: stopped\n"
    writer_path.write_bytes(writer_raw)
    gateway_path.write_bytes(gateway_raw)
    recovery_root = activation_root / "recovered-staging"
    evidence_root = tmp_path / "config-collector"
    collector_path = evidence_root / SOURCE_REVISION / f"{COLLECTOR_SHA256}.json"
    collector_path.parent.mkdir(parents=True)
    collector_path.write_text("{}", encoding="utf-8")
    foreign_path = staging_root / "activation-plan.json"
    unit = "muncho-canonical-writer.service"
    service_states = [
        {
            "unit": unit,
            "state": "absent",
            "properties": {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "SubState": "dead",
                "UnitFileState": "",
                "MainPID": "0",
                "FragmentPath": "",
                "DropInPaths": "",
            },
        }
    ]

    monkeypatch.setattr(recovery, "STAGING_ROOT", staging_root)
    monkeypatch.setattr(recovery, "RECOVERY_ROOT", recovery_root)
    monkeypatch.setattr(
        recovery,
        "DEFAULT_WRITER_CONFIG_SOURCE_PATH",
        writer_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_GATEWAY_CONFIG_SOURCE_PATH",
        gateway_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_STAGED_NATIVE_PLAN_PATH",
        native_plan_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_INSTALLED_NATIVE_PLAN_PATH",
        installed_native_plan_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_STAGED_WRITER_UNIT_PATH",
        writer_unit_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_STAGED_PHASE_B_READINESS_UNIT_PATH",
        phase_b_unit_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_STAGED_GATEWAY_UNIT_PATH",
        gateway_unit_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_STAGED_OWNER_APPROVAL_PATH",
        owner_approval_path,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_STAGED_EXTERNAL_IAM_PATH",
        external_iam_path,
    )
    monkeypatch.setattr(recovery, "DEFAULT_QUARANTINE_PATH", quarantine_path)
    monkeypatch.setattr(
        recovery,
        "DEFAULT_NATIVE_FAILURE_ROOT",
        native_failure_root,
    )
    monkeypatch.setattr(
        recovery,
        "DEFAULT_NATIVE_OBSERVATION_EVIDENCE_ROOT",
        native_evidence_root,
    )
    monkeypatch.setattr(recovery, "CONFIG_COLLECTOR_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        recovery,
        "_ACTIVATION_PATHS",
        (
            writer_path,
            gateway_path,
            native_plan_path,
            owner_approval_path,
            external_iam_path,
            installed_native_plan_path,
            foreign_path,
            writer_unit_path,
            phase_b_unit_path,
            gateway_unit_path,
        ),
    )
    monkeypatch.setattr(recovery, "_STOPPED_SERVICE_UNITS", (unit,))
    monkeypatch.setattr(recovery, "_collect_service_states", lambda: service_states)
    monkeypatch.setattr(recovery, "_trusted_config", lambda path: path.read_bytes())

    def validate_directory(path: Path) -> frozenset[str]:
        pair = frozenset({
            "writer.json",
            "gateway.yaml",
        })
        bundle = pair | frozenset({
            "native-observation-plan.json",
            "muncho-canonical-writer.service",
            "muncho-canonical-writer-phase-b-readiness.service",
            "hermes-cloud-gateway.service",
        })
        failed = bundle | frozenset({
            "owner-approval.json",
            "external-iam-receipt.json",
        })
        if not path.is_dir() or frozenset(os.listdir(path)) not in {
            pair,
            bundle,
            failed,
        }:
            raise RuntimeError("test staging directory is not exact")
        return frozenset(os.listdir(path))

    monkeypatch.setattr(recovery, "_validate_staging_directory", validate_directory)
    collector_value = _collector_receipt()
    collector_value["writer_config_sha256"] = _sha256(writer_raw)
    collector_value["gateway_config_sha256"] = _sha256(gateway_raw)
    receipt = SimpleNamespace(sha256=COLLECTOR_SHA256, value=collector_value)
    monkeypatch.setattr(
        recovery,
        "_matching_collector_receipt",
        lambda **_kwargs: (receipt, collector_path),
    )
    monkeypatch.setattr(
        recovery,
        "_ensure_exact_directory",
        lambda path, **_kwargs: path.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(
        recovery,
        "_publish_bytes_no_replace",
        lambda path, raw, **_kwargs: path.write_bytes(raw),
    )
    monkeypatch.setattr(
        recovery,
        "_trusted_publication",
        lambda path, **_kwargs: path.read_bytes(),
    )
    monkeypatch.setattr(recovery, "_fsync_directory", lambda _path: None)
    monkeypatch.setattr(
        recovery,
        "_require_current_exact_host_identities",
        _exact_host_identity_snapshot,
    )
    return {
        "staging_root": staging_root,
        "writer_path": writer_path,
        "gateway_path": gateway_path,
        "writer_raw": writer_raw,
        "gateway_raw": gateway_raw,
        "native_plan_path": native_plan_path,
        "installed_native_plan_path": installed_native_plan_path,
        "writer_unit_path": writer_unit_path,
        "phase_b_unit_path": phase_b_unit_path,
        "gateway_unit_path": gateway_unit_path,
        "owner_approval_path": owner_approval_path,
        "external_iam_path": external_iam_path,
        "quarantine_path": quarantine_path,
        "native_failure_root": native_failure_root,
        "native_evidence_root": native_evidence_root,
        "recovery_root": recovery_root,
        "foreign_path": foreign_path,
        "service_states": service_states,
    }


def test_plan_binds_exact_receipt_and_fixed_pair(recovery_tree: dict[str, Any]) -> None:
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)

    assert plan["schema"] == recovery.PLAN_SCHEMA
    assert plan["target_release_revision"] == TARGET_REVISION
    assert plan["source_release_revision"] == SOURCE_REVISION
    assert plan["collector_receipt_sha256"] == COLLECTOR_SHA256
    assert plan["writer_config_sha256"] == _sha256(recovery_tree["writer_raw"])
    assert plan["gateway_config_sha256"] == _sha256(recovery_tree["gateway_raw"])
    assert plan["staged_artifacts"] == {
        "gateway.yaml": _sha256(recovery_tree["gateway_raw"]),
        "writer.json": _sha256(recovery_tree["writer_raw"]),
    }
    assert plan["invariants"]["staged_configs_deleted"] is False
    assert plan["invariants"]["staged_artifacts_deleted"] is False
    assert (
        recovery.validate_plan_mapping(
            plan,
            expected_target_revision=TARGET_REVISION,
        )
        == plan
    )


def test_legacy_pair_plan_and_receipt_remain_valid_after_upgrade(
    recovery_tree: dict[str, Any],
) -> None:
    current = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    legacy = _legacy_plan(current)

    assert recovery.validate_plan_mapping(legacy) == legacy

    receipt_unsigned = recovery._receipt_unsigned(
        legacy,
        service_states_after=recovery_tree["service_states"],
        created_at_unix=321,
    )
    receipt = {
        **receipt_unsigned,
        "receipt_sha256": recovery._sha256_json(receipt_unsigned),
    }
    assert receipt["schema"] == recovery.LEGACY_RECEIPT_SCHEMA
    assert "staged_artifacts" not in receipt
    assert recovery.validate_receipt_mapping(receipt, plan=legacy) == receipt


def test_legacy_persisted_intent_resumes_after_rename_and_stays_idempotent(
    recovery_tree: dict[str, Any],
) -> None:
    legacy = _legacy_plan(
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    )
    recovery_tree["recovery_root"].mkdir()
    recovery._write_intent(legacy)
    os.rename(recovery_tree["staging_root"], Path(legacy["archive_path"]))

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        legacy["plan_sha256"],
        clock=lambda: 654,
        lifecycle_lock=contextlib.nullcontext,
    )
    repeated = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        legacy["plan_sha256"],
        clock=lambda: 999,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert receipt["schema"] == recovery.LEGACY_RECEIPT_SCHEMA
    assert repeated == receipt


def test_apply_atomically_quarantines_and_is_idempotent(
    recovery_tree: dict[str, Any],
) -> None:
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 123,
        lifecycle_lock=contextlib.nullcontext,
    )

    archive = Path(plan["archive_path"])
    assert not recovery_tree["staging_root"].exists()
    assert (archive / "writer.json").read_bytes() == recovery_tree["writer_raw"]
    assert (archive / "gateway.yaml").read_bytes() == recovery_tree["gateway_raw"]
    assert receipt["state"] == "staging_residue_quarantined_services_stopped"
    assert receipt["source_activation_paths_absent"] is True
    assert receipt["staged_configs_deleted"] is False
    assert receipt["staged_artifacts_deleted"] is False

    repeated = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 999,
        lifecycle_lock=contextlib.nullcontext,
    )
    assert repeated == receipt


def test_apply_resumes_after_atomic_rename_before_terminal_receipt(
    recovery_tree: dict[str, Any],
) -> None:
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    recovery_tree["recovery_root"].mkdir()
    recovery._write_intent(plan)
    os.rename(recovery_tree["staging_root"], Path(plan["archive_path"]))

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 456,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert receipt["created_at_unix"] == 456
    assert Path(plan["receipt_path"]).is_file()


def test_apply_quarantines_complete_preflight_bundle_without_deleting_artifacts(
    recovery_tree: dict[str, Any],
) -> None:
    writer_unit = b"[Service]\nExecStart=/writer\n"
    gateway_unit = b"[Service]\nExecStart=/gateway\n"
    native_plan = _native_plan_mapping(
        writer_sha256=_sha256(recovery_tree["writer_raw"]),
        gateway_sha256=_sha256(recovery_tree["gateway_raw"]),
        writer_unit_sha256=_sha256(writer_unit),
        gateway_unit_sha256=_sha256(gateway_unit),
    )
    phase_b_unit = recovery.render_phase_b_readiness_service(
        revision=SOURCE_REVISION,
        artifact_root=f"/opt/muncho-canary-releases/{SOURCE_REVISION}",
        artifact_sha256="d" * 64,
    ).encode()
    extras = {
        recovery_tree["native_plan_path"]: recovery._canonical_bytes(native_plan),
        recovery_tree["writer_unit_path"]: writer_unit,
        recovery_tree["phase_b_unit_path"]: phase_b_unit,
        recovery_tree["gateway_unit_path"]: gateway_unit,
    }
    for path, raw in extras.items():
        path.write_bytes(raw)

    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)

    assert set(plan["staged_artifacts"]) == {
        "writer.json",
        "gateway.yaml",
        "native-observation-plan.json",
        "muncho-canonical-writer.service",
        "muncho-canonical-writer-phase-b-readiness.service",
        "hermes-cloud-gateway.service",
    }
    for path, raw in extras.items():
        assert plan["staged_artifacts"][path.name] == _sha256(raw)

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 789,
        lifecycle_lock=contextlib.nullcontext,
    )

    archive = Path(plan["archive_path"])
    assert not recovery_tree["staging_root"].exists()
    for path, raw in extras.items():
        assert (archive / path.name).read_bytes() == raw
    assert receipt["staged_artifacts"] == plan["staged_artifacts"]
    assert receipt["staged_artifacts_deleted"] is False


def _write_complete_bundle_with_installed_native(
    recovery_tree: dict[str, Any],
) -> tuple[dict[Path, bytes], bytes]:
    writer_unit = b"[Service]\nExecStart=/writer\n"
    gateway_unit = b"[Service]\nExecStart=/gateway\n"
    native_plan = _native_plan_mapping(
        writer_sha256=_sha256(recovery_tree["writer_raw"]),
        gateway_sha256=_sha256(recovery_tree["gateway_raw"]),
        writer_unit_sha256=_sha256(writer_unit),
        gateway_unit_sha256=_sha256(gateway_unit),
    )
    native_raw = recovery._canonical_bytes(native_plan)
    phase_b_unit = recovery.render_phase_b_readiness_service(
        revision=SOURCE_REVISION,
        artifact_root=f"/opt/muncho-canary-releases/{SOURCE_REVISION}",
        artifact_sha256="d" * 64,
    ).encode()
    extras = {
        recovery_tree["native_plan_path"]: native_raw,
        recovery_tree["writer_unit_path"]: writer_unit,
        recovery_tree["phase_b_unit_path"]: phase_b_unit,
        recovery_tree["gateway_unit_path"]: gateway_unit,
    }
    for path, raw in extras.items():
        path.write_bytes(raw)
    recovery_tree["installed_native_plan_path"].write_bytes(native_raw)
    return extras, native_raw


def _external_iam_mapping(*, source_approval_sha256: str) -> dict[str, Any]:
    return {
        "schema": "muncho-writer-external-iam-evidence.v1",
        "project": "adventico-ai-platform",
        "zone": "europe-west3-a",
        "instance": "muncho-canary-v2-01",
        "service_account": (
            "muncho-canary-v2-runtime@adventico-ai-platform.iam.gserviceaccount.com"
        ),
        "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
        "roles": [
            "roles/logging.logWriter",
            "roles/monitoring.metricWriter",
            ("projects/adventico-ai-platform/roles/munchoCanaryCloudSqlReadinessV1"),
        ],
        "permissions": [
            "cloudsql.instances.get",
            "logging.logEntries.create",
            "logging.logEntries.route",
            "monitoring.metricDescriptors.create",
            "monitoring.metricDescriptors.get",
            "monitoring.metricDescriptors.list",
            "monitoring.monitoredResourceDescriptors.get",
            "monitoring.monitoredResourceDescriptors.list",
            "monitoring.timeSeries.create",
        ],
        "foundation_plan_sha256": "1" * 64,
        "host_plan_sha256": "2" * 64,
        "foundation_report_sha256": "3" * 64,
        "host_report_sha256": "4" * 64,
        "source_approval_sha256": source_approval_sha256,
        "collected_at_unix": 100,
        "expires_at_unix": 1300,
    }


def _write_failed_native_bundle(
    recovery_tree: dict[str, Any],
    *,
    host_identity_convergence_failure: bool = False,
    install_failure: bool = False,
) -> tuple[dict[Path, bytes], bytes, bytes]:
    if host_identity_convergence_failure and install_failure:
        raise ValueError("test failure shape must be exact")
    writer_unit = b"[Service]\nExecStart=/writer\n"
    gateway_unit = b"[Service]\nExecStart=/gateway\n"
    policy_receipt = recovery.ExternalIAMReceipt.from_mapping(
        _external_iam_mapping(source_approval_sha256="0" * 64)
    )
    native_mapping = _native_plan_mapping(
        writer_sha256=_sha256(recovery_tree["writer_raw"]),
        gateway_sha256=_sha256(recovery_tree["gateway_raw"]),
        writer_unit_sha256=_sha256(writer_unit),
        gateway_unit_sha256=_sha256(gateway_unit),
        external_iam_policy_sha256=policy_receipt.policy_sha256,
    )
    native = recovery.NativeObservationPlan.from_mapping(native_mapping)
    native_raw = recovery._canonical_bytes(native.to_mapping())
    owner = recovery.OwnerApprovalReceipt.from_mapping({
        "schema": "muncho-writer-owner-approval.v1",
        "scope": "native_observation",
        "plan_sha256": native.sha256,
        "authority_kind": "trusted_root_bootstrap_out_of_band_owner",
        "cryptographic_owner_proof": False,
        "owner_subject_sha256": "5" * 64,
        "approval_source_sha256": "6" * 64,
        "nonce_sha256": "7" * 64,
        "approved_at_unix": 100,
        "expires_at_unix": 400,
    })
    iam = recovery.ExternalIAMReceipt.from_mapping(
        _external_iam_mapping(source_approval_sha256=owner.sha256)
    )
    owner_raw = recovery._canonical_bytes(owner.to_mapping())
    iam_raw = recovery._canonical_bytes(iam.to_mapping())
    phase_b_unit = recovery.render_phase_b_readiness_service(
        revision=SOURCE_REVISION,
        artifact_root=f"/opt/muncho-canary-releases/{SOURCE_REVISION}",
        artifact_sha256="d" * 64,
    ).encode()
    extras = {
        recovery_tree["native_plan_path"]: native_raw,
        recovery_tree["writer_unit_path"]: writer_unit,
        recovery_tree["phase_b_unit_path"]: phase_b_unit,
        recovery_tree["gateway_unit_path"]: gateway_unit,
        recovery_tree["owner_approval_path"]: owner_raw,
        recovery_tree["external_iam_path"]: iam_raw,
    }
    for path, raw in extras.items():
        path.write_bytes(raw)
    recovery_tree["installed_native_plan_path"].write_bytes(native_raw)
    failure_path = (
        recovery_tree["native_failure_root"]
        / SOURCE_REVISION
        / native.sha256
        / "failures"
        / "failure-123-456.json"
    )
    failure_path.parent.mkdir(parents=True)
    failure: dict[str, Any] = {
        "schema": "muncho-writer-only-activation-failure.v1",
        "revision": SOURCE_REVISION,
        "native_observation_plan_sha256": native.sha256,
        "owner_approval_receipt_sha256": owner.sha256,
        "owner_approval_receipt": owner.to_mapping(),
        "external_iam_evidence": {},
        "stage": "read_only_preflight",
        "error_type": "ValueError",
        "error_sha256": "8" * 64,
        "failed_at_unix": 200,
        "quarantined": True,
        "failure_receipt_path": str(failure_path),
        "host_preparation_sha256": recovery._sha256_json({}),
        "host_preparation_evidence": {},
        "stage_preserved": False,
    }
    if host_identity_convergence_failure:
        evidence_root = (
            recovery_tree["native_evidence_root"] / SOURCE_REVISION / native.sha256
        )
        archived_iam_path = evidence_root / "external-iam" / f"{iam.sha256}.json"
        archived_iam_path.parent.mkdir(parents=True)
        archived_iam_path.write_bytes(iam_raw)
        exact_after = _exact_host_identity_snapshot()
        host_path = (
            evidence_root
            / "host-preparation-failures"
            / "failure-124-457.json"
        )
        host_path.parent.mkdir(parents=True)
        host_unsigned = {
            "schema": "muncho-writer-host-preparation-failure.v1",
            "revision": SOURCE_REVISION,
            "native_observation_plan_sha256": native.sha256,
            "owner_approval_receipt_sha256": owner.sha256,
            "changed": True,
            "before": {"state": "pre-reconciliation"},
            "after": exact_after,
            "error_type": recovery._HOST_IDENTITY_CONVERGENCE_ERROR_TYPE,
            "error_sha256": recovery._HOST_IDENTITY_CONVERGENCE_ERROR_SHA256,
            "failed_at_unix": 200,
            "receipt_path": str(host_path),
        }
        host = {
            **host_unsigned,
            "receipt_sha256": recovery._sha256_json(host_unsigned),
        }
        activation_host_state = {
            "changed": host["changed"],
            "before": host["before"],
            "after": host["after"],
            "failed": True,
        }
        host_path.write_bytes(recovery._canonical_bytes(host))
        failure.update({
            "external_iam_evidence": {
                "path": str(archived_iam_path),
                "sha256": iam.sha256,
                "policy_sha256": iam.policy_sha256,
                "mode": "0400",
                "owner_uid": 0,
                "group_gid": 0,
                "live_path": str(recovery.DEFAULT_EXTERNAL_IAM_LIVE_PATH),
            },
            "stage": "prepare_host_identities",
            "error_type": recovery._HOST_IDENTITY_CONVERGENCE_ERROR_TYPE,
            "error_sha256": recovery._HOST_IDENTITY_CONVERGENCE_ERROR_SHA256,
            "failed_at_unix": 201,
            "host_preparation_sha256": recovery._sha256_json(
                activation_host_state
            ),
            "host_preparation_evidence": host,
        })
    if install_failure:
        evidence_root = (
            recovery_tree["native_evidence_root"] / SOURCE_REVISION / native.sha256
        )
        archived_iam_path = evidence_root / "external-iam" / f"{iam.sha256}.json"
        archived_iam_path.parent.mkdir(parents=True)
        archived_iam_path.write_bytes(iam_raw)
        exact_state = _exact_host_identity_snapshot()
        host_path = evidence_root / "host-preparation.json"
        host_unsigned = {
            "schema": "muncho-writer-host-preparation.v1",
            "revision": SOURCE_REVISION,
            "native_observation_plan_sha256": native.sha256,
            "owner_approval_receipt_sha256": owner.sha256,
            "changed": False,
            "before": exact_state,
            "after": exact_state,
            "prepared_at_unix": 200,
            "receipt_path": str(host_path),
        }
        host = {
            **host_unsigned,
            "receipt_sha256": recovery._sha256_json(host_unsigned),
        }
        host_path.write_bytes(recovery._canonical_bytes(host))
        activation_host_state = {
            "changed": host["changed"],
            "before": host["before"],
            "after": host["after"],
        }
        failure.update({
            "external_iam_evidence": {
                "path": str(archived_iam_path),
                "sha256": iam.sha256,
                "policy_sha256": iam.policy_sha256,
                "mode": "0400",
                "owner_uid": 0,
                "group_gid": 0,
                "live_path": str(recovery.DEFAULT_EXTERNAL_IAM_LIVE_PATH),
            },
            "stage": "install",
            "error_type": "ValueError",
            "error_sha256": recovery.hashlib.sha256(
                b"ValueError:activation parent path is unavailable"
            ).hexdigest(),
            "failed_at_unix": 201,
            "host_preparation_sha256": recovery._sha256_json(
                activation_host_state
            ),
            "host_preparation_evidence": host,
        })
    failure_raw = recovery._canonical_bytes(failure)
    failure_path.write_bytes(failure_raw)
    recovery_tree["quarantine_path"].parent.mkdir(parents=True)
    recovery_tree["quarantine_path"].write_bytes(failure_raw)
    return extras, native_raw, failure_raw


def test_apply_archives_identical_installed_native_plan_crash_safely(
    recovery_tree: dict[str, Any],
) -> None:
    extras, native_raw = _write_complete_bundle_with_installed_native(recovery_tree)

    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)

    assert plan["schema"] == recovery.INSTALLED_NATIVE_PLAN_SCHEMA
    installed = plan["installed_native_observation_plan"]
    assert installed == {
        "source_path": str(recovery_tree["installed_native_plan_path"]),
        "sha256": _sha256(native_raw),
        "archive_path": str(
            recovery._installed_native_archive_path(
                TARGET_REVISION,
                SOURCE_REVISION,
                COLLECTOR_SHA256,
            )
        ),
    }

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 890,
        lifecycle_lock=contextlib.nullcontext,
    )

    archive = Path(plan["archive_path"])
    installed_archive = Path(installed["archive_path"])
    assert not recovery_tree["staging_root"].exists()
    assert not recovery_tree["installed_native_plan_path"].exists()
    assert installed_archive.read_bytes() == native_raw
    for path, raw in extras.items():
        assert (archive / path.name).read_bytes() == raw
    assert receipt["schema"] == recovery.INSTALLED_NATIVE_RECEIPT_SCHEMA
    assert receipt["installed_native_observation_plan"] == installed
    assert receipt["installed_native_observation_plan_archived"] is True
    assert receipt["installed_native_observation_plan_deleted"] is False

    repeated = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 999,
        lifecycle_lock=contextlib.nullcontext,
    )
    assert repeated == receipt


def test_apply_archives_exact_read_only_preflight_failure_chain_last(
    recovery_tree: dict[str, Any],
) -> None:
    extras, native_raw, failure_raw = _write_failed_native_bundle(recovery_tree)

    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)

    assert plan["schema"] == recovery.FAILED_NATIVE_PLAN_SCHEMA
    assert set(plan["staged_artifacts"]) == {
        "writer.json",
        "gateway.yaml",
        "native-observation-plan.json",
        "muncho-canonical-writer.service",
        "muncho-canonical-writer-phase-b-readiness.service",
        "hermes-cloud-gateway.service",
        "owner-approval.json",
        "external-iam-receipt.json",
    }
    failed = plan["failed_native_observation"]
    assert failed["source_path"] == str(recovery_tree["quarantine_path"])
    assert failed["sha256"] == _sha256(failure_raw)
    assert failed["failure_receipt_sha256"] == _sha256(failure_raw)

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 891,
        lifecycle_lock=contextlib.nullcontext,
    )

    archive = Path(plan["archive_path"])
    installed_archive = Path(plan["installed_native_observation_plan"]["archive_path"])
    failure_archive = Path(failed["archive_path"])
    assert not recovery_tree["staging_root"].exists()
    assert not recovery_tree["installed_native_plan_path"].exists()
    assert not recovery_tree["quarantine_path"].exists()
    assert installed_archive.read_bytes() == native_raw
    assert failure_archive.read_bytes() == failure_raw
    assert Path(failed["failure_receipt_path"]).read_bytes() == failure_raw
    for path, raw in extras.items():
        assert (archive / path.name).read_bytes() == raw
    assert receipt["schema"] == recovery.FAILED_NATIVE_RECEIPT_SCHEMA
    assert receipt["failure_quarantine_archived"] is True
    assert receipt["failure_quarantine_deleted"] is False
    assert receipt["failure_receipt_preserved"] is True

    repeated = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 999,
        lifecycle_lock=contextlib.nullcontext,
    )
    assert repeated == receipt


def test_apply_archives_exact_host_identity_convergence_failure_chain(
    recovery_tree: dict[str, Any],
) -> None:
    _extras, _native_raw, failure_raw = _write_failed_native_bundle(
        recovery_tree,
        host_identity_convergence_failure=True,
    )

    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 893,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert plan["schema"] == recovery.FAILED_NATIVE_PLAN_SCHEMA
    assert plan["failed_native_observation"]["sha256"] == _sha256(failure_raw)
    assert receipt["schema"] == recovery.FAILED_NATIVE_RECEIPT_SCHEMA
    assert receipt["failure_receipt_preserved"] is True


def test_apply_archives_exact_native_install_failure_chain(
    recovery_tree: dict[str, Any],
) -> None:
    _extras, _native_raw, failure_raw = _write_failed_native_bundle(
        recovery_tree,
        install_failure=True,
    )

    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 894,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert plan["schema"] == recovery.FAILED_NATIVE_PLAN_SCHEMA
    assert plan["failed_native_observation"]["sha256"] == _sha256(failure_raw)
    assert receipt["schema"] == recovery.FAILED_NATIVE_RECEIPT_SCHEMA
    assert receipt["failure_receipt_preserved"] is True


def test_install_failure_recovery_rejects_host_state_digest_drift(
    recovery_tree: dict[str, Any],
) -> None:
    _write_failed_native_bundle(recovery_tree, install_failure=True)
    failure = json.loads(recovery_tree["quarantine_path"].read_text())
    failure["host_preparation_sha256"] = "f" * 64
    failure_raw = recovery._canonical_bytes(failure)
    Path(failure["failure_receipt_path"]).write_bytes(failure_raw)
    recovery_tree["quarantine_path"].write_bytes(failure_raw)

    with pytest.raises(ValueError, match="host preparation binding is invalid"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_host_identity_failure_recovery_rejects_nonexact_current_state(
    recovery_tree: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_failed_native_bundle(
        recovery_tree,
        host_identity_convergence_failure=True,
    )
    monkeypatch.setattr(
        recovery,
        "_require_current_exact_host_identities",
        lambda: (_ for _ in ()).throw(
            RuntimeError("current canary host identities are not exact")
        ),
    )

    with pytest.raises(RuntimeError, match="current canary host identities"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("error_sha256", "f" * 64),
        ("error_type", "ValueError"),
    ),
)
def test_host_identity_failure_recovery_rejects_outer_error_drift(
    recovery_tree: dict[str, Any],
    field: str,
    value: str,
) -> None:
    _write_failed_native_bundle(
        recovery_tree,
        host_identity_convergence_failure=True,
    )
    failure = json.loads(recovery_tree["quarantine_path"].read_text())
    failure[field] = value
    drifted = recovery._canonical_bytes(failure)
    Path(failure["failure_receipt_path"]).write_bytes(drifted)
    recovery_tree["quarantine_path"].write_bytes(drifted)

    with pytest.raises(ValueError, match="convergence failure is invalid"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_host_identity_failure_recovery_rejects_embedded_after_drift(
    recovery_tree: dict[str, Any],
) -> None:
    _write_failed_native_bundle(
        recovery_tree,
        host_identity_convergence_failure=True,
    )
    failure = json.loads(recovery_tree["quarantine_path"].read_text())
    host = failure["host_preparation_evidence"]
    host["after"]["effective_gid_members"]["991"] = ["muncho-projector"]
    host_unsigned = {
        name: item for name, item in host.items() if name != "receipt_sha256"
    }
    host["receipt_sha256"] = recovery._sha256_json(host_unsigned)
    failure["host_preparation_sha256"] = recovery._sha256_json(host)
    host_raw = recovery._canonical_bytes(host)
    Path(host["receipt_path"]).write_bytes(host_raw)
    failure_raw = recovery._canonical_bytes(failure)
    Path(failure["failure_receipt_path"]).write_bytes(failure_raw)
    recovery_tree["quarantine_path"].write_bytes(failure_raw)

    with pytest.raises(ValueError, match="convergence failure is invalid"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_failed_native_plan_cannot_be_downcast_to_installed_native_schema(
    recovery_tree: dict[str, Any],
) -> None:
    _write_failed_native_bundle(recovery_tree)
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    downcast = dict(plan)
    downcast["schema"] = recovery.INSTALLED_NATIVE_PLAN_SCHEMA
    del downcast["failed_native_observation"]
    downcast["invariants"] = {
        name: value
        for name, value in downcast["invariants"].items()
        if name
        not in {
            "failure_quarantine_archived",
            "failure_quarantine_deleted",
            "failure_receipt_preserved",
        }
    }
    unsigned = {
        name: value for name, value in downcast.items() if name != "plan_sha256"
    }
    downcast["plan_sha256"] = recovery._sha256_json(unsigned)

    with pytest.raises(
        ValueError,
        match="plan schema artifact set is invalid",
    ):
        recovery.validate_plan_mapping(downcast)


def test_failed_native_recovery_rejects_unbound_quarantine(
    recovery_tree: dict[str, Any],
) -> None:
    _write_failed_native_bundle(recovery_tree)
    value = json.loads(recovery_tree["quarantine_path"].read_text())
    value["native_observation_plan_sha256"] = "f" * 64
    recovery_tree["quarantine_path"].write_bytes(recovery._canonical_bytes(value))

    with pytest.raises(ValueError, match="quarantine binding is invalid"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_failed_native_recovery_resumes_with_quarantine_still_blocking(
    recovery_tree: dict[str, Any],
) -> None:
    _write_failed_native_bundle(recovery_tree)
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    recovery_tree["recovery_root"].mkdir()
    recovery._write_intent(plan)
    installed = plan["installed_native_observation_plan"]
    os.rename(
        recovery_tree["installed_native_plan_path"],
        Path(installed["archive_path"]),
    )
    os.rename(recovery_tree["staging_root"], Path(plan["archive_path"]))

    assert recovery_tree["quarantine_path"].is_file()
    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 892,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert receipt["created_at_unix"] == 892
    assert not recovery_tree["quarantine_path"].exists()
    assert Path(plan["failed_native_observation"]["archive_path"]).is_file()


def test_failed_native_recovery_rejects_quarantine_moved_before_residue(
    recovery_tree: dict[str, Any],
) -> None:
    _write_failed_native_bundle(recovery_tree)
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    recovery_tree["recovery_root"].mkdir()
    recovery._write_intent(plan)
    os.rename(
        recovery_tree["quarantine_path"],
        Path(plan["failed_native_observation"]["archive_path"]),
    )

    with pytest.raises(
        RuntimeError,
        match="quarantine moved before residue",
    ):
        recovery.apply_stopped_writer_residue_recovery(
            TARGET_REVISION,
            plan["plan_sha256"],
            lifecycle_lock=contextlib.nullcontext,
        )


def test_apply_resumes_after_installed_native_plan_rename(
    recovery_tree: dict[str, Any],
) -> None:
    _extras, native_raw = _write_complete_bundle_with_installed_native(recovery_tree)
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    recovery_tree["recovery_root"].mkdir()
    recovery._write_intent(plan)
    installed = plan["installed_native_observation_plan"]
    os.rename(
        recovery_tree["installed_native_plan_path"],
        Path(installed["archive_path"]),
    )

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 901,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert receipt["created_at_unix"] == 901
    assert Path(installed["archive_path"]).read_bytes() == native_raw
    assert Path(plan["archive_path"]).is_dir()


def test_apply_resumes_after_staging_rename_with_installed_plan_still_live(
    recovery_tree: dict[str, Any],
) -> None:
    _write_complete_bundle_with_installed_native(recovery_tree)
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    recovery_tree["recovery_root"].mkdir()
    recovery._write_intent(plan)
    os.rename(recovery_tree["staging_root"], Path(plan["archive_path"]))

    receipt = recovery.apply_stopped_writer_residue_recovery(
        TARGET_REVISION,
        plan["plan_sha256"],
        clock=lambda: 902,
        lifecycle_lock=contextlib.nullcontext,
    )

    assert receipt["created_at_unix"] == 902
    assert not recovery_tree["installed_native_plan_path"].exists()
    assert Path(plan["installed_native_observation_plan"]["archive_path"]).is_file()


def test_plan_rejects_installed_native_plan_that_differs_from_staging(
    recovery_tree: dict[str, Any],
) -> None:
    _write_complete_bundle_with_installed_native(recovery_tree)
    recovery_tree["installed_native_plan_path"].write_bytes(b"{}")

    with pytest.raises(ValueError, match="native observation plan is invalid"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_plan_rejects_unbound_complete_preflight_bundle(
    recovery_tree: dict[str, Any],
) -> None:
    writer_unit = b"[Service]\nExecStart=/writer\n"
    gateway_unit = b"[Service]\nExecStart=/gateway\n"
    native_plan = _native_plan_mapping(
        writer_sha256=_sha256(recovery_tree["writer_raw"]),
        gateway_sha256=_sha256(recovery_tree["gateway_raw"]),
        writer_unit_sha256=_sha256(writer_unit),
        gateway_unit_sha256=_sha256(gateway_unit),
        collector_sha256="f" * 64,
    )
    extras = {
        recovery_tree["native_plan_path"]: recovery._canonical_bytes(native_plan),
        recovery_tree["writer_unit_path"]: writer_unit,
        recovery_tree["phase_b_unit_path"]: recovery.render_phase_b_readiness_service(
            revision=SOURCE_REVISION,
            artifact_root=f"/opt/muncho-canary-releases/{SOURCE_REVISION}",
            artifact_sha256="d" * 64,
        ).encode(),
        recovery_tree["gateway_unit_path"]: gateway_unit,
    }
    for path, raw in extras.items():
        path.write_bytes(raw)

    with pytest.raises(
        ValueError,
        match="config_collector_receipt_sha256 binding drifted",
    ):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_plan_rejects_partial_or_foreign_activation_residue(
    recovery_tree: dict[str, Any],
) -> None:
    recovery_tree["gateway_path"].unlink()
    with pytest.raises(RuntimeError, match="directory is not exact"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)

    recovery_tree["gateway_path"].write_bytes(recovery_tree["gateway_raw"])
    recovery_tree["foreign_path"].write_bytes(b"foreign")
    with pytest.raises(RuntimeError, match="directory is not exact"):
        recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)


def test_plan_digest_rejects_any_path_drift(recovery_tree: dict[str, Any]) -> None:
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    drifted = dict(plan)
    drifted["archive_path"] = str(Path(plan["archive_path"]).with_name("other"))

    with pytest.raises(ValueError, match="fixed path drifted"):
        recovery.validate_plan_mapping(drifted)


def test_v2_plan_rejects_non_string_artifact_digest(
    recovery_tree: dict[str, Any],
) -> None:
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    drifted = dict(plan)
    drifted["staged_artifacts"] = dict(plan["staged_artifacts"])
    drifted["staged_artifacts"]["writer.json"] = int("1" * 64)

    with pytest.raises(ValueError, match="staged writer.json digest is invalid"):
        recovery.validate_plan_mapping(drifted)


def test_receipt_missing_digest_fails_as_validation_error(
    recovery_tree: dict[str, Any],
) -> None:
    plan = recovery.plan_stopped_writer_residue_recovery(TARGET_REVISION)
    unsigned = recovery._receipt_unsigned(
        plan,
        service_states_after=recovery_tree["service_states"],
        created_at_unix=741,
    )

    with pytest.raises(ValueError, match="recovery receipt digest is invalid"):
        recovery.validate_receipt_mapping(unsigned, plan=plan)
