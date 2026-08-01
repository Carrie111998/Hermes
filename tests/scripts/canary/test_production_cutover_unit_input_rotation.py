from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway import canonical_writer_production_cutover as cutover
from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_owner_launcher as owner
from scripts.canary import production_cutover_public_stager as stager
from scripts.canary import production_cutover_unit_input_rotation as rotation
from scripts.canary import production_release_unit_inputs_v4 as unit_v4
from scripts.canary import production_release_update_contract as release_update
from tests.scripts.canary import test_production_release_unit_inputs_v4 as v4_tests
from tests.scripts.canary.test_production_cutover_owner_launcher import (
    _canonical,
    _patch_staged_paths,
    _rotation_state,
    _runtime_attestation,
    _unit_input_authority,
    _unit_input_payload,
)

_LEGACY_RELEASE_FINALIZED_RECEIPT_SCHEMA = (
    "muncho-production-release-unit-input-rotation-receipt.v3"
)


def _live_triplet() -> dict[str, bytes]:
    return {
        "plan": package.STAGED_UNIT_INPUT_PLAN_PATH.read_bytes(),
        "approval": package.STAGED_UNIT_INPUT_APPROVAL_PATH.read_bytes(),
        "fixed": package.FIXED_UNIT_INPUTS_PATH.read_bytes(),
    }


def _release_documents(
    monkeypatch: pytest.MonkeyPatch,
    *,
    private_key: Ed25519PrivateKey,
    trusted_predecessor: dict[str, Any],
    target_revision: str,
    now: int,
) -> dict[str, Any]:
    monkeypatch.setattr(v4_tests, "TARGET", target_revision)
    monkeypatch.setattr(v4_tests, "NOW", now)
    payload = v4_tests._payload(v4_tests._v3_payload())
    plan, approval, publication = v4_tests._unit_documents(
        private_key,
        trusted_predecessor,
        payload,
    )
    update_plan, update_approval, update_publication = (
        v4_tests._release_update_documents(
            private_key,
            trusted_predecessor,
            payload,
            publication,
        )
    )
    fixed = dict(
        unit_v4.derive_fixed_inputs(
            unit_input_publication=publication,
            release_update_publication=update_publication,
            trusted_predecessor=trusted_predecessor,
            expected_predecessor_trust_sha256=str(
                trusted_predecessor["trust_sha256"]
            ),
            now_unix=now,
        )
    )
    return {
        "plan": plan,
        "approval": approval,
        "publication": publication,
        "update_plan": update_plan,
        "update_approval": update_approval,
        "update_publication": update_publication,
        "fixed": fixed,
    }


def _release_rotation_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    now: int,
    target_revision: str = "b" * 40,
) -> tuple[
    Ed25519PrivateKey,
    tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir(mode=0o700)
    evidence.chmod(0o700)
    os.chown(evidence, os.geteuid(), os.getegid())  # windows-footgun: ok
    staged = evidence / "staged"
    _patch_staged_paths(monkeypatch, staged)
    monkeypatch.setattr(cutover, "EVIDENCE_ROOT", evidence)
    predecessor_revision = "a" * 40
    private_key = Ed25519PrivateKey.generate()
    predecessor = owner.build_unit_input_authority(
        release_revision=predecessor_revision,
        unit_inputs=_unit_input_payload(predecessor_revision),
        owner_subject_sha256="a" * 64,
        private_key=private_key,
        owner_runtime_attestation=_runtime_attestation(
            predecessor_revision
        ),
        now_unix=now,
    )
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
    fixed_raw = package.FIXED_UNIT_INPUTS_PATH.read_bytes()
    trusted = dict(
        release_update.build_predecessor_trust(
            release_revision=predecessor_revision,
            authority_plan_sha256=predecessor[0]["plan_sha256"],
            authority_approval_sha256=predecessor[1][
                "approval_sha256"
            ],
            fixed_inputs_sha256=hashlib.sha256(fixed_raw).hexdigest(),
            activation_receipt_sha256="04" * 32,
            owner_subject_sha256=predecessor[0][
                "owner_subject_sha256"
            ],
            owner_public_key_ed25519_hex=predecessor[0][
                "owner_public_key_ed25519_hex"
            ],
            owner_key_id=predecessor[0]["owner_key_id"],
        )
    )
    documents = _release_documents(
        monkeypatch,
        private_key=private_key,
        trusted_predecessor=trusted,
        target_revision=target_revision,
        now=now,
    )
    return private_key, predecessor, trusted, documents


def _prepare_release(
    documents: dict[str, Any],
    trusted: dict[str, Any],
    *,
    now: int,
) -> dict[str, Any]:
    return dict(
        rotation._prepare_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            require_root=False,
            clock=lambda: now,
            lock_factory=nullcontext,
        )
    )


def _finalize_release(
    documents: dict[str, Any],
    trusted: dict[str, Any],
    prepared: dict[str, Any],
    *,
    now: int | None = None,
) -> dict[str, Any]:
    observed = (
        prepared["authorization_checked_at_unix"]
        if now is None
        else now
    )
    return dict(
        rotation._finalize_prepared_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            clock=lambda: observed,
            lock_factory=nullcontext,
        )
    )


def _preauthorize_release(
    documents: dict[str, Any],
    trusted: dict[str, Any],
    prepared: dict[str, Any],
    *,
    now: int,
) -> dict[str, Any]:
    return dict(
        rotation._preauthorize_prepared_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            clock=lambda: now,
            lock_factory=nullcontext,
        )
    )


def _finalize_preauthorized_release(
    documents: dict[str, Any],
    trusted: dict[str, Any],
    prepared: dict[str, Any],
    preauthorization: dict[str, Any],
) -> dict[str, Any]:
    return dict(
        rotation._finalize_preauthorized_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            preauthorization,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            lock_factory=nullcontext,
        )
    )


def _abort_preauthorized_release(
    documents: dict[str, Any],
    trusted: dict[str, Any],
    prepared: dict[str, Any],
    preauthorization: dict[str, Any],
) -> dict[str, Any]:
    return dict(
        rotation._abort_preauthorized_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            preauthorization,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            lock_factory=nullcontext,
        )
    )


def _tree_snapshot(
    root: Path,
) -> dict[str, tuple[str, int, bytes | None]]:
    paths = [root, *sorted(root.rglob("*"))]
    snapshot: dict[str, tuple[str, int, bytes | None]] = {}
    for path in paths:
        relative = "." if path == root else path.relative_to(root).as_posix()
        observed = path.lstat()
        if stat.S_ISDIR(observed.st_mode):
            snapshot[relative] = (
                "directory",
                stat.S_IMODE(observed.st_mode),
                None,
            )
        else:
            snapshot[relative] = (
                "file",
                stat.S_IMODE(observed.st_mode),
                path.read_bytes(),
            )
    return snapshot


def _downgrade_finalized_release_evidence_to_legacy_v4(
    prepared: dict[str, Any],
) -> dict[str, Any]:
    transaction = Path(prepared["audit_transaction_path"])
    activation_path = transaction / rotation.ACTIVATION_BEGIN_FILE_NAME
    receipt_path = transaction / rotation.RECEIPT_FILE_NAME
    current = json.loads(receipt_path.read_bytes())
    assert current["schema"] == rotation.RELEASE_FINALIZED_RECEIPT_SCHEMA
    assert activation_path.exists()

    activation_path.unlink()
    legacy = {
        name: item
        for name, item in current.items()
        if name not in {"activation_begin_sha256", "receipt_sha256"}
    }
    legacy["schema"] = _LEGACY_RELEASE_FINALIZED_RECEIPT_SCHEMA
    legacy["receipt_sha256"] = hashlib.sha256(
        _canonical(legacy)
    ).hexdigest()
    receipt_path.unlink()
    receipt_path.write_bytes(_canonical(legacy))
    receipt_path.chmod(0o400)

    assert set(legacy) == (
        rotation._RELEASE_FINALIZED_RECEIPT_FIELDS
        - {"activation_begin_sha256"}
    )
    assert legacy["receipt_sha256"] == hashlib.sha256(
        _canonical(
            {
                name: item
                for name, item in legacy.items()
                if name != "receipt_sha256"
            }
        )
    ).hexdigest()
    return legacy


class _SimulatedInstallPowerLoss(BaseException):
    pass


def _temporary_aliases(path: Path) -> list[Path]:
    prefix = f".{path.name}.rotate."
    return sorted(
        child
        for child in path.parent.iterdir()
        if child.name.startswith(prefix)
    )


