from __future__ import annotations

import contextlib
import hashlib
import os
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
