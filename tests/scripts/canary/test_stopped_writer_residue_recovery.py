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
    monkeypatch.setattr(recovery, "CONFIG_COLLECTOR_EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(
        recovery,
        "_ACTIVATION_PATHS",
        (writer_path, gateway_path, foreign_path),
    )
    monkeypatch.setattr(recovery, "_STOPPED_SERVICE_UNITS", (unit,))
    monkeypatch.setattr(recovery, "_collect_service_states", lambda: service_states)
    monkeypatch.setattr(recovery, "_trusted_config", lambda path: path.read_bytes())

    def validate_directory(path: Path) -> None:
        if not path.is_dir() or frozenset(os.listdir(path)) != {
            "writer.json",
            "gateway.yaml",
        }:
            raise RuntimeError("test staging directory is not exact")

    monkeypatch.setattr(recovery, "_validate_staging_directory", validate_directory)
    receipt = SimpleNamespace(
        sha256=COLLECTOR_SHA256,
        value={"release_revision": SOURCE_REVISION},
    )
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
    assert plan["invariants"]["staged_configs_deleted"] is False
    assert (
        recovery.validate_plan_mapping(
            plan,
            expected_target_revision=TARGET_REVISION,
        )
        == plan
    )


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
