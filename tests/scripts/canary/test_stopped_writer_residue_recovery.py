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
        "external_iam_policy_sha256": "e" * 64,
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
    writer_unit_path = staging_root / "muncho-canonical-writer.service"
    phase_b_unit_path = (
        staging_root / "muncho-canonical-writer-phase-b-readiness.service"
    )
    gateway_unit_path = staging_root / "hermes-cloud-gateway.service"
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
    monkeypatch.setattr(recovery, "CONFIG_COLLECTOR_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        recovery,
        "_ACTIVATION_PATHS",
        (
            writer_path,
            gateway_path,
            native_plan_path,
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
        if not path.is_dir() or frozenset(os.listdir(path)) not in {pair, bundle}:
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
    return {
        "staging_root": staging_root,
        "writer_path": writer_path,
        "gateway_path": gateway_path,
        "writer_raw": writer_raw,
        "gateway_raw": gateway_raw,
        "native_plan_path": native_plan_path,
        "writer_unit_path": writer_unit_path,
        "phase_b_unit_path": phase_b_unit_path,
        "gateway_unit_path": gateway_unit_path,
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