def _inject_install_power_loss(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target: Path,
    boundary: str,
) -> None:
    checkpoint = {
        "create": "temporary_created",
        "partial_write": "temporary_write_progress",
        "write": "temporary_written",
        "chmod": "temporary_chmod",
        "fsync": "temporary_fsynced",
        "rename": "destination_installed",
        "link": "destination_installed",
    }[boundary]
    expected = f"install_exact:{target.name}:{checkpoint}"

    def crash(stage: str) -> None:
        if stage == expected:
            raise _SimulatedInstallPowerLoss

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    if boundary == "partial_write":
        write = rotation.os.write

        def short_write(descriptor: int, value: Any) -> int:
            view = memoryview(value)
            return write(
                descriptor,
                view[: max(1, len(view) // 2)],
            )

        monkeypatch.setattr(rotation.os, "write", short_write)
    if boundary in {"rename", "link"}:

        def install(
            source: Path,
            destination: Path,
            *,
            directory_fd: int,
            expected_identity: tuple[int, int],
        ) -> bool:
            observed = os.stat(
                source.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            assert (observed.st_dev, observed.st_ino) == expected_identity
            if boundary == "rename":
                try:
                    os.stat(
                        destination.name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    return False
                os.rename(
                    source.name,
                    destination.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            else:
                try:
                    os.link(
                        source.name,
                        destination.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    return False
            return True

        monkeypatch.setattr(rotation, "_rename_noreplace", install)


def test_prepare_persists_exact_authorization_without_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()

    prepared = rotation.prepare_unit_input_authority_rotation(
        publication=successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )

    assert _live_triplet() == before
    assert prepared["schema"] == rotation.PREPARED_RECEIPT_SCHEMA
    assert prepared["live_triplet_unchanged"] is True
    assert prepared["mutation_performed"] is False
    transaction = Path(prepared["audit_transaction_path"])
    assert (transaction / rotation.TRANSACTION_FILE_NAME).exists()
    assert (transaction / rotation.PUBLICATION_FILE_NAME).exists()
    assert (
        transaction / rotation.PREPARED_RECEIPT_FILE_NAME
    ).read_bytes() == _canonical(prepared)
    assert not (transaction / rotation.RECEIPT_FILE_NAME).exists()
    assert rotation.validate_prepared_rotation_receipt(
        prepared,
        publication=successor[2],
    ) == prepared


def test_prepare_rechecks_freshness_after_lock_acquisition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()
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
        rotation.prepare_unit_input_authority_rotation(
            successor[2],
            require_root=False,
            lock_factory=ExpireWhileWaiting,
        )

    assert _live_triplet() == before
    audit = cutover.EVIDENCE_ROOT / rotation.AUDIT_DIRECTORY_NAME
    assert not audit.exists() or list(audit.iterdir()) == []


def test_prepare_exact_replay_survives_approval_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()
    prepared = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )

    replay = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )

    assert replay == prepared
    assert _live_triplet() == before


def test_prepare_recovers_exact_receipt_publish_checkpoint_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()

    def crash(stage: str) -> None:
        if stage == "prepared_receipt_published":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.prepare_unit_input_authority_rotation(
            successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )
    assert _live_triplet() == before

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    replay = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )
    persisted = json.loads(
        (
            Path(replay["audit_transaction_path"])
            / rotation.PREPARED_RECEIPT_FILE_NAME
        ).read_bytes()
    )
    assert replay == persisted
    assert _live_triplet() == before


def test_finalize_rejects_mismatched_caller_authorization_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    before = _live_triplet()

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_finalize_authorization_invalid",
    ):
        rotation.finalize_prepared_unit_input_authority_rotation(
            successor[2],
            prepared,
            expected_transaction_sha256="f" * 64,
            require_root=False,
            lock_factory=nullcontext,
        )

    changed = {**prepared, "receipt_sha256": "e" * 64}
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_prepared_receipt_invalid",
    ):
        rotation.finalize_prepared_unit_input_authority_rotation(
            successor[2],
            changed,
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            lock_factory=nullcontext,
        )

    different = _unit_input_authority("c" * 40, now)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_prepared_receipt_invalid",
    ):
        rotation.finalize_prepared_unit_input_authority_rotation(
            different[2],
            prepared,
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            lock_factory=nullcontext,
        )
    assert _live_triplet() == before


def test_finalize_uses_persisted_authorization_without_freshness_recheck(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )
    monkeypatch.setattr(rotation.time, "time", lambda: now + 4_000)

    receipt = rotation.finalize_prepared_unit_input_authority_rotation(
        publication=successor[2],
        prepared_receipt=prepared,
        expected_transaction_sha256=prepared["transaction_sha256"],
        require_root=False,
        lock_factory=nullcontext,
    )
    replay = rotation.finalize_prepared_unit_input_authority_rotation(
        successor[2],
        prepared,
        expected_transaction_sha256=prepared["transaction_sha256"],
        require_root=False,
        lock_factory=nullcontext,
    )

    assert replay == receipt
    assert receipt["schema"] == rotation.FINALIZED_RECEIPT_SCHEMA
    assert (
        receipt["prepared_receipt_sha256"]
        == prepared["receipt_sha256"]
    )
    assert package.STAGED_UNIT_INPUT_PLAN_PATH.read_bytes() == _canonical(
        successor[0]
    )
    assert package.STAGED_UNIT_INPUT_APPROVAL_PATH.read_bytes() == _canonical(
        successor[1]
    )
    assert package.FIXED_UNIT_INPUTS_PATH.read_bytes() == (
        _canonical(
            package._unit_inputs_from_authority(
                successor[0],
                successor[1],
            )
        )
        + b"\n"
    )


def test_legacy_rotate_wrapper_can_finish_an_exact_prepared_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )

    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 4_000,
        lock_factory=nullcontext,
    )
    replay = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now + 4_001,
        lock_factory=nullcontext,
    )

    assert replay == receipt
    assert receipt["schema"] == rotation.FINALIZED_RECEIPT_SCHEMA
    assert (
        receipt["prepared_receipt_sha256"]
        == prepared["receipt_sha256"]
    )


def test_legacy_rotate_accepts_exact_pre_edge_expansion_v3_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_800_000_000
    evidence = (tmp_path / "evidence").resolve()
    evidence.mkdir(mode=0o700)
    evidence.chmod(0o700)
    os.chown(evidence, os.geteuid(), os.getegid())
    staged = evidence / "staged"
    staged.mkdir(mode=0o700)
    _patch_staged_paths(monkeypatch, staged)
    monkeypatch.setattr(cutover, "EVIDENCE_ROOT", evidence)

    private_key = Ed25519PrivateKey.generate()
    predecessor = owner.build_unit_input_authority(
        release_revision="a" * 40,
        unit_inputs=_unit_input_payload("a" * 40),
        owner_subject_sha256="a" * 64,
        private_key=private_key,
        owner_runtime_attestation=_runtime_attestation("a" * 40),
        now_unix=now,
    )
    successor = owner.build_unit_input_authority(
        release_revision="b" * 40,
        unit_inputs=_unit_input_payload("b" * 40),
        owner_subject_sha256="a" * 64,
        private_key=private_key,
        owner_runtime_attestation=_runtime_attestation("b" * 40),
        now_unix=now,
    )

    plan = deepcopy(predecessor[0])
    removed = set(package.CREDENTIALS_BY_DOMAIN) - set(
        package.LEGACY_V3_OPERATIONAL_EDGE_DOMAINS
    )
    assert removed == {"skyvision_backup", "skyvision_seo"}
    for field in (
        "operational_edge_identities",
        "operational_edge_socket_groups",
        "operational_edge_receipt_public_key_ids",
    ):
        for domain in removed:
            del plan["unit_inputs"][field][domain]
    plan_without_hash = {
        name: item for name, item in plan.items() if name != "plan_sha256"
    }
    plan["plan_sha256"] = hashlib.sha256(
        _canonical(plan_without_hash)
    ).hexdigest()
    plan = dict(
        package.validate_unit_input_plan(
            plan,
            operational_edge_domains=(
                package.LEGACY_V3_OPERATIONAL_EDGE_DOMAINS
            ),
        )
    )

    approval = deepcopy(predecessor[1])
    approval["plan_sha256"] = plan["plan_sha256"]
    approval["signature_ed25519_hex"] = "0" * 128
    approval["approval_sha256"] = "0" * 64
    approval["signature_ed25519_hex"] = private_key.sign(
        package.unit_input_approval_signature_payload(approval)
    ).hex()
    approval["approval_sha256"] = hashlib.sha256(
        _canonical(
            {
                name: item
                for name, item in approval.items()
                if name != "approval_sha256"
            }
        )
    ).hexdigest()
    approval = dict(
        package.validate_unit_input_approval(
            approval,
            plan=plan,
            now_unix=now,
        )
    )
    fixed = package._unit_inputs_from_authority(
        plan,
        approval,
        operational_edge_domains=package.LEGACY_V3_OPERATIONAL_EDGE_DOMAINS,
    )
    for path, payload, mode in (
        (package.STAGED_UNIT_INPUT_PLAN_PATH, _canonical(plan), 0o400),
        (
            package.STAGED_UNIT_INPUT_APPROVAL_PATH,
            _canonical(approval),
            0o400,
        ),
        (
            package.FIXED_UNIT_INPUTS_PATH,
            _canonical(fixed) + b"\n",
            package.FIXED_UNIT_INPUTS_MODE,
        ),
    ):
        path.write_bytes(payload)
        path.chmod(mode)

    receipt = rotation.rotate_unit_input_authority(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )

    assert receipt["predecessor_revision"] == plan["release_revision"]
    assert receipt["successor_revision"] == successor[0]["release_revision"]
    assert _live_triplet() == {
        "plan": _canonical(successor[0]),
        "approval": _canonical(successor[1]),
        "fixed": _canonical(
            package._unit_inputs_from_authority(successor[0], successor[1])
        )
        + b"\n",
    }
    archived = Path(receipt["audit_transaction_path"]) / "predecessor"
    assert json.loads((archived / "unit-input-plan.json").read_bytes()) == plan


