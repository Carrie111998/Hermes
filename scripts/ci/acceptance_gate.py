#!/usr/bin/env python3
"""Freeze acceptance criteria and verify deterministic criterion→proof coverage."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^AC-[1-9][0-9]*$")


def _criteria_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("criteria")
    if not isinstance(payload, list):
        raise ValueError("criteria must be a JSON array or an object with a criteria array")
    return payload


def _canonical(criteria: list[dict[str, Any]]) -> bytes:
    normalized: list[dict[str, str]] = []
    for item in criteria:
        if not isinstance(item, dict):
            raise ValueError("each criterion must be an object")
        criterion_id = item.get("id")
        text = item.get("text")
        if not isinstance(criterion_id, str) or not isinstance(text, str) or not text.strip():
            raise ValueError("each criterion requires string id and non-empty text")
        normalized.append({"id": criterion_id.strip(), "text": text.strip()})
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def canonical_hash(criteria: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_canonical(criteria)).hexdigest()


def freeze(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return canonical_hash(_criteria_list(payload))


def verify(
    criteria: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    expected_sha: str,
    expected_hash: str,
) -> dict[str, Any]:
    ids = [item.get("id") for item in criteria if isinstance(item, dict)]
    counts = Counter(ids)
    expected_ids = {item for item in ids if isinstance(item, str)}
    invalid_expected_ids = sorted({item for item in expected_ids if not _ID_RE.fullmatch(item)})
    duplicate_expected_ids = sorted(item for item, count in counts.items() if isinstance(item, str) and count > 1)

    try:
        actual_hash = canonical_hash(criteria)
    except (TypeError, ValueError):
        actual_hash = ""
    criteria_mutated = actual_hash != expected_hash

    evidence_ids = [item.get("criterion_id") for item in evidence if isinstance(item, dict)]
    evidence_counts = Counter(evidence_ids)
    duplicates = sorted(item for item, count in evidence_counts.items() if isinstance(item, str) and count > 1)
    unknown = sorted({item for item in evidence_ids if isinstance(item, str) and item not in expected_ids})

    passed_ids: set[str] = set()
    failed: set[str] = set()
    wrong_sha: set[str] = set()
    wrong_criteria_hash: set[str] = set()
    empty_proof: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        criterion_id = item.get("criterion_id")
        if not isinstance(criterion_id, str) or criterion_id not in expected_ids:
            continue
        if item.get("status") != "passed":
            failed.add(criterion_id)
        if item.get("sha") != expected_sha:
            wrong_sha.add(criterion_id)
        if item.get("criteria_hash") != expected_hash:
            wrong_criteria_hash.add(criterion_id)
        proof = item.get("proof")
        if not isinstance(proof, str) or not proof.strip():
            empty_proof.add(criterion_id)
        if (
            item.get("status") == "passed"
            and item.get("sha") == expected_sha
            and item.get("criteria_hash") == expected_hash
            and isinstance(proof, str)
            and proof.strip()
            and evidence_counts[criterion_id] == 1
        ):
            passed_ids.add(criterion_id)

    missing = sorted(expected_ids - passed_ids)
    result = {
        "passed": False,
        "criteria_hash": actual_hash,
        "criteria_mutated": criteria_mutated,
        "invalid_expected_ids": invalid_expected_ids,
        "duplicate_expected_ids": duplicate_expected_ids,
        "missing": missing,
        "duplicates": duplicates,
        "unknown": unknown,
        "failed": sorted(failed),
        "wrong_sha": sorted(wrong_sha),
        "wrong_criteria_hash": sorted(wrong_criteria_hash),
        "empty_proof": sorted(empty_proof),
    }
    result["passed"] = not any(
        (
            criteria_mutated,
            invalid_expected_ids,
            duplicate_expected_ids,
            missing,
            duplicates,
            unknown,
            failed,
            wrong_sha,
            wrong_criteria_hash,
            empty_proof,
        )
    )
    return result


def _load(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("criteria")

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("criteria")
    verify_parser.add_argument("evidence")
    verify_parser.add_argument("--sha", required=True)
    verify_parser.add_argument("--criteria-hash", required=True)

    args = parser.parse_args()
    if args.command == "freeze":
        print(freeze(Path(args.criteria)))
        return 0

    criteria = _criteria_list(_load(args.criteria))
    evidence_payload = _load(args.evidence)
    evidence = evidence_payload.get("evidence") if isinstance(evidence_payload, dict) else evidence_payload
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a JSON array or object with an evidence array")
    result = verify(criteria, evidence, expected_sha=args.sha, expected_hash=args.criteria_hash)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
