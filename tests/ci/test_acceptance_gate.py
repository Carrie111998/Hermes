"""Tests for the deterministic acceptance-criteria gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "acceptance_gate.py"
_spec = importlib.util.spec_from_file_location("acceptance_gate", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load acceptance_gate.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
canonical_hash = _mod.canonical_hash
freeze = _mod.freeze
verify = _mod.verify


def _criteria():
    return [
        {"id": "AC-1", "text": "The targeted command succeeds."},
        {"id": "AC-2", "text": "The result is bound to the frozen SHA."},
    ]


def test_freeze_is_deterministic_and_order_sensitive(tmp_path: Path):
    path = tmp_path / "criteria.json"
    path.write_text(json.dumps({"criteria": _criteria()}), encoding="utf-8")

    first = freeze(path)
    second = freeze(path)

    assert first == second
    assert first == canonical_hash(_criteria())
    assert len(first) == 64


def test_all_expected_criteria_must_have_green_proof_on_same_sha_and_hash():
    criteria = _criteria()
    criteria_hash = canonical_hash(criteria)
    evidence = [
        {"criterion_id": "AC-1", "status": "passed", "sha": "abc123", "criteria_hash": criteria_hash, "proof": "pytest test_one"},
        {"criterion_id": "AC-2", "status": "passed", "sha": "abc123", "criteria_hash": criteria_hash, "proof": "browser gate"},
    ]

    result = verify(criteria, evidence, expected_sha="abc123", expected_hash=criteria_hash)

    assert result["passed"] is True
    assert result["missing"] == []


def test_missing_unknown_duplicate_failed_and_wrong_binding_all_block():
    criteria = _criteria()
    criteria_hash = canonical_hash(criteria)
    evidence = [
        {"criterion_id": "AC-1", "status": "failed", "sha": "abc123", "criteria_hash": criteria_hash, "proof": "pytest"},
        {"criterion_id": "AC-1", "status": "passed", "sha": "abc123", "criteria_hash": criteria_hash, "proof": "duplicate"},
        {"criterion_id": "AC-999", "status": "passed", "sha": "abc123", "criteria_hash": criteria_hash, "proof": "unknown"},
        {"criterion_id": "AC-2", "status": "passed", "sha": "other", "criteria_hash": "0" * 64, "proof": "wrong binding"},
    ]

    result = verify(criteria, evidence, expected_sha="abc123", expected_hash=criteria_hash)

    assert result["passed"] is False
    assert result["duplicates"] == ["AC-1"]
    assert result["unknown"] == ["AC-999"]
    assert result["failed"] == ["AC-1"]
    assert result["wrong_sha"] == ["AC-2"]
    assert result["wrong_criteria_hash"] == ["AC-2"]


def test_mutated_criteria_are_rejected_by_frozen_hash():
    original = _criteria()
    frozen_hash = canonical_hash(original)
    mutated = [*original]
    mutated[0] = {"id": "AC-1", "text": "A weaker target succeeds."}

    result = verify(mutated, [], expected_sha="abc123", expected_hash=frozen_hash)

    assert result["passed"] is False
    assert result["criteria_mutated"] is True


def test_invalid_criterion_ids_and_duplicate_expected_ids_are_rejected():
    result = verify(
        [{"id": "one", "text": "bad"}, {"id": "one", "text": "duplicate"}],
        [],
        expected_sha="abc123",
        expected_hash="0" * 64,
    )

    assert result["passed"] is False
    assert result["invalid_expected_ids"] == ["one"]
    assert result["duplicate_expected_ids"] == ["one"]