def test_rotate_reads_completed_exact_legacy_v3_audit_before_current_successor(
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
    monkeypatch.setattr(cutover, "EVIDENCE_ROOT", evidence)

    private_key = Ed25519PrivateKey.generate()
    legacy_credentials = {
        domain: package.CREDENTIALS_BY_DOMAIN[domain]
        for domain in package.LEGACY_V3_OPERATIONAL_EDGE_DOMAINS
    }
    with monkeypatch.context() as legacy:
        legacy.setattr(package, "CREDENTIALS_BY_DOMAIN", legacy_credentials)
        predecessor = owner.build_unit_input_authority(
            release_revision="a" * 40,
            unit_inputs=_unit_input_payload("a" * 40),
            owner_subject_sha256="a" * 64,
            private_key=private_key,
            owner_runtime_attestation=_runtime_attestation("a" * 40),
            now_unix=now,
        )
        legacy_successor = owner.build_unit_input_authority(
            release_revision="b" * 40,
            unit_inputs=_unit_input_payload("b" * 40),
            owner_subject_sha256="a" * 64,
            private_key=private_key,
            owner_runtime_attestation=_runtime_attestation("b" * 40),
            now_unix=now,
        )
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
        legacy_receipt = rotation.rotate_unit_input_authority(
            legacy_successor[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )

    current_successor = owner.build_unit_input_authority(
        release_revision="c" * 40,
        unit_inputs=_unit_input_payload("c" * 40),
        owner_subject_sha256="a" * 64,
        private_key=private_key,
        owner_runtime_attestation=_runtime_attestation("c" * 40),
        now_unix=now + 1,
    )
    current_receipt = rotation.rotate_unit_input_authority(
        current_successor[2],
        require_root=False,
        now_unix=now + 1,
        lock_factory=nullcontext,
    )
    replay = rotation.rotate_unit_input_authority(
        current_successor[2],
        require_root=False,
        now_unix=now + 2,
        lock_factory=nullcontext,
    )

    assert legacy_receipt["successor_revision"] == "b" * 40
    assert current_receipt["predecessor_revision"] == "b" * 40
    assert current_receipt["successor_revision"] == "c" * 40
    assert replay == current_receipt
    assert _live_triplet() == {
        "plan": _canonical(current_successor[0]),
        "approval": _canonical(current_successor[1]),
        "fixed": _canonical(
            package._unit_inputs_from_authority(
                current_successor[0],
                current_successor[1],
            )
        )
        + b"\n",
    }


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
def test_finalize_exact_recovery_after_every_live_mutation_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_stage: str,
) -> None:
    now = 1_800_000_000
    _staged, _predecessor, successor = _rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = rotation.prepare_unit_input_authority_rotation(
        successor[2],
        require_root=False,
        now_unix=now,
        lock_factory=nullcontext,
    )

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        rotation.finalize_prepared_unit_input_authority_rotation(
            successor[2],
            prepared,
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            lock_factory=nullcontext,
        )
    transaction = Path(prepared["audit_transaction_path"])
    assert not (transaction / rotation.RECEIPT_FILE_NAME).exists()

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    receipt = rotation.finalize_prepared_unit_input_authority_rotation(
        successor[2],
        prepared,
        expected_transaction_sha256=prepared["transaction_sha256"],
        require_root=False,
        lock_factory=nullcontext,
    )

    assert receipt["schema"] == rotation.FINALIZED_RECEIPT_SCHEMA
    assert (
        receipt["prepared_receipt_sha256"]
        == prepared["receipt_sha256"]
    )
    assert package.STAGED_UNIT_INPUT_PLAN_PATH.read_bytes() == _canonical(
        successor[0]
    )
    persisted = json.loads(
        (transaction / rotation.RECEIPT_FILE_NAME).read_bytes()
    )
    assert persisted == receipt


def test_release_v3_to_v4_prepare_and_finalize_bind_exact_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()

    prepared = _prepare_release(documents, trusted, now=now)

    assert _live_triplet() == before
    assert prepared["schema"] == rotation.RELEASE_PREPARED_RECEIPT_SCHEMA
    assert prepared["predecessor"]["authority_version"] == "v3"
    assert prepared["successor"]["authority_version"] == "v4"
    assert prepared["successor"]["publication_sha256"] == (
        documents["publication"]["publication_sha256"]
    )
    assert prepared["successor"][
        "release_update_publication_sha256"
    ] == documents["update_publication"]["publication_sha256"]
    assert prepared["predecessor_trust_sha256"] == trusted["trust_sha256"]
    assert rotation.validate_release_prepared_rotation_receipt(
        prepared,
        unit_input_publication=documents["publication"],
        release_update_publication=documents["update_publication"],
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=trusted["trust_sha256"],
    ) == prepared
    transaction = Path(prepared["audit_transaction_path"])
    archived = transaction / rotation.PREDECESSOR_DIRECTORY_NAME
    assert (archived / "unit-input-plan.json").read_bytes() == _canonical(
        predecessor[0]
    )
    assert (
        transaction / rotation.RELEASE_UPDATE_PUBLICATION_FILE_NAME
    ).read_bytes() == _canonical(documents["update_publication"])
    assert (
        transaction / rotation.PREDECESSOR_TRUST_FILE_NAME
    ).read_bytes() == _canonical(trusted)

    receipt = _finalize_release(documents, trusted, prepared)

    assert receipt["schema"] == rotation.RELEASE_FINALIZED_RECEIPT_SCHEMA
    assert receipt["prepared_receipt_sha256"] == prepared["receipt_sha256"]
    assert package.STAGED_UNIT_INPUT_PLAN_PATH.read_bytes() == _canonical(
        documents["plan"]
    )
    assert package.STAGED_UNIT_INPUT_APPROVAL_PATH.read_bytes() == _canonical(
        documents["approval"]
    )
    assert package.FIXED_UNIT_INPUTS_PATH.read_bytes() == (
        _canonical(documents["fixed"]) + b"\n"
    )
    for path, mode in (
        (package.STAGED_UNIT_INPUT_PLAN_PATH, 0o400),
        (package.STAGED_UNIT_INPUT_APPROVAL_PATH, 0o400),
        (
            package.FIXED_UNIT_INPUTS_PATH,
            package.FIXED_UNIT_INPUTS_MODE,
        ),
    ):
        observed = path.stat()
        assert observed.st_uid == os.geteuid()  # windows-footgun: ok
        assert observed.st_gid == os.getegid()  # windows-footgun: ok
        assert stat.S_IMODE(observed.st_mode) == mode
    assert rotation.validate_release_rotation_receipt(
        receipt,
        unit_input_publication=documents["publication"],
        release_update_publication=documents["update_publication"],
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=trusted["trust_sha256"],
        prepared_receipt=prepared,
        mutation_begin=json.loads(
            (
                transaction / rotation.MUTATION_BEGIN_FILE_NAME
            ).read_bytes()
        ),
        activation_begin=json.loads(
            (
                transaction / rotation.ACTIVATION_BEGIN_FILE_NAME
            ).read_bytes()
        ),
    ) == receipt


@pytest.mark.parametrize(
    "forged_link",
    (
        "prepared_receipt_sha256",
        "mutation_begin_sha256",
        "activation_begin_sha256",
    ),
)
def test_release_final_receipt_rejects_rehashed_forged_evidence_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    forged_link: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    receipt = _finalize_release(documents, trusted, prepared)
    transaction = Path(prepared["audit_transaction_path"])
    mutation = json.loads(
        (transaction / rotation.MUTATION_BEGIN_FILE_NAME).read_bytes()
    )
    activation = json.loads(
        (transaction / rotation.ACTIVATION_BEGIN_FILE_NAME).read_bytes()
    )
    forged = {**receipt, forged_link: "ff" * 32}
    unsigned = {
        name: item
        for name, item in forged.items()
        if name != "receipt_sha256"
    }
    forged["receipt_sha256"] = hashlib.sha256(
        _canonical(unsigned)
    ).hexdigest()

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_receipt_invalid",
    ):
        rotation.validate_release_rotation_receipt(
            forged,
            unit_input_publication=documents["publication"],
            release_update_publication=documents["update_publication"],
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            prepared_receipt=prepared,
            mutation_begin=mutation,
            activation_begin=activation,
        )


def test_release_v4_to_v4_rotation_and_v3_downgrade_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, first = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    first_prepared = _prepare_release(first, trusted, now=now)
    _finalize_release(first, trusted, first_prepared)
    current_fixed = package.FIXED_UNIT_INPUTS_PATH.read_bytes()
    next_trusted = dict(
        release_update.build_predecessor_trust(
            release_revision=first["plan"]["release_revision"],
            authority_plan_sha256=first["plan"]["plan_sha256"],
            authority_approval_sha256=first["approval"][
                "approval_sha256"
            ],
            fixed_inputs_sha256=first["fixed"]["fixed_inputs_sha256"],
            activation_receipt_sha256="24" * 32,
            owner_subject_sha256=first["plan"]["owner_subject_sha256"],
            owner_public_key_ed25519_hex=first["plan"][
                "owner_public_key_ed25519_hex"
            ],
            owner_key_id=first["plan"]["owner_key_id"],
        )
    )
    second = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=next_trusted,
        target_revision="c" * 40,
        now=now,
    )

    second_prepared = _prepare_release(second, next_trusted, now=now)

    assert second_prepared["predecessor"]["authority_version"] == "v4"
    archived = (
        Path(second_prepared["audit_transaction_path"])
        / rotation.PREDECESSOR_DIRECTORY_NAME
    )
    assert json.loads(
        (archived / "unit-input-plan.json").read_bytes()
    )["schema"] == unit_v4.PLAN_SCHEMA
    assert (
        archived / "production-unit-inputs.json"
    ).read_bytes() == current_fixed
    second_receipt = _finalize_release(
        second,
        next_trusted,
        second_prepared,
    )
    assert second_receipt["successor"]["revision"] == "c" * 40

    downgrade = _unit_input_authority("d" * 40, now)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_predecessor_invalid",
    ):
        rotation.prepare_unit_input_authority_rotation(
            downgrade[2],
            require_root=False,
            now_unix=now,
            lock_factory=nullcontext,
        )


