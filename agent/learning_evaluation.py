"""Deterministic regression checks for proposed skill mutations.

V1 manifests are data, not prompts.  They assert mechanical properties of the
candidate text and compare those results with the exact reviewed baseline.
Qualitative model judging can be layered on later without weakening these hard
contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from hermes_constants import get_hermes_home
from utils import atomic_json_write, atomic_write_text

_SAFE_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _snapshot_dir(candidate_id: str) -> Path:
    home = get_hermes_home()
    learning = home / "learning"
    root = learning / "snapshots"
    directory = root / candidate_id
    if (root.exists() and root.is_symlink()) or (
        directory.exists() and directory.is_symlink()
    ):
        raise ValueError("evaluation snapshot directory must not be a symlink")
    if learning.is_symlink():
        raise ValueError("evaluation snapshot parent must not be a symlink")
    return directory


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_fingerprint(manifest: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical_json(manifest))


def _persist_snapshot_manifest(candidate_id: str, manifest: Mapping[str, Any]) -> None:
    directory = _snapshot_dir(candidate_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = directory / "snapshot.json"
    atomic_json_write(snapshot, manifest, mode=0o600)


def _load_snapshot_manifest(candidate_id: str) -> dict[str, Any]:
    snapshot = _snapshot_dir(candidate_id) / "snapshot.json"
    if snapshot.is_symlink() or not snapshot.is_file():
        raise ValueError("candidate has no usable evaluation snapshot")
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    if data.get("candidate_id") != candidate_id:
        raise ValueError("evaluation snapshot candidate identity mismatch")
    return data


def _evaluate_text(text: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    if int(manifest.get("version", 0)) != 1:
        raise ValueError("evaluation manifest version must be 1")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("evaluation manifest requires at least one case")

    passed = 0
    failures: list[dict[str, Any]] = []
    case_results: list[dict[str, Any]] = []
    for index, raw_case in enumerate(cases):
        if not isinstance(raw_case, Mapping):
            raise ValueError(f"evaluation case {index} must be an object")
        name = str(raw_case.get("name") or f"case-{index + 1}")
        reasons: list[str] = []
        case_sensitive = bool(raw_case.get("case_sensitive", False))
        haystack = text if case_sensitive else text.lower()
        for required in raw_case.get("must_contain") or []:
            needle = str(required) if case_sensitive else str(required).lower()
            if needle not in haystack:
                reasons.append(f"missing required text: {required}")
        for forbidden in raw_case.get("must_not_contain") or []:
            needle = str(forbidden) if case_sensitive else str(forbidden).lower()
            if needle in haystack:
                reasons.append(f"contains forbidden text: {forbidden}")
        max_chars = raw_case.get("max_chars")
        if isinstance(max_chars, int) and len(text) > max_chars:
            reasons.append(f"exceeds max_chars={max_chars}")
        min_chars = raw_case.get("min_chars")
        if isinstance(min_chars, int) and len(text) < min_chars:
            reasons.append(f"below min_chars={min_chars}")
        ok = not reasons
        case_results.append({"case": name, "passed": ok, "reasons": reasons})
        if ok:
            passed += 1
        else:
            failures.append({"case": name, "reasons": reasons})
    return {
        "passed": passed,
        "total": len(cases),
        "failures": failures,
        "cases": case_results,
    }


def compare_texts(*, baseline: str, candidate: str, manifest: Mapping[str, Any]) -> dict[str, Any]:
    baseline_result = _evaluate_text(baseline, manifest)
    candidate_result = _evaluate_text(candidate, manifest)
    baseline_passed = {
        item["case"] for item in baseline_result["cases"] if item["passed"]
    }
    candidate_passed = {
        item["case"] for item in candidate_result["cases"] if item["passed"]
    }
    if baseline_passed - candidate_passed:
        verdict = "regressed"
    elif candidate_result["passed"] > baseline_result["passed"]:
        verdict = "improved"
    elif candidate_result["passed"] == candidate_result["total"]:
        verdict = "passed"
    else:
        verdict = "unchanged"
    return {
        "verdict": verdict,
        "baseline": baseline_result,
        "candidate": candidate_result,
    }


def simulate_candidate_text(baseline: str, payload: Mapping[str, Any]) -> str:
    action = str(payload.get("action") or "")
    if action in {"create", "edit"}:
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError(f"{action} candidate requires content")
        return content
    if action == "patch":
        old = payload.get("old_string")
        new = payload.get("new_string")
        if not isinstance(old, str) or not old:
            raise ValueError("patch candidate requires old_string")
        if not isinstance(new, str):
            raise ValueError("patch candidate requires new_string")
        occurrences = baseline.count(old)
        if occurrences == 0:
            raise ValueError("reviewed baseline no longer contains old_string")
        replace_all = bool(payload.get("replace_all"))
        if occurrences > 1 and not replace_all:
            raise ValueError("reviewed baseline contains a non-unique old_string")
        return baseline.replace(old, new) if replace_all else baseline.replace(old, new, 1)
    if action == "delete":
        return ""
    raise ValueError(f"unsupported deterministic evaluation action: {action}")


def evaluate_pending_skill(record: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a pending skill payload against its colocated manifest."""
    payload = dict(record.get("payload") or {})
    name = str(payload.get("name") or "")
    if not name:
        raise ValueError("pending skill candidate has no name")
    from tools.skill_manager_tool import _find_skill, _validate_file_path

    found = _find_skill(name)
    action = str(payload.get("action") or "")
    if found is None and action != "create":
        raise ValueError(f"skill '{name}' is no longer available")
    skill_dir = Path(found["path"]) if found is not None else None
    target_file = "SKILL.md"
    requested_file = payload.get("file_path")
    if requested_file:
        path_error = _validate_file_path(str(requested_file))
        if path_error:
            raise ValueError(path_error)
        target_file = str(requested_file)
    baseline_path = skill_dir / target_file if skill_dir is not None else None
    if baseline_path is not None and baseline_path.is_symlink():
        raise ValueError("evaluated skill target must not be a symlink")
    if skill_dir is not None and baseline_path is not None:
        try:
            baseline_path.resolve().relative_to(skill_dir.resolve())
        except ValueError as exc:
            raise ValueError("evaluated skill target escapes its skill directory") from exc
    baseline = baseline_path.read_text(encoding="utf-8") if baseline_path and baseline_path.exists() else ""
    manifest_path = skill_dir / "evals" / "manifest.json" if skill_dir is not None else None
    if manifest_path is None or not manifest_path.exists():
        return {"verdict": "no_manifest", "baseline": None, "candidate": None}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if action not in {"edit", "patch"}:
        raise ValueError("deterministic skill evaluation supports edit and patch candidates")
    candidate = simulate_candidate_text(baseline, payload)
    candidate_id = str(record.get("candidate_id") or record.get("id") or "")
    if candidate_id:
        if not _SAFE_CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError("invalid candidate id for baseline snapshot")
        # Bind skill identity into the ledger so rollback can verify it later.
        # Skip when the candidate has no ledger row (e.g. standalone evaluation).
        from agent import learning_ledger

        if learning_ledger.ledger_exists():
            learning_ledger.update_candidate_proposal_fields(
                candidate_id,
                {"skill_name": name, "target_file": target_file},
            )
        directory = _snapshot_dir(candidate_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        baseline_path = directory / "baseline.txt"
        atomic_write_text(baseline_path, baseline)
        _persist_snapshot_manifest(
            candidate_id,
            {
                "version": 1,
                "candidate_id": candidate_id,
                "skill_name": name,
                "target_file": target_file,
                "baseline_sha256": _sha256_text(baseline),
                "candidate_sha256": _sha256_text(candidate),
                "rollback_action": "edit" if target_file == "SKILL.md" else "write_file",
                "manifest_fingerprint": _snapshot_fingerprint(manifest),
            },
        )
        try:
            baseline_path.chmod(0o600)
        except OSError:
            pass
    return compare_texts(baseline=baseline, candidate=candidate, manifest=manifest)


def _sha256_text(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def prepare_evaluated_skill_rollback(candidate_id: str) -> dict[str, Any]:
    """Build a rollback payload after proving the evaluated target is unchanged.

    This function is read-only. The caller must execute the returned payload via
    ``apply_skill_pending`` so the existing skill mutation handler remains the
    sole mutation engine.
    """
    if not _SAFE_CANDIDATE_ID.fullmatch(candidate_id):
        raise ValueError("invalid candidate id for rollback")
    from agent import learning_ledger
    from tools.skill_manager_tool import _find_skill, _validate_file_path

    candidate = learning_ledger.get_candidate(candidate_id)
    if candidate is None or candidate.get("subsystem") != "skills":
        raise ValueError("unknown evaluated skill candidate")
    if candidate.get("status") not in {"active", "validated", "rolling_back"}:
        raise ValueError("only active, validated, or rolling_back candidates can be rolled back")
    metadata = _load_snapshot_manifest(candidate_id)
    if metadata.get("version") != 1:
        raise ValueError("evaluation snapshot metadata does not match candidate")
    name = str(metadata.get("skill_name") or "")
    if not name:
        raise ValueError("evaluation snapshot is missing skill identity")
    # Verify snapshot identity against the skill_name stored in the ledger
    # at evaluation time.  A mismatch means the snapshot was retargeted.
    proposal = dict(candidate.get("proposal") or {})
    proposal_name = str(proposal.get("skill_name") or "")
    if proposal_name and proposal_name != name:
        raise ValueError("evaluation snapshot skill identity does not match candidate")
    # Also verify target_file if it was stored.
    proposal_target = str(proposal.get("target_file") or "")
    if proposal_target:
        metadata_target = str(metadata.get("target_file") or "SKILL.md")
        if proposal_target != metadata_target:
            raise ValueError("evaluation snapshot target file does not match candidate")
    found = _find_skill(name)
    if found is None:
        raise ValueError("evaluated skill target no longer exists")
    target_file = str(metadata.get("target_file") or "SKILL.md")
    if target_file != "SKILL.md":
        path_error = _validate_file_path(target_file)
        if path_error:
            raise ValueError(path_error)
    skill_dir = Path(found["path"])
    current_path = skill_dir / target_file
    try:
        current_path.resolve().relative_to(skill_dir.resolve())
    except ValueError as exc:
        raise ValueError("evaluated skill target escapes its skill directory") from exc
    if current_path.is_symlink() or not current_path.is_file():
        raise ValueError("evaluated skill target is not a regular file")
    current = current_path.read_text(encoding="utf-8")
    if _sha256_text(current) != metadata.get("candidate_sha256"):
        raise ValueError("skill changed after evaluated candidate was applied")
    baseline_path = _snapshot_dir(candidate_id) / "baseline.txt"
    baseline = baseline_path.read_text(encoding="utf-8")
    if _sha256_text(baseline) != metadata.get("baseline_sha256"):
        raise ValueError("evaluation snapshot baseline is corrupted")
    manifest_path = skill_dir / "evals" / "manifest.json"
    if manifest_path.exists():
        current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if _snapshot_fingerprint(current_manifest) != metadata.get("manifest_fingerprint"):
            raise ValueError("evaluation manifest changed after snapshot was taken")
    if target_file == "SKILL.md":
        return {"action": "edit", "name": name, "content": baseline}
    return {
        "action": "write_file",
        "name": name,
        "file_path": target_file,
        "file_content": baseline,
    }


def evaluate_pending_memory(record: Mapping[str, Any], store) -> dict[str, Any]:
    """Simulate a memory mutation and enforce deterministic store invariants."""
    payload = dict(record.get("payload") or {})
    target = str(payload.get("target") or "memory")
    baseline = list(store._entries_for(target))
    candidate = list(baseline)

    def apply_one(operation: Mapping[str, Any]) -> None:
        action = str(operation.get("action") or "")
        content = operation.get("content")
        old_text = operation.get("old_text")
        if action == "add":
            if not isinstance(content, str) or not content.strip():
                raise ValueError("memory add requires non-empty content")
            if content not in candidate:
                candidate.append(content)
        elif action == "replace":
            needle = str(old_text or "")
            matches = [i for i, entry in enumerate(candidate) if needle and needle in entry]
            if len({candidate[i] for i in matches}) != 1 or not isinstance(content, str) or not content.strip():
                raise ValueError("memory replace target is stale or replacement is empty")
            candidate[matches[0]] = content
        elif action == "remove":
            needle = str(old_text or "")
            matches = [i for i, entry in enumerate(candidate) if needle and needle in entry]
            if len({candidate[i] for i in matches}) != 1:
                raise ValueError("memory remove target is stale")
            candidate.pop(matches[0])
        else:
            raise ValueError(f"unsupported memory evaluation action: {action}")

    if payload.get("action") == "batch":
        for operation in payload.get("operations") or []:
            apply_one(operation)
    else:
        apply_one(payload)

    limit = store.user_char_limit if target == "user" else store.memory_char_limit
    checks = {
        "nonempty_entries": all(isinstance(item, str) and item.strip() for item in candidate),
        "no_duplicates": len(candidate) == len(set(candidate)),
        "within_char_limit": sum(len(item) for item in candidate) <= limit,
    }
    candidate_id = str(record.get("candidate_id") or record.get("id") or "")
    if candidate_id:
        if not _SAFE_CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError("invalid candidate id for memory snapshot")
        snapshot = _snapshot_dir(candidate_id) / "memory.json"
        atomic_json_write(
            snapshot,
            {
                "version": 1,
                "candidate_id": candidate_id,
                "target": target,
                "baseline": baseline,
                "candidate": candidate,
            },
            mode=0o600,
        )
    passed = sum(bool(value) for value in checks.values())
    return {
        "verdict": "passed" if passed == len(checks) else "regressed",
        "baseline": {"entries": len(baseline)},
        "candidate": {"passed": passed, "total": len(checks), "checks": checks},
    }


def prepare_evaluated_memory_rollback(candidate_id: str, store) -> dict[str, Any]:
    """Build an exact batch rollback after proving evaluated memory is unchanged."""
    if not _SAFE_CANDIDATE_ID.fullmatch(candidate_id):
        raise ValueError("invalid candidate id for rollback")
    from agent import learning_ledger

    candidate_record = learning_ledger.get_candidate(candidate_id)
    if candidate_record is None or candidate_record.get("subsystem") != "memory":
        raise ValueError("unknown evaluated memory candidate")
    if candidate_record.get("status") not in {"active", "validated", "rolling_back"}:
        raise ValueError("only active, validated, or rolling_back candidates can be rolled back")
    snapshot = _snapshot_dir(candidate_id) / "memory.json"
    if snapshot.is_symlink() or not snapshot.is_file():
        raise ValueError("candidate has no usable memory evaluation snapshot")
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    if data.get("version") != 1 or data.get("candidate_id") != candidate_id:
        raise ValueError("memory evaluation snapshot does not match candidate")
    target = str(data.get("target") or "memory")
    baseline = data.get("baseline")
    expected = data.get("candidate")
    if not isinstance(baseline, list) or not all(isinstance(item, str) for item in baseline):
        raise ValueError("memory evaluation baseline is malformed")
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError("memory evaluation candidate is malformed")
    current = list(store._entries_for(target))
    if current != expected:
        raise ValueError("memory changed after evaluated candidate was applied")
    operations = [
        {"action": "remove", "old_text": entry}
        for entry in sorted(current, key=len, reverse=True)
    ]
    operations.extend({"action": "add", "content": entry} for entry in baseline)
    return {"action": "batch", "target": target, "operations": operations}
