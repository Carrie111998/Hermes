from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Mapping

import pytest

from gateway import canonical_writer_production_cutover as cutover
from scripts.canary import production_cutover_host_staging_rotation as rotation


PREDECESSOR = "1" * 40
SUCCESSOR = "2" * 40


def _physical(root: Path, logical: Path) -> Path:
    return root.joinpath(*logical.parts[1:])


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_exact(path: Path, payload: bytes, mode: int = 0o400) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)


def _stage_set(
    root: Path,
    revision: str,
    *,
    names: tuple[str, ...],
) -> Mapping[str, Any]:
    host = _physical(root, rotation.HOST_STAGING_ROOT)
    _private_directory(host)
    rows: dict[str, Mapping[str, Any]] = {}
    for index, name in enumerate(names):
        filename = f"artifact-{index}.json"
        payload = rotation._canonical({"name": name, "revision": revision})
        path = host / filename
        _write_exact(path, payload)
        rows[name] = {
            "sha256": rotation._sha(payload),
            "size": len(payload),
            "staged_gid": os.getegid(),
            "staged_mode": 0o400,
            "staged_path": str(rotation.HOST_STAGING_ROOT / filename),
            "staged_uid": os.geteuid(),
            "target_path": f"/etc/muncho/{filename}",
        }
    unsigned = {
        "schema": "muncho-production-cutover-fixed-host-staging.v1",
        "release_revision": revision,
        "staged_file_count": len(rows),
        "staged_files": rows,
        "staged_set_sha256": rotation._sha(
            rotation._canonical({"files": rows})
        ),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    receipt = {
        **unsigned,
        "receipt_sha256": rotation._sha(rotation._canonical(unsigned)),
    }
    _write_exact(
        _physical(root, rotation.STAGING_RECEIPT_PATH),
        rotation._canonical(receipt),
    )
    return receipt


def _fixture_root(tmp_path: Path) -> Path:
    evidence = _physical(tmp_path, cutover.EVIDENCE_ROOT)
    _private_directory(evidence)
    staged = _physical(tmp_path, rotation.STAGING_RECEIPT_PATH.parent)
    _private_directory(staged)
    return tmp_path


@contextlib.contextmanager
def _lock():
    yield


def test_rotation_preserves_predecessor_and_publishes_successor(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    predecessor = _stage_set(root, PREDECESSOR, names=("one", "two"))

    def stage(revision: str) -> Mapping[str, Any]:
        assert revision == SUCCESSOR
        return _stage_set(root, revision, names=("one", "two", "three"))

    receipt = rotation.rotate_host_staging(
        SUCCESSOR,
        filesystem_root=root,
        require_root=False,
        lock_factory=_lock,
        stage_successor=stage,
    )

    assert receipt["schema"] == rotation.RECEIPT_SCHEMA
    assert receipt["predecessor_staging_receipt_sha256"] == predecessor[
        "receipt_sha256"
    ]
    assert receipt["successor_release_revision"] == SUCCESSOR
    assert receipt["predecessor_preserved"] is True
    assert receipt["production_service_mutation_performed"] is False

    transaction = rotation._transaction_paths(receipt["rotation_id"])
    assert _physical(root, transaction["predecessor_host"]).is_dir()
    assert _physical(root, transaction["predecessor_receipt"]).is_file()
    assert _physical(root, transaction["receipt"]).is_file()
    live = rotation._decode(
        _physical(root, rotation.STAGING_RECEIPT_PATH).read_bytes()
    )
    assert live["release_revision"] == SUCCESSOR
    assert live["staged_file_count"] == 3

    repeated = rotation.rotate_host_staging(
        SUCCESSOR,
        filesystem_root=root,
        require_root=False,
        lock_factory=_lock,
        stage_successor=lambda _revision: pytest.fail("must not restage"),
    )
    assert repeated["receipt_sha256"] == receipt["receipt_sha256"]
    assert repeated["predecessor_preserved"] is True

    _physical(root, transaction["receipt"]).unlink()
    recovered_terminal = rotation.rotate_host_staging(
        SUCCESSOR,
        filesystem_root=root,
        require_root=False,
        lock_factory=_lock,
        stage_successor=lambda _revision: pytest.fail("must not restage"),
    )
    assert recovered_terminal == receipt
    assert _physical(root, transaction["receipt"]).is_file()


def test_rotation_resumes_after_successor_staging_failure(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    predecessor = _stage_set(root, PREDECESSOR, names=("one",))

    def fail(_revision: str) -> Mapping[str, Any]:
        raise RuntimeError("synthetic staging failure")

    with pytest.raises(
        rotation.HostStagingRotationError,
        match="host_staging_rotation_successor_staging_failed",
    ):
        rotation.rotate_host_staging(
            SUCCESSOR,
            filesystem_root=root,
            require_root=False,
            lock_factory=_lock,
            stage_successor=fail,
        )

    assert not _physical(root, rotation.HOST_STAGING_ROOT).exists()
    assert not _physical(root, rotation.STAGING_RECEIPT_PATH).exists()
    rotation_id = rotation._rotation_id(
        predecessor["receipt_sha256"], SUCCESSOR
    )
    transaction = rotation._transaction_paths(rotation_id)
    assert _physical(root, transaction["predecessor_host"]).is_dir()
    assert _physical(root, transaction["predecessor_receipt"]).is_file()

    resumed = rotation.rotate_host_staging(
        SUCCESSOR,
        filesystem_root=root,
        require_root=False,
        lock_factory=_lock,
        stage_successor=lambda revision: _stage_set(
            root, revision, names=("one", "two")
        ),
    )
    assert resumed["rotation_id"] == rotation_id
    assert resumed["successor_readback_verified"] is True


def test_rotation_resumes_after_host_archive_before_receipt_archive(
    tmp_path: Path,
) -> None:
    root = _fixture_root(tmp_path)
    predecessor = _stage_set(root, PREDECESSOR, names=("one",))
    rotation_id = rotation._rotation_id(
        predecessor["receipt_sha256"], SUCCESSOR
    )
    paths = rotation._transaction_paths(rotation_id)
    _private_directory(_physical(root, rotation.ROTATION_ROOT))
    _private_directory(_physical(root, paths["root"]))
    unsigned_intent = {
        "schema": rotation.ROTATION_SCHEMA,
        "rotation_id": rotation_id,
        "predecessor_release_revision": PREDECESSOR,
        "predecessor_receipt_sha256": predecessor["receipt_sha256"],
        "successor_release_revision": SUCCESSOR,
        "host_staging_path": str(rotation.HOST_STAGING_ROOT),
        "staging_receipt_path": str(rotation.STAGING_RECEIPT_PATH),
        "predecessor_archive_path": str(paths["predecessor_host"]),
        "predecessor_receipt_archive_path": str(paths["predecessor_receipt"]),
        "production_service_mutation_authorized": False,
    }
    intent = {
        **unsigned_intent,
        "intent_sha256": rotation._sha(rotation._canonical(unsigned_intent)),
    }
    _write_exact(_physical(root, paths["intent"]), rotation._canonical(intent))
    os.rename(
        _physical(root, rotation.HOST_STAGING_ROOT),
        _physical(root, paths["predecessor_host"]),
    )

    receipt = rotation.rotate_host_staging(
        SUCCESSOR,
        filesystem_root=root,
        require_root=False,
        lock_factory=_lock,
        stage_successor=lambda revision: _stage_set(
            root, revision, names=("one", "two")
        ),
    )
    assert receipt["rotation_id"] == rotation_id
    assert _physical(root, paths["predecessor_receipt"]).is_file()
    assert _physical(root, rotation.STAGING_RECEIPT_PATH).read_bytes() != rotation._canonical(
        predecessor
    )


@pytest.mark.parametrize(
    "forbidden",
    (
        cutover.STAGED_FREEZE_PLAN_PATH,
        cutover.STAGED_FREEZE_APPROVAL_PATH,
        cutover.STAGED_CUTOVER_PLAN_PATH,
    ),
)
def test_rotation_refuses_after_freeze_or_cutover_authority(
    tmp_path: Path,
    forbidden: Path,
) -> None:
    root = _fixture_root(tmp_path)
    _stage_set(root, PREDECESSOR, names=("one",))
    _write_exact(_physical(root, forbidden), b"fixed")

    with pytest.raises(
        rotation.HostStagingRotationError,
        match="host_staging_rotation_after_freeze_forbidden",
    ):
        rotation.rotate_host_staging(
            SUCCESSOR,
            filesystem_root=root,
            require_root=False,
            lock_factory=_lock,
            stage_successor=lambda _revision: pytest.fail("must not stage"),
        )


def test_rotation_rejects_changed_predecessor_bytes(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    _stage_set(root, PREDECESSOR, names=("one",))
    artifact = _physical(root, rotation.HOST_STAGING_ROOT) / "artifact-0.json"
    artifact.chmod(0o600)
    artifact.write_bytes(b"changed")
    artifact.chmod(0o400)

    with pytest.raises(
        rotation.HostStagingRotationError,
        match="host_staging_rotation_host_set_invalid",
    ):
        rotation.rotate_host_staging(
            SUCCESSOR,
            filesystem_root=root,
            require_root=False,
            lock_factory=_lock,
            stage_successor=lambda _revision: pytest.fail("must not stage"),
        )