def test_release_v5_ignores_immutable_legacy_v4_finalized_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, first = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    v5_root_name = rotation.RELEASE_AUDIT_DIRECTORY_NAME
    assert v5_root_name != rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME
    monkeypatch.setattr(
        rotation,
        "RELEASE_AUDIT_DIRECTORY_NAME",
        rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME,
    )
    first_prepared = _prepare_release(first, trusted, now=now)
    first_preauthorization = _preauthorize_release(
        first,
        trusted,
        first_prepared,
        now=now,
    )
    _finalize_preauthorized_release(
        first,
        trusted,
        first_prepared,
        first_preauthorization,
    )
    legacy_receipt = _downgrade_finalized_release_evidence_to_legacy_v4(
        first_prepared
    )
    legacy_root = (
        cutover.EVIDENCE_ROOT
        / rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME
    )
    assert (
        Path(first_prepared["audit_transaction_path"]).parent
        == legacy_root
    )
    assert (
        legacy_receipt["schema"]
        == _LEGACY_RELEASE_FINALIZED_RECEIPT_SCHEMA
    )
    legacy_before = _tree_snapshot(legacy_root)

    monkeypatch.setattr(
        rotation,
        "RELEASE_AUDIT_DIRECTORY_NAME",
        v5_root_name,
    )
    next_trusted = dict(
        release_update.build_predecessor_trust(
            release_revision=first["plan"]["release_revision"],
            authority_plan_sha256=first["plan"]["plan_sha256"],
            authority_approval_sha256=first["approval"][
                "approval_sha256"
            ],
            fixed_inputs_sha256=first["fixed"]["fixed_inputs_sha256"],
            activation_receipt_sha256="24" * 32,
            owner_subject_sha256=first["plan"]["owner_subject_sha256"],
            owner_public_key_ed25519_hex=first["plan"][
                "owner_public_key_ed25519_hex"
            ],
            owner_key_id=first["plan"]["owner_key_id"],
        )
    )
    second = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=next_trusted,
        target_revision="c" * 40,
        now=now,
    )

    second_prepared = _prepare_release(second, next_trusted, now=now)
    second_preauthorization = _preauthorize_release(
        second,
        next_trusted,
        second_prepared,
        now=now,
    )
    second_receipt = _finalize_preauthorized_release(
        second,
        next_trusted,
        second_prepared,
        second_preauthorization,
    )

    assert (
        Path(second_prepared["audit_transaction_path"]).parent
        == cutover.EVIDENCE_ROOT / v5_root_name
    )
    assert second_receipt["schema"] == (
        rotation.RELEASE_FINALIZED_RECEIPT_SCHEMA
    )
    assert second_receipt["successor"]["revision"] == "c" * 40
    assert _tree_snapshot(legacy_root) == legacy_before


def test_release_v5_ignores_immutable_legacy_v4_partial_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, first = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    second = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=trusted,
        target_revision="c" * 40,
        now=now,
    )
    v5_root_name = rotation.RELEASE_AUDIT_DIRECTORY_NAME
    monkeypatch.setattr(
        rotation,
        "RELEASE_AUDIT_DIRECTORY_NAME",
        rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME,
    )
    legacy_prepared = _prepare_release(first, trusted, now=now)
    legacy_preauthorization = _preauthorize_release(
        first,
        trusted,
        legacy_prepared,
        now=now,
    )
    legacy_transaction = Path(
        legacy_prepared["audit_transaction_path"]
    )
    assert (
        legacy_transaction / rotation.MUTATION_BEGIN_FILE_NAME
    ).read_bytes() == _canonical(legacy_preauthorization)
    assert not (
        legacy_transaction / rotation.ACTIVATION_BEGIN_FILE_NAME
    ).exists()
    assert not (legacy_transaction / rotation.RECEIPT_FILE_NAME).exists()
    legacy_root = (
        cutover.EVIDENCE_ROOT
        / rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME
    )
    legacy_before = _tree_snapshot(legacy_root)

    monkeypatch.setattr(
        rotation,
        "RELEASE_AUDIT_DIRECTORY_NAME",
        v5_root_name,
    )
    second_prepared = _prepare_release(second, trusted, now=now)
    second_preauthorization = _preauthorize_release(
        second,
        trusted,
        second_prepared,
        now=now,
    )
    second_receipt = _finalize_preauthorized_release(
        second,
        trusted,
        second_prepared,
        second_preauthorization,
    )

    assert (
        Path(second_prepared["audit_transaction_path"]).parent
        == cutover.EVIDENCE_ROOT / v5_root_name
    )
    assert second_receipt["successor"]["revision"] == "c" * 40
    assert _tree_snapshot(legacy_root) == legacy_before


def test_release_v5_ignores_legacy_evidence_but_rejects_mixed_live_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, first = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    second = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=trusted,
        target_revision="c" * 40,
        now=now,
    )
    v5_root_name = rotation.RELEASE_AUDIT_DIRECTORY_NAME
    monkeypatch.setattr(
        rotation,
        "RELEASE_AUDIT_DIRECTORY_NAME",
        rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME,
    )
    legacy_prepared = _prepare_release(first, trusted, now=now)
    _preauthorize_release(
        first,
        trusted,
        legacy_prepared,
        now=now,
    )
    legacy_root = (
        cutover.EVIDENCE_ROOT
        / rotation.LEGACY_RELEASE_AUDIT_DIRECTORY_NAME
    )
    legacy_before = _tree_snapshot(legacy_root)
    monkeypatch.setattr(
        rotation,
        "RELEASE_AUDIT_DIRECTORY_NAME",
        v5_root_name,
    )

    package.FIXED_UNIT_INPUTS_PATH.unlink()
    package.FIXED_UNIT_INPUTS_PATH.write_bytes(
        _canonical(first["fixed"]) + b"\n"
    )
    package.FIXED_UNIT_INPUTS_PATH.chmod(package.FIXED_UNIT_INPUTS_MODE)

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_predecessor_invalid",
    ):
        _prepare_release(second, trusted, now=now)

    v5_root = cutover.EVIDENCE_ROOT / v5_root_name
    assert not v5_root.exists() or list(v5_root.iterdir()) == []
    assert _tree_snapshot(legacy_root) == legacy_before


@pytest.mark.parametrize(
    "artifact",
    ("preauthorization", "activation_begin", "abort", "final_receipt"),
)
@pytest.mark.parametrize(
    "boundary",
    (
        "create",
        "partial_write",
        "write",
        "chmod",
        "fsync",
        "rename",
        "link",
    ),
)
def test_release_marker_install_recovers_every_syscall_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact: str,
    boundary: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    preauthorization: dict[str, Any] | None = None
    if artifact != "preauthorization":
        preauthorization = _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now,
        )
    target_name = {
        "preauthorization": rotation.MUTATION_BEGIN_FILE_NAME,
        "activation_begin": rotation.ACTIVATION_BEGIN_FILE_NAME,
        "abort": rotation.ABORT_RECEIPT_FILE_NAME,
        "final_receipt": rotation.RECEIPT_FILE_NAME,
    }[artifact]
    target = Path(prepared["audit_transaction_path"]) / target_name
    before = _live_triplet()

    with monkeypatch.context() as crash_patch:
        _inject_install_power_loss(
            crash_patch,
            target=target,
            boundary=boundary,
        )
        with pytest.raises(_SimulatedInstallPowerLoss):
            if artifact == "preauthorization":
                _preauthorize_release(
                    documents,
                    trusted,
                    prepared,
                    now=now,
                )
            elif artifact == "abort":
                assert preauthorization is not None
                _abort_preauthorized_release(
                    documents,
                    trusted,
                    prepared,
                    preauthorization,
                )
            else:
                assert preauthorization is not None
                _finalize_preauthorized_release(
                    documents,
                    trusted,
                    prepared,
                    preauthorization,
                )

    if boundary in {"create", "partial_write", "write"}:
        aliases = _temporary_aliases(target)
        assert len(aliases) == 1
        assert stat.S_IMODE(aliases[0].stat().st_mode) == 0o600
    elif boundary in {"chmod", "fsync"}:
        aliases = _temporary_aliases(target)
        assert len(aliases) == 1
        assert stat.S_IMODE(aliases[0].stat().st_mode) == 0o400
    elif boundary == "rename":
        assert target.exists()
        assert _temporary_aliases(target) == []
    else:
        aliases = _temporary_aliases(target)
        assert target.exists()
        assert len(aliases) == 1
        assert target.stat().st_ino == aliases[0].stat().st_ino
        assert target.stat().st_nlink == 2

    if artifact == "preauthorization":
        recovered = _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now,
        )
        assert recovered["schema"] == rotation.RELEASE_MUTATION_BEGIN_SCHEMA
        assert _live_triplet() == before
    elif artifact == "abort":
        assert preauthorization is not None
        recovered = _abort_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )
        assert recovered["schema"] == rotation.RELEASE_ABORTED_RECEIPT_SCHEMA
        assert _live_triplet() == before
    else:
        assert preauthorization is not None
        recovered = _finalize_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )
        assert recovered["schema"] == rotation.RELEASE_FINALIZED_RECEIPT_SCHEMA
    assert target.exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert _temporary_aliases(target) == []


