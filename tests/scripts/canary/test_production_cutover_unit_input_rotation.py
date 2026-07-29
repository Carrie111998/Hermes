from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
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
    os.chown(evidence, os.geteuid(), os.getegid())
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
        assert observed.st_uid == os.geteuid()
        assert observed.st_gid == os.getegid()
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
    ) == receipt


@pytest.mark.parametrize(
    "forged_link",
    ("prepared_receipt_sha256", "mutation_begin_sha256"),
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
    prepare = inspect.signature(
        rotation.prepare_release_unit_input_authority_rotation
    ).parameters
    finalize = inspect.signature(
        rotation.finalize_prepared_release_unit_input_authority_rotation
    ).parameters

    for parameters in (prepare, finalize):
        assert "now_unix" not in parameters
        assert "clock" not in parameters
        assert "lock_factory" not in parameters
        assert "require_root" not in parameters


def test_release_public_root_api_fixes_clock_lock_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []

    def prepare(*_args: Any, **kwargs: Any) -> dict[str, bool]:
        observed.append(kwargs)
        return {"ok": True}

    def finalize(*_args: Any, **kwargs: Any) -> dict[str, bool]:
        observed.append(kwargs)
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

    assert len(observed) == 2
    for kwargs in observed:
        assert kwargs["require_root"] is True
        assert kwargs["clock"] is rotation._production_clock
        assert kwargs["lock_factory"] is None


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


def test_release_expired_replay_after_marker_but_before_live_mutation_fails(
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
        if stage == "v4_live_mutation_begun":
            raise KeyboardInterrupt

    monkeypatch.setattr(rotation, "_checkpoint", crash)
    with pytest.raises(KeyboardInterrupt):
        _finalize_release(documents, trusted, prepared, now=now)
    transaction = Path(prepared["audit_transaction_path"])
    mutation = json.loads(
        (transaction / rotation.MUTATION_BEGIN_FILE_NAME).read_bytes()
    )
    assert mutation["live_mutation_write_ahead_committed"] is True
    assert mutation["freshness_checked_at_unix"] == now

    monkeypatch.setattr(rotation, "_checkpoint", lambda _stage: None)
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
    assert _live_triplet() == before
    assert not (transaction / rotation.RECEIPT_FILE_NAME).exists()


def test_release_rechecks_freshness_after_marker_before_first_live_mutation(
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

    def advance_after_marker(stage: str) -> None:
        if stage == "v4_live_mutation_begun":
            observed[0] = now + 4_000

    monkeypatch.setattr(rotation, "_checkpoint", advance_after_marker)
    with pytest.raises(
        rotation.UnitInputRotationError,
        match="unit_input_rotation_publication_expired",
    ):
        rotation._finalize_prepared_release_unit_input_authority_rotation(
            documents["publication"],
            documents["update_publication"],
            prepared,
            trusted_predecessor=trusted,
            expected_predecessor_trust_sha256=trusted["trust_sha256"],
            expected_transaction_sha256=prepared["transaction_sha256"],
            require_root=False,
            clock=lambda: observed[0],
            lock_factory=nullcontext,
        )

    transaction = Path(prepared["audit_transaction_path"])
    assert (transaction / rotation.MUTATION_BEGIN_FILE_NAME).exists()
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
