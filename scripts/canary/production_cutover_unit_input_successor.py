#!/usr/bin/env python3
"""Derive one successor unit-input payload from pinned predecessor authority.

The predecessor plan, approval, and fixed-input document form one exact
cryptographic triplet.  This module validates that triplet without reviving
the predecessor approval lease, then changes only the release binding inside
the Discord reconciliation intent.  It performs no filesystem or production
mutation.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from scripts.canary import package_production_cutover_artifacts as package


class SuccessorUnitInputError(RuntimeError):
    """Stable, secret-free successor unit-input failure."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SuccessorUnitInputError(
            "unit_input_successor_predecessor_invalid"
        ) from exc


def _validated_predecessor(
    *,
    predecessor_plan: Mapping[str, Any],
    predecessor_approval: Mapping[str, Any],
    predecessor_fixed_inputs: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    try:
        plan = package.validate_unit_input_plan(predecessor_plan)
        issued_at = predecessor_approval.get("issued_at_unix")
        if type(issued_at) is not int:
            raise ValueError("predecessor approval issue time invalid")
        approval = package.validate_unit_input_approval(
            predecessor_approval,
            plan=plan,
            now_unix=issued_at,
        )
        package._unit_inputs(
            predecessor_fixed_inputs,
            revision=str(plan["release_revision"]),
        )
        expected_fixed_inputs = package._unit_inputs_from_authority(
            plan,
            approval,
        )
        if _canonical(predecessor_fixed_inputs) != _canonical(expected_fixed_inputs):
            raise ValueError("predecessor fixed inputs do not match authority")
    except SuccessorUnitInputError:
        raise
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        package.PackagingError,
    ) as exc:
        raise SuccessorUnitInputError(
            "unit_input_successor_predecessor_invalid"
        ) from exc
    return plan, approval


def _expected_successor_payload(
    plan: Mapping[str, Any],
    *,
    successor_revision: str,
) -> Mapping[str, Any]:
    predecessor_revision = str(plan["release_revision"])
    if (
        not isinstance(successor_revision, str)
        or package.REVISION.fullmatch(successor_revision) is None
        or successor_revision == predecessor_revision
        or successor_revision[:12] == predecessor_revision[:12]
    ):
        raise SuccessorUnitInputError("unit_input_successor_revision_invalid")
    try:
        expected = copy.deepcopy(dict(plan["unit_inputs"]))
        reconciliation_intent = expected["discord_reconciliation_intent"]
        if not isinstance(reconciliation_intent, dict):
            raise TypeError("reconciliation intent is not mutable")
        reconciliation_intent["release_revision"] = successor_revision
        return package._unit_input_payload(expected)
    except (KeyError, TypeError, ValueError, package.PackagingError) as exc:
        raise SuccessorUnitInputError(
            "unit_input_successor_predecessor_invalid"
        ) from exc


def derive_successor_payload(
    *,
    predecessor_plan: Mapping[str, Any],
    predecessor_approval: Mapping[str, Any],
    predecessor_fixed_inputs: Mapping[str, Any],
    successor_revision: str,
) -> Mapping[str, Any]:
    """Return the deterministic successor payload for one exact predecessor."""

    plan, _approval = _validated_predecessor(
        predecessor_plan=predecessor_plan,
        predecessor_approval=predecessor_approval,
        predecessor_fixed_inputs=predecessor_fixed_inputs,
    )
    return copy.deepcopy(
        _expected_successor_payload(
            plan,
            successor_revision=successor_revision,
        )
    )


def validate_successor_payload(
    value: Any,
    *,
    predecessor_plan: Mapping[str, Any],
    predecessor_approval: Mapping[str, Any],
    predecessor_fixed_inputs: Mapping[str, Any],
    successor_revision: str,
) -> Mapping[str, Any]:
    """Require a payload to equal the deterministic successor byte-for-byte."""

    plan, _approval = _validated_predecessor(
        predecessor_plan=predecessor_plan,
        predecessor_approval=predecessor_approval,
        predecessor_fixed_inputs=predecessor_fixed_inputs,
    )
    expected = _expected_successor_payload(
        plan,
        successor_revision=successor_revision,
    )
    try:
        candidate = package._unit_input_payload(value)
        if _canonical(candidate) != _canonical(expected):
            raise ValueError("successor payload differs from exact derivation")
    except SuccessorUnitInputError:
        raise
    except (TypeError, ValueError, package.PackagingError) as exc:
        raise SuccessorUnitInputError("unit_input_successor_payload_invalid") from exc
    return copy.deepcopy(candidate)