def test_release_prepare_cannot_prune_activation_branch_scratch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )
    transaction = Path(prepared["audit_transaction_path"])
    activation = transaction / rotation.ACTIVATION_BEGIN_FILE_NAME

    with monkeypatch.context() as crash_patch:
        _inject_install_power_loss(
            crash_patch,
            target=activation,
            boundary="create",
        )
        with pytest.raises(_SimulatedInstallPowerLoss):
            _finalize_preauthorized_release(
                documents,
                trusted,
                prepared,
                preauthorization,
            )

    pending = _temporary_aliases(activation)
    assert len(pending) == 1
    identity = (pending[0].stat().st_dev, pending[0].stat().st_ino)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        _prepare_release(documents, trusted, now=now)
    assert (
        pending[0].stat().st_dev,
        pending[0].stat().st_ino,
    ) == identity
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_abort_authorization_invalid",
    ):
        _abort_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )
    assert (
        pending[0].stat().st_dev,
        pending[0].stat().st_ino,
    ) == identity

    receipt = _finalize_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )
    assert receipt["schema"] == rotation.RELEASE_FINALIZED_RECEIPT_SCHEMA
    assert _temporary_aliases(activation) == []


def test_release_prepare_cannot_prune_abort_branch_scratch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )
    transaction = Path(prepared["audit_transaction_path"])
    aborted = transaction / rotation.ABORT_RECEIPT_FILE_NAME

    with monkeypatch.context() as crash_patch:
        _inject_install_power_loss(
            crash_patch,
            target=aborted,
            boundary="create",
        )
        with pytest.raises(_SimulatedInstallPowerLoss):
            _abort_preauthorized_release(
                documents,
                trusted,
                prepared,
                preauthorization,
            )

    pending = _temporary_aliases(aborted)
    assert len(pending) == 1
    identity = (pending[0].stat().st_dev, pending[0].stat().st_ino)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        _prepare_release(documents, trusted, now=now)
    assert (
        pending[0].stat().st_dev,
        pending[0].stat().st_ino,
    ) == identity
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_preauthorization_aborted",
    ):
        _finalize_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )
    assert (
        pending[0].stat().st_dev,
        pending[0].stat().st_ino,
    ) == identity

    receipt = _abort_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )
    assert receipt["schema"] == rotation.RELEASE_ABORTED_RECEIPT_SCHEMA
    assert _temporary_aliases(aborted) == []


@pytest.mark.parametrize(
    "target_name",
    (
        rotation.TRANSACTION_FILE_NAME,
        rotation.PUBLICATION_FILE_NAME,
        rotation.RELEASE_UPDATE_PUBLICATION_FILE_NAME,
        rotation.PREDECESSOR_TRUST_FILE_NAME,
        package.STAGED_UNIT_INPUT_PLAN_PATH.name,
        package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
        package.FIXED_UNIT_INPUTS_PATH.name,
        rotation.PREPARED_RECEIPT_FILE_NAME,
    ),
)
def test_release_prepare_recovers_pending_temp_for_every_logical_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_name: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()
    transaction_name = (
        f"{trusted['authority_plan_sha256']}-"
        f"{documents['publication']['publication_sha256']}"
    )
    transaction = (
        cutover.EVIDENCE_ROOT
        / rotation.RELEASE_AUDIT_DIRECTORY_NAME
        / transaction_name
    )
    predecessor_names = {
        package.STAGED_UNIT_INPUT_PLAN_PATH.name,
        package.STAGED_UNIT_INPUT_APPROVAL_PATH.name,
        package.FIXED_UNIT_INPUTS_PATH.name,
    }
    target = (
        transaction / rotation.PREDECESSOR_DIRECTORY_NAME / target_name
        if target_name in predecessor_names
        else transaction / target_name
    )

    with monkeypatch.context() as crash_patch:
        _inject_install_power_loss(
            crash_patch,
            target=target,
            boundary="partial_write",
        )
        with pytest.raises(_SimulatedInstallPowerLoss):
            _prepare_release(documents, trusted, now=now)

    aliases = _temporary_aliases(target)
    assert len(aliases) == 1
    assert stat.S_IMODE(aliases[0].stat().st_mode) == 0o600
    recovered = _prepare_release(documents, trusted, now=now)

    assert recovered["schema"] == rotation.RELEASE_PREPARED_RECEIPT_SCHEMA
    assert target.exists()
    assert _temporary_aliases(target) == []
    assert _live_triplet() == before


@pytest.mark.parametrize(
    "adversary",
    (
        "hardlink",
        "symlink",
        "wrong_mode",
        "partial_final_mode",
        "malformed_suffix",
        "multiple_pending",
        "extended_metadata",
    ),
)
def test_release_pending_temp_adversarial_state_is_not_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    adversary: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    transaction = Path(prepared["audit_transaction_path"])
    target = transaction / rotation.MUTATION_BEGIN_FILE_NAME
    temporary = target.with_name(f".{target.name}.rotate.991")
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"sentinel")
    paths = [temporary]

    if adversary == "symlink":
        temporary.symlink_to(sentinel)
    elif adversary == "malformed_suffix":
        temporary = target.with_name(f".{target.name}.rotate.invalid")
        temporary.write_bytes(b"attacker")
        temporary.chmod(0o600)
        paths = [temporary]
    else:
        temporary.write_bytes(
            b"{" if adversary == "partial_final_mode" else b"attacker"
        )
        temporary.chmod(
            0o400
            if adversary == "partial_final_mode"
            else 0o200
            if adversary == "wrong_mode"
            else 0o600
        )
        if adversary == "hardlink":
            linked = tmp_path / "external-hardlink"
            os.link(temporary, linked)
            paths.append(linked)
        elif adversary == "multiple_pending":
            second = target.with_name(f".{target.name}.rotate.992")
            second.write_bytes(b"attacker-two")
            second.chmod(0o600)
            paths.append(second)
        elif adversary == "extended_metadata":

            def listxattr(path: Any, **_kwargs: Any) -> list[str]:
                return (
                    ["user.attacker"]
                    if Path(path) == temporary
                    else []
                )

            monkeypatch.setattr(
                rotation.os,
                "listxattr",
                listxattr,
                raising=False,
            )

    before = {path: path.lstat() for path in paths}
    with pytest.raises(
        rotation.UnitInputRotationError,
        match=r"unit_input_rotation_(audit_invalid|conflict)",
    ):
        _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now,
        )

    for path, identity in before.items():
        current = path.lstat()
        assert (current.st_dev, current.st_ino) == (
            identity.st_dev,
            identity.st_ino,
        )
    assert sentinel.read_bytes() == b"sentinel"
    assert not target.exists()


def test_release_out_of_order_pending_temp_is_not_pruned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    transaction = Path(prepared["audit_transaction_path"])
    activation = transaction / rotation.ACTIVATION_BEGIN_FILE_NAME
    pending = activation.with_name(f".{activation.name}.rotate.993")
    pending.write_bytes(b"out-of-order")
    pending.chmod(0o600)
    before = pending.lstat()

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now,
        )

    after = pending.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert not (
        transaction / rotation.MUTATION_BEGIN_FILE_NAME
    ).exists()


