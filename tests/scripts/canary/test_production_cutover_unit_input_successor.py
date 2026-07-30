from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.canary import package_production_cutover_artifacts as package
from scripts.canary import production_cutover_owner_launcher as owner
from scripts.canary import production_cutover_unit_input_successor as successor
from tests.scripts.canary.test_production_cutover_owner_launcher import (
    _canonical,
    _unit_input_authority,
)


PREDECESSOR_REVISION = "a" * 40
SUCCESSOR_REVISION = "b" * 40
HISTORICAL_ISSUED_AT = 1_700_000_000


def _authority_triplet() -> tuple[dict, dict, dict]:
    plan, approval, _publication = _unit_input_authority(
        PREDECESSOR_REVISION,
        HISTORICAL_ISSUED_AT,
    )
    fixed_inputs = package._unit_inputs_from_authority(plan, approval)
    return dict(plan), dict(approval), dict(fixed_inputs)


def _derive(
    plan: dict,
    approval: dict,
    fixed_inputs: dict,
) -> dict:
    return dict(
        successor.derive_successor_payload(
            predecessor_plan=plan,
            predecessor_approval=approval,
            predecessor_fixed_inputs=fixed_inputs,
            successor_revision=SUCCESSOR_REVISION,
        )
    )


def test_derives_successor_from_the_exact_predecessor_authority_triplet() -> None:
    plan, approval, fixed_inputs = _authority_triplet()

    result = _derive(plan, approval, fixed_inputs)

    expected = copy.deepcopy(plan["unit_inputs"])
    expected["discord_reconciliation_intent"]["release_revision"] = SUCCESSOR_REVISION
    assert result == expected
    assert result["schema"] == package.UNIT_INPUT_PAYLOAD_SCHEMA
    assert (
        result["discord_reconciliation_intent"]["release_revision"]
        == SUCCESSOR_REVISION
    )
    assert (
        plan["unit_inputs"]["discord_reconciliation_intent"]["release_revision"]
        == PREDECESSOR_REVISION
    )
    assert (
        successor.validate_successor_payload(
            result,
            predecessor_plan=plan,
            predecessor_approval=approval,
            predecessor_fixed_inputs=fixed_inputs,
            successor_revision=SUCCESSOR_REVISION,
        )
        == result
    )


def test_rejects_a_tampered_predecessor_approval_after_self_hash_repair() -> None:
    plan, approval, fixed_inputs = _authority_triplet()
    changed = copy.deepcopy(approval)
    changed["nonce_sha256"] = "c" * 64
    changed["approval_sha256"] = hashlib.sha256(
        _canonical({
            name: item for name, item in changed.items() if name != "approval_sha256"
        })
    ).hexdigest()

    with pytest.raises(
        successor.SuccessorUnitInputError,
        match="unit_input_successor_predecessor_invalid",
    ):
        _derive(plan, changed, fixed_inputs)


def test_rejects_fixed_inputs_not_derived_from_the_predecessor_authority() -> None:
    plan, approval, fixed_inputs = _authority_triplet()
    changed = copy.deepcopy(fixed_inputs)
    changed["authority_plan_sha256"] = "c" * 64

    with pytest.raises(
        successor.SuccessorUnitInputError,
        match="unit_input_successor_predecessor_invalid",
    ):
        _derive(plan, approval, changed)


@pytest.mark.parametrize(
    "revision",
    [
        PREDECESSOR_REVISION,
        "not-a-revision",
        PREDECESSOR_REVISION[:12] + "b" * 28,
        None,
    ],
)
def test_rejects_an_invalid_or_ambiguous_successor_revision(
    revision: object,
) -> None:
    plan, approval, fixed_inputs = _authority_triplet()

    with pytest.raises(
        successor.SuccessorUnitInputError,
        match="unit_input_successor_revision_invalid",
    ):
        successor.derive_successor_payload(
            predecessor_plan=plan,
            predecessor_approval=approval,
            predecessor_fixed_inputs=fixed_inputs,
            successor_revision=revision,
        )


def test_validator_rejects_any_drift_beyond_the_revision_binding() -> None:
    plan, approval, fixed_inputs = _authority_triplet()
    changed = _derive(plan, approval, fixed_inputs)
    changed["target"]["sql_instance"] = "different-production-pg18"
    package._unit_input_payload(changed)

    with pytest.raises(
        successor.SuccessorUnitInputError,
        match="unit_input_successor_payload_invalid",
    ):
        successor.validate_successor_payload(
            changed,
            predecessor_plan=plan,
            predecessor_approval=approval,
            predecessor_fixed_inputs=fixed_inputs,
            successor_revision=SUCCESSOR_REVISION,
        )


def test_owner_cli_derives_canonical_payload_without_mutating_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, approval, fixed_inputs = _authority_triplet()
    plan_path = (tmp_path / "predecessor-plan.json").resolve()
    approval_path = (tmp_path / "predecessor-approval.json").resolve()
    fixed_path = (tmp_path / "predecessor-fixed-inputs.json").resolve()
    output_path = (tmp_path / "successor-unit-inputs.json").resolve()
    plan_path.write_bytes(_canonical(plan))
    approval_path.write_bytes(_canonical(approval))
    fixed_path.write_bytes(_canonical(fixed_inputs) + b"\n")
    original_inputs = {
        path: path.read_bytes() for path in (plan_path, approval_path, fixed_path)
    }
    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: {"revision": revision},
    )

    assert (
        owner.main([
            "derive-unit-inputs",
            "--revision",
            SUCCESSOR_REVISION,
            "--predecessor-plan",
            str(plan_path),
            "--predecessor-approval",
            str(approval_path),
            "--predecessor-fixed-inputs",
            str(fixed_path),
            "--output",
            str(output_path),
        ])
        == 0
    )

    result = json.loads(output_path.read_bytes())
    assert result == _derive(plan, approval, fixed_inputs)
    assert output_path.read_bytes() == _canonical(result)
    assert {
        path: path.read_bytes() for path in (plan_path, approval_path, fixed_path)
    } == original_inputs


def test_owner_cli_requires_the_fixed_input_file_trailing_newline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plan, approval, fixed_inputs = _authority_triplet()
    plan_path = (tmp_path / "predecessor-plan.json").resolve()
    approval_path = (tmp_path / "predecessor-approval.json").resolve()
    fixed_path = (tmp_path / "predecessor-fixed-inputs.json").resolve()
    output_path = (tmp_path / "successor-unit-inputs.json").resolve()
    plan_path.write_bytes(_canonical(plan))
    approval_path.write_bytes(_canonical(approval))
    fixed_path.write_bytes(_canonical(fixed_inputs))
    monkeypatch.setattr(
        owner,
        "_active_owner_runtime_attestation",
        lambda revision: {"revision": revision},
    )

    assert (
        owner.main([
            "derive-unit-inputs",
            "--revision",
            SUCCESSOR_REVISION,
            "--predecessor-plan",
            str(plan_path),
            "--predecessor-approval",
            str(approval_path),
            "--predecessor-fixed-inputs",
            str(fixed_path),
            "--output",
            str(output_path),
        ])
        == 2
    )
    assert not output_path.exists()