def test_install_exact_rolls_back_substituted_destination_inode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = (tmp_path / "secure").resolve()
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    target = directory / "receipt.json"
    payload = b'{"safe":true}'

    def substitute(
        source: Path,
        destination: Path,
        *,
        directory_fd: int,
        expected_identity: tuple[int, int],
    ) -> bool:
        original = os.stat(
            source.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        assert (original.st_dev, original.st_ino) == expected_identity
        os.unlink(source.name, dir_fd=directory_fd)
        descriptor = os.open(
            source.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            os.write(descriptor, payload)
            os.fchmod(descriptor, 0o400)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        substituted = os.stat(
            source.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        assert (
            substituted.st_dev,
            substituted.st_ino,
        ) != expected_identity
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        return True

    with monkeypatch.context() as swap_patch:
        swap_patch.setattr(rotation, "_rename_noreplace", substitute)
        with pytest.raises(
            rotation.UnitInputRotationError,
            match="unit_input_rotation_conflict",
        ):
            rotation._install_exact(
                target,
                payload,
                uid=os.geteuid(),  # windows-footgun: ok — POSIX-only inode ownership test
                gid=os.getegid(),  # windows-footgun: ok — POSIX-only inode ownership test
                mode=0o400,
            )

    assert not target.exists()
    assert _temporary_aliases(target) == []
    assert rotation._install_exact(
        target,
        payload,
        uid=os.geteuid(),  # windows-footgun: ok — POSIX-only inode ownership test
        gid=os.getegid(),  # windows-footgun: ok — POSIX-only inode ownership test
        mode=0o400,
    )
    assert target.read_bytes() == payload


def test_install_exact_resyncs_sealed_temp_before_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = (tmp_path / "secure").resolve()
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    target = directory / "receipt.json"
    temporary = target.with_name(f".{target.name}.rotate.994")
    payload = b'{"safe":true}'
    temporary.write_bytes(payload)
    temporary.chmod(0o400)
    temporary_identity = (temporary.stat().st_dev, temporary.stat().st_ino)
    fsync = rotation.os.fsync
    synced: set[tuple[int, int]] = set()

    def observe_fsync(descriptor: int) -> None:
        item = os.fstat(descriptor)
        if stat.S_ISREG(item.st_mode):
            synced.add((item.st_dev, item.st_ino))
        fsync(descriptor)

    def install(
        source: Path,
        destination: Path,
        *,
        directory_fd: int,
        expected_identity: tuple[int, int],
    ) -> bool:
        assert expected_identity == temporary_identity
        assert expected_identity in synced
        os.rename(
            source.name,
            destination.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        return True

    monkeypatch.setattr(rotation.os, "fsync", observe_fsync)
    monkeypatch.setattr(rotation, "_rename_noreplace", install)

    assert rotation._install_exact(
        target,
        payload,
        uid=os.geteuid(),  # windows-footgun: ok — POSIX-only inode ownership test
        gid=os.getegid(),  # windows-footgun: ok — POSIX-only inode ownership test
        mode=0o400,
    )
    assert target.read_bytes() == payload
    assert temporary_identity in synced


@pytest.mark.parametrize(
    "crash_stage",
    (
        "v4_transaction_authorized",
        "v4_successor_publication_archived",
        "v4_release_update_publication_archived",
        "v4_predecessor_trust_archived",
        "v4_predecessor_plan_archived",
        "v4_predecessor_approval_archived",
        "v4_predecessor_fixed_inputs_archived",
        "v4_prepared_receipt_published",
    ),
)
def test_release_prepare_recovers_every_durable_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_stage: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    before = _live_triplet()

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        _prepare_release(documents, trusted, now=now)
    assert _live_triplet() == before

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    prepared = _prepare_release(documents, trusted, now=now + 4_000)

    assert prepared["schema"] == rotation.RELEASE_PREPARED_RECEIPT_SCHEMA
    assert _live_triplet() == before


@pytest.mark.parametrize(
    "crash_stage",
    (
        "v4_live_activation_begun",
        "audit_prepared",
        "predecessor_fixed_inputs_removed",
        "predecessor_approval_removed",
        "predecessor_plan_removed",
        "successor_plan_staged",
        "successor_approval_staged",
        "successor_fixed_inputs_staged",
        "v4_final_receipt_published",
    ),
)
def test_release_finalize_recovers_every_mutation_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    crash_stage: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        _finalize_release(documents, trusted, prepared)

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    receipt = _finalize_release(documents, trusted, prepared)

    assert receipt["schema"] == rotation.RELEASE_FINALIZED_RECEIPT_SCHEMA
    assert package.FIXED_UNIT_INPUTS_PATH.read_bytes() == (
        _canonical(documents["fixed"]) + b"\n"
    )


def test_release_prepare_exact_replay_after_expiry_but_new_expired_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)

    replay = _prepare_release(documents, trusted, now=now + 4_000)

    assert replay == prepared

    other_root = tmp_path / "other"
    other_root.mkdir()
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        other_root,
        now=now,
    )
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        _prepare_release(documents, trusted, now=now + 4_000)


def test_release_prepare_rejects_conflicting_inflight_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, first = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    second = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=trusted,
        target_revision="c" * 40,
        now=now,
    )
    _prepare_release(first, trusted, now=now)

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_successor_conflict",
    ):
        _prepare_release(second, trusted, now=now)


@pytest.mark.parametrize("mixed_part", ("approval", "fixed", "unknown_plan"))
def test_release_predecessor_schema_is_authoritative_and_mixed_triplets_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mixed_part: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    if mixed_part == "approval":
        path = package.STAGED_UNIT_INPUT_APPROVAL_PATH
        replacement = _canonical(documents["approval"])
    elif mixed_part == "fixed":
        path = package.FIXED_UNIT_INPUTS_PATH
        replacement = _canonical(documents["fixed"]) + b"\n"
    else:
        path = package.STAGED_UNIT_INPUT_PLAN_PATH
        value = json.loads(path.read_bytes())
        value["schema"] = "unknown-unit-input-plan.v99"
        replacement = _canonical(value)
    path.unlink()
    path.write_bytes(replacement)
    path.chmod(
        package.FIXED_UNIT_INPUTS_MODE
        if path == package.FIXED_UNIT_INPUTS_PATH
        else 0o400
    )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_predecessor_invalid",
    ):
        _prepare_release(documents, trusted, now=now)


def test_release_receipt_rejects_update_or_trust_substitution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    other = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=trusted,
        target_revision="c" * 40,
        now=now,
    )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_prepared_receipt_invalid",
    ):
        rotation._finalize_prepared_release_unit_input_authority_rotation(
            documents["publication"],
            other["update_publication"],
            prepared,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            clock=lambda: now,
            lock_factory=nullcontext,
        )
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_prepared_receipt_invalid",
    ):
        rotation._finalize_prepared_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256="ff" * 32,
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            clock=lambda: now,
            lock_factory=nullcontext,
        )


def test_release_prepared_receipt_cannot_rewrite_predecessor_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    forged = json.loads(json.dumps(prepared))
    forged["predecessor"]["plan_sha256"] = "ff" * 32
    transaction_unsigned = {
        "schema": rotation.RELEASE_TRANSACTION_SCHEMA,
        "predecessor": forged["predecessor"],
        "predecessor_trust_sha256": forged[
            "predecessor_trust_sha256"
        ],
        "authorization_checked_at_unix": forged[
            "authorization_checked_at_unix"
        ],
        "successor": forged["successor"],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    forged["transaction_sha256"] = hashlib.sha256(
        _canonical(transaction_unsigned)
    ).hexdigest()
    receipt_unsigned = {
        key: item
        for key, item in forged.items()
        if key != "receipt_sha256"
    }
    forged["receipt_sha256"] = hashlib.sha256(
        _canonical(receipt_unsigned)
    ).hexdigest()

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_prepared_receipt_invalid",
    ):
        rotation.validate_release_prepared_rotation_receipt(
            forged,
            unit_input_publication=documents["publication"],
            release_update_publication=documents["update_publication"],
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
        )


def test_release_finalize_rejects_persisted_publication_tamper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    path = (
        Path(prepared["audit_transaction_path"])
        / rotation.RELEASE_UPDATE_PUBLICATION_FILE_NAME
    )
    path.unlink()
    path.write_bytes(b"{}")
    path.chmod(0o400)

    with pytest.raises(rotation.UnitInputRotationError):
        _finalize_release(documents, trusted, prepared)


def test_release_public_root_api_has_no_clock_lock_or_identity_override() -> None:
    apis = (
        rotation.prepare_release_unit_input_authority_rotation,
        rotation.preauthorize_prepared_release_unit_input_authority_rotation,
        rotation.finalize_preauthorized_release_unit_input_authority_rotation,
        rotation.abort_preauthorized_release_unit_input_authority_rotation,
        rotation.finalize_prepared_release_unit_input_authority_rotation,
    )

    for api in apis:
        parameters = inspect.signature(api).parameters
        assert "now_unix" not in parameters
        assert "clock" not in parameters
        assert "lock_factory" not in parameters
        assert "require_root" not in parameters


def test_release_phase_dispatcher_binds_exact_request_before_root_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    receipt = {
        "transaction_sha256": "b" * 64,
        "audit_transaction_path": "/var/lib/exact-audit",
        "receipt_sha256": "c" * 64,
    }

    def prepare(
        unit_publication: dict[str, Any],
        update_publication: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed.append(
            (
                {
                    "unit": unit_publication,
                    "update": update_publication,
                },
                kwargs,
            )
        )
        return receipt

    monkeypatch.setattr(
        rotation,
        "prepare_release_unit_input_authority_rotation",
        prepare,
    )
    monkeypatch.setattr(
        rotation,
        "validate_release_prepared_rotation_receipt",
        lambda value, **_kwargs: dict(value),
    )
    request: dict[str, Any] = {
        "schema": rotation.RELEASE_PHASE_REQUEST_SCHEMA,
        "action": "prepare-release-unit-inputs",
        "owner_release_revision": "a" * 40,
        "remote_stager_revision": "b" * 40,
        "unit_input_publication": {"release_revision": "b" * 40},
        "release_update_publication": {"release_revision": "b" * 40},
        "trusted_predecessor": {"trust_sha256": "d" * 64},
        "expected_predecessor_trust_sha256": "d" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    request["request_sha256"] = rotation._sha(rotation._canonical(request))

    result = rotation.execute_release_unit_input_phase(
        "prepare-release-unit-inputs",
        request,
    )

    assert len(observed) == 1
    assert result["request_sha256"] == request["request_sha256"]
    assert result["owner_release_revision"] == "a" * 40
    assert result["remote_stager_revision"] == "b" * 40
    assert result["canonical_receipt"] == receipt
    assert result["canonical_receipt_sha256"] == "c" * 64
    assert result["activation_begin"] is None
    unsigned = {
        key: value
        for key, value in result.items()
        if key != "result_sha256"
    }
    assert result["result_sha256"] == rotation._sha(
        rotation._canonical(unsigned)
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("action", "abort-release-unit-inputs"),
        ("owner_release_revision", "c" * 40),
        ("remote_stager_revision", "d" * 40),
    ),
)
def test_release_phase_dispatcher_rejects_request_tamper_before_root_call(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
) -> None:
    called = False

    def prepare(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        rotation,
        "prepare_release_unit_input_authority_rotation",
        prepare,
    )
    request: dict[str, Any] = {
        "schema": rotation.RELEASE_PHASE_REQUEST_SCHEMA,
        "action": "prepare-release-unit-inputs",
        "owner_release_revision": "a" * 40,
        "remote_stager_revision": "b" * 40,
        "unit_input_publication": {},
        "release_update_publication": {},
        "trusted_predecessor": {},
        "expected_predecessor_trust_sha256": "d" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    request["request_sha256"] = rotation._sha(rotation._canonical(request))
    request[field] = replacement

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_phase_request_invalid",
    ):
        rotation.execute_release_unit_input_phase(
            "prepare-release-unit-inputs",
            request,
        )

    assert called is False


def test_release_phase_dispatcher_rejects_rehashed_publication_revision_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def prepare(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        rotation,
        "prepare_release_unit_input_authority_rotation",
        prepare,
    )
    request: dict[str, Any] = {
        "schema": rotation.RELEASE_PHASE_REQUEST_SCHEMA,
        "action": "prepare-release-unit-inputs",
        "owner_release_revision": "a" * 40,
        "remote_stager_revision": "b" * 40,
        "unit_input_publication": {"release_revision": "c" * 40},
        "release_update_publication": {"release_revision": "b" * 40},
        "trusted_predecessor": {},
        "expected_predecessor_trust_sha256": "d" * 64,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    request["request_sha256"] = rotation._sha(rotation._canonical(request))

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_phase_request_invalid",
    ):
        rotation.execute_release_unit_input_phase(
            "prepare-release-unit-inputs",
            request,
        )

    assert called is False


def test_release_public_root_api_fixes_clock_lock_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, dict[str, Any]]] = []

    def prepare(*_args: Any, **kwargs: Any) -> dict[str, bool]:
        observed.append(("prepare", kwargs))
        return {"ok": True}

    def preauthorize(*_args: Any, **kwargs: Any) -> dict[str, bool]:
        observed.append(("preauthorize", kwargs))
        return {"ok": True}

    def finalize_preauthorized(
        *_args: Any,
        **kwargs: Any,
    ) -> dict[str, bool]:
        observed.append(("finalize_preauthorized", kwargs))
        return {"ok": True}

    def abort(*_args: Any, **kwargs: Any) -> dict[str, bool]:
        observed.append(("abort", kwargs))
        return {"ok": True}

    def finalize(*_args: Any, **kwargs: Any) -> dict[str, bool]:
        observed.append(("finalize", kwargs))
        return {"ok": True}

    monkeypatch.setattr(
        rotation,
        "_prepare_release_unit_input_authority_rotation",
        prepare,
    )
    rotation.prepare_release_unit_input_authority_rotation(
        {},
        {},
        trusted_predecessor={},
        expected_predecessor_trust_sha256="a" * 64,
    )
    monkeypatch.setattr(
        rotation,
        "_preauthorize_prepared_release_unit_input_authority_rotation",
        preauthorize,
    )
    rotation.preauthorize_prepared_release_unit_input_authority_rotation(
        {},
        {},
        {},
        trusted_predecessor={},
        expected_predecessor_trust_sha256="a" * 64,
        expected_transaction_sha256="b" * 64,
    )
    monkeypatch.setattr(
        rotation,
        "_finalize_preauthorized_release_unit_input_authority_rotation",
        finalize_preauthorized,
    )
    rotation.finalize_preauthorized_release_unit_input_authority_rotation(
        {},
        {},
        {},
        {},
        trusted_predecessor={},
        expected_predecessor_trust_sha256="a" * 64,
        expected_transaction_sha256="b" * 64,
    )
    monkeypatch.setattr(
        rotation,
        "_abort_preauthorized_release_unit_input_authority_rotation",
        abort,
    )
    rotation.abort_preauthorized_release_unit_input_authority_rotation(
        {},
        {},
        {},
        {},
        trusted_predecessor={},
        expected_predecessor_trust_sha256="a" * 64,
        expected_transaction_sha256="b" * 64,
    )
    monkeypatch.setattr(
        rotation,
        "_finalize_prepared_release_unit_input_authority_rotation",
        finalize,
    )
    rotation.finalize_prepared_release_unit_input_authority_rotation(
        {},
        {},
        {},
        trusted_predecessor={},
        expected_predecessor_trust_sha256="a" * 64,
        expected_transaction_sha256="b" * 64,
    )

    assert len(observed) == 5
    for name, kwargs in observed:
        assert kwargs["require_root"] is True
        assert kwargs["lock_factory"] is None
        if name in {"prepare", "preauthorize", "finalize"}:
            assert kwargs["clock"] is rotation._production_clock
        else:
            assert "clock" not in kwargs


def test_release_expired_prepared_transaction_cannot_begin_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        _finalize_release(
            documents,
            trusted,
            prepared,
            now=now + 4_000,
        )

    transaction = Path(prepared["audit_transaction_path"])
    assert not (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()
    assert not (transaction / rotation.RECEIPT_FILE_NAME).exists()
    assert _live_triplet() == before


def test_release_preauthorization_checks_freshness_under_canonical_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()
    observed = [now]

    class ExpireWhileWaiting:
        def __enter__(self):
            observed[0] = now + 4_000

        def __exit__(self, *_args: Any) -> bool:
            return False

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        rotation._preauthorize_prepared_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            clock=lambda: observed[0],
            lock_factory=ExpireWhileWaiting,
        )

    transaction = Path(prepared["audit_transaction_path"])
    assert not (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()
    assert _live_triplet() == before


def test_release_backward_clock_cannot_poison_preauthorization_replay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    transaction = Path(prepared["audit_transaction_path"])
    before_live = _live_triplet()
    before_inventory = {
        path.name: path.read_bytes()
        for path in transaction.iterdir()
        if path.is_file()
    }

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_clock_invalid",
    ):
        _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now - 1,
        )

    assert {
        path.name: path.read_bytes()
        for path in transaction.iterdir()
        if path.is_file()
    } == before_inventory
    assert not (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()
    assert not any(
        path.name.startswith(
            f".{rotation.MUTATION_BEGIN_FILE_NAME}.rotate."
        )
        for path in transaction.iterdir()
    )
    assert _live_triplet() == before_live

    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )
    receipt = _finalize_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )
    assert receipt["mutation_begin_sha256"] == preauthorization[
        "mutation_begin_sha256"
    ]


def test_release_expired_after_preauthorization_finalizes_without_clock_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()
    transaction = Path(prepared["audit_transaction_path"])

    def crash(stage: str) -> None:
        if stage == "v4_live_mutation_begun":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now,
        )
    preauthorization = json.loads(
        (transaction / rotation.MUTATION_BEGIN_FILE_NAME).read_bytes()
    )
    assert _live_triplet() == before
    assert not (transaction / rotation.RECEIPT_FILE_NAME).exists()
    assert not (transaction / rotation.ACTIVATION_BEGIN_FILE_NAME).exists()
    assert preauthorization["freshness_checked_at_unix"] == now
    assert (
        transaction / rotation.MUTATION_BEGIN_FILE_NAME
    ).read_bytes() == _canonical(preauthorization)
    assert rotation.validate_release_preauthorization_receipt(
        preauthorization,
        unit_input_publication=documents["publication"],
        release_update_publication=documents["update_publication"],
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=trusted["trust_sha256"],
        prepared_receipt=prepared,
    ) == preauthorization

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    replay = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now + 4_000,
    )
    receipt = _finalize_preauthorized_release(
        documents,
        trusted,
        prepared,
        replay,
    )

    assert replay == preauthorization
    assert receipt["mutation_begin_sha256"] == preauthorization[
        "mutation_begin_sha256"
    ]
    assert (transaction / rotation.ACTIVATION_BEGIN_FILE_NAME).exists()
    assert package.FIXED_UNIT_INPUTS_PATH.read_bytes() == (
        _canonical(documents["fixed"]) + b"\n"
    )


def test_release_unpreauthorized_finalize_fails_without_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()
    transaction = rotation._release_receipt_transaction(prepared)
    successor = rotation._release_successor(
        documents["publication"],
        documents["update_publication"],
        trusted,
        expected_predecessor_trust_sha256=trusted["trust_sha256"],
        now_unix=now,
    )
    unpersisted = rotation._release_mutation_begin_value(
        transaction,
        successor,
        freshness_checked_at_unix=now,
    )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_finalize_authorization_invalid",
    ):
        rotation._finalize_preauthorized_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            unpersisted,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            lock_factory=nullcontext,
        )

    transaction = Path(prepared["audit_transaction_path"])
    assert not (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()
    assert not (transaction / rotation.RECEIPT_FILE_NAME).exists()
    assert _live_triplet() == before


def test_release_expired_replay_allowed_after_actual_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)

    def crash(stage: str) -> None:
        if stage == "predecessor_fixed_inputs_removed":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        _finalize_release(documents, trusted, prepared, now=now)
    transaction = Path(prepared["audit_transaction_path"])
    mutation = json.loads(
        (transaction / rotation.MUTATION_BEGIN_FILE_NAME).read_bytes()
    )

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    receipt = _finalize_release(
        documents,
        trusted,
        prepared,
        now=now + 4_000,
    )
    assert receipt["mutation_begin_sha256"] == mutation[
        "mutation_begin_sha256"
    ]
    assert package.FIXED_UNIT_INPUTS_PATH.read_bytes() == (
        _canonical(documents["fixed"]) + b"\n"
    )


def test_release_preauthorization_abort_is_terminal_exact_and_replayable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()
    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )

    def crash(stage: str) -> None:
        if stage == "v4_preauthorization_aborted":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        _abort_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )
    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    aborted = _abort_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )
    replay = _abort_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )

    transaction = Path(prepared["audit_transaction_path"])
    assert replay == aborted
    assert aborted["schema"] == rotation.RELEASE_ABORTED_RECEIPT_SCHEMA
    assert aborted["live_predecessor_unchanged"] is True
    assert aborted["live_mutation_performed"] is False
    assert (
        transaction / rotation.ABORT_RECEIPT_FILE_NAME
    ).read_bytes() == _canonical(aborted)
    assert rotation.validate_release_rotation_abort_receipt(
        aborted,
        unit_input_publication=documents["publication"],
        release_update_publication=documents["update_publication"],
        trusted_predecessor=trusted,
        expected_predecessor_trust_sha256=trusted["trust_sha256"],
        prepared_receipt=prepared,
        preauthorization_receipt=preauthorization,
    ) == aborted
    assert _live_triplet() == before
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_preauthorization_aborted",
    ):
        _finalize_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )


def test_release_same_successor_retry_after_abort_remains_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )
    aborted = _abort_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )
    root = (
        cutover.EVIDENCE_ROOT / rotation.RELEASE_AUDIT_DIRECTORY_NAME
    )
    before_directories = sorted(path.name for path in root.iterdir())

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_preauthorization_aborted",
    ):
        _preauthorize_release(
            documents,
            trusted,
            prepared,
            now=now + 4_000,
        )
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_successor_conflict",
    ):
        _prepare_release(documents, trusted, now=now)

    assert sorted(path.name for path in root.iterdir()) == before_directories
    assert (
        Path(prepared["audit_transaction_path"])
        / rotation.ABORT_RECEIPT_FILE_NAME
    ).read_bytes() == _canonical(aborted)


@pytest.mark.parametrize("state", ("partial_live_mutation", "finalized"))
def test_release_preauthorization_cannot_abort_after_live_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )
    if state == "partial_live_mutation":

        def crash(stage: str) -> None:
            if stage == "predecessor_fixed_inputs_removed":
                raise KeyboardInterrupt

        monkeypatch.setattr(rotation, "_checkpoint", crash)
        with pytest.raises(KeyboardInterrupt):
            _finalize_preauthorized_release(
                documents,
                trusted,
                prepared,
                preauthorization,
            )
        monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    else:
        _finalize_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_abort_authorization_invalid",
    ):
        _abort_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )

    transaction = Path(prepared["audit_transaction_path"])
    assert not (transaction / rotation.ABORT_RECEIPT_FILE_NAME).exists()


def test_release_activation_marker_survives_runtime_error_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()
    preauthorization = _preauthorize_release(
        documents,
        trusted,
        prepared,
        now=now,
    )

    def fail_after_first_live_write(stage: str) -> None:
        if stage == "predecessor_fixed_inputs_removed":
            raise RuntimeError("synthetic activation failure")

    monkeypatch.setattr(
        rotation,
        "_checkpoint",
        fail_after_first_live_write,
    )
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_failed",
    ):
        _finalize_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )

    transaction = Path(prepared["audit_transaction_path"])
    activation = json.loads(
        (transaction / rotation.ACTIVATION_BEGIN_FILE_NAME).read_bytes()
    )
    assert activation["live_activation_write_ahead_committed"] is True
    assert _live_triplet() == before
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_abort_authorization_invalid",
    ):
        _abort_preauthorized_release(
            documents,
            trusted,
            prepared,
            preauthorization,
        )
    assert not (transaction / rotation.ABORT_RECEIPT_FILE_NAME).exists()

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
    receipt = _finalize_preauthorized_release(
        documents,
        trusted,
        prepared,
        preauthorization,
    )
    assert receipt["activation_begin_sha256"] == activation[
        "activation_begin_sha256"
    ]


def test_release_aborted_history_allows_different_prepared_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = 1_900_000_000
    private, _predecessor, trusted, first = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    second = _release_documents(
        monkeypatch,
        private_key=private,
        trusted_predecessor=trusted,
        target_revision="c" * 40,
        now=now,
    )
    first_prepared = _prepare_release(first, trusted, now=now)
    first_preauthorization = _preauthorize_release(
        first,
        trusted,
        first_prepared,
        now=now,
    )
    first_abort = _abort_preauthorized_release(
        first,
        trusted,
        first_prepared,
        first_preauthorization,
    )

    second_prepared = _prepare_release(second, trusted, now=now)
    second_preauthorization = _preauthorize_release(
        second,
        trusted,
        second_prepared,
        now=now,
    )
    second_final = _finalize_preauthorized_release(
        second,
        trusted,
        second_prepared,
        second_preauthorization,
    )
    historical_replay = _abort_preauthorized_release(
        first,
        trusted,
        first_prepared,
        first_preauthorization,
    )

    assert first_prepared["transaction_sha256"] != second_prepared[
        "transaction_sha256"
    ]
    assert historical_replay == first_abort
    assert Path(
        first_prepared["audit_transaction_path"]
    ).joinpath(rotation.ABORT_RECEIPT_FILE_NAME).read_bytes() == _canonical(
        first_abort
    )
    assert second_final["successor"]["revision"] == "c" * 40


@pytest.mark.parametrize("location", ("transaction", "predecessor"))
def test_release_closed_world_inventory_rejects_extra_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    location: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    before = _live_triplet()
    transaction = Path(prepared["audit_transaction_path"])
    root = (
        transaction
        if location == "transaction"
        else transaction / rotation.PREDECESSOR_DIRECTORY_NAME
    )
    unexpected = root / "unexpected-evidence.json"
    unexpected.write_bytes(b"{}")
    unexpected.chmod(0o400)

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        _finalize_release(documents, trusted, prepared, now=now)

    assert _live_triplet() == before
    assert not (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()


@pytest.mark.parametrize(
    "attribute",
    ("user.untrusted", "system.posix_acl_access", "security.capability"),
)
def test_release_production_evidence_rejects_xattrs_acls_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attribute: str,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    monkeypatch.setattr(rotation.sys, "platform", "linux")
    monkeypatch.setattr(
        rotation.os,
        "listxattr",
        lambda _path, **_kwargs: [attribute],
        raising=False,
    )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        rotation._release_require_no_extended_metadata(
            evidence,
            uid=0,
            gid=0,
        )


@pytest.mark.parametrize(
    "target_name",
    ("staged_directory", "plan", "approval", "fixed"),
)
@pytest.mark.parametrize(
    "attribute",
    ("user.untrusted", "system.posix_acl_access", "security.capability"),
)
def test_release_prepare_path_rejects_live_extended_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_name: str,
    attribute: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    targets = {
        "staged_directory": package.STAGED_UNIT_INPUT_PLAN_PATH.parent,
        "plan": package.STAGED_UNIT_INPUT_PLAN_PATH,
        "approval": package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        "fixed": package.FIXED_UNIT_INPUTS_PATH,
    }
    target = targets[target_name]
    before = _live_triplet()
    monkeypatch.setattr(
        rotation,
        "_release_extended_metadata_required",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        rotation.os,
        "listxattr",
        lambda path, **_kwargs: (
            [attribute] if Path(path) == target else []
        ),
        raising=False,
    )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        _prepare_release(documents, trusted, now=now)

    assert _live_triplet() == before
    assert not (
        cutover.EVIDENCE_ROOT / rotation.RELEASE_AUDIT_DIRECTORY_NAME
    ).exists()


@pytest.mark.parametrize(
    "target_name",
    ("staged_directory", "plan", "approval", "fixed"),
)
@pytest.mark.parametrize(
    "attribute",
    ("user.untrusted", "system.posix_acl_access", "security.capability"),
)
def test_release_finalize_path_rejects_live_extended_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_name: str,
    attribute: str,
) -> None:
    now = 1_900_000_000
    _private, _predecessor, trusted, documents = _release_rotation_state(
        monkeypatch,
        tmp_path,
        now=now,
    )
    prepared = _prepare_release(documents, trusted, now=now)
    targets = {
        "staged_directory": package.STAGED_UNIT_INPUT_PLAN_PATH.parent,
        "plan": package.STAGED_UNIT_INPUT_PLAN_PATH,
        "approval": package.STAGED_UNIT_INPUT_APPROVAL_PATH,
        "fixed": package.FIXED_UNIT_INPUTS_PATH,
    }
    target = targets[target_name]
    before = _live_triplet()
    monkeypatch.setattr(
        rotation,
        "_release_extended_metadata_required",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        rotation.os,
        "listxattr",
        lambda path, **_kwargs: (
            [attribute] if Path(path) == target else []
        ),
        raising=False,
    )

    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_audit_invalid",
    ):
        _finalize_release(documents, trusted, prepared, now=now)

    transaction = Path(prepared["audit_transaction_path"])
    assert _live_triplet() == before
    assert not (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()
