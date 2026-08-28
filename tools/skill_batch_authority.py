"""Atomic cross-skill batches under generation-bound mutation authority.

The skill manager supplies resolution and mutation callbacks; this module owns
the batch plan, composite optimistic-concurrency precondition, deterministic
multi-identity admission, snapshots, execution, and rollback settlement.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Tuple, Union

from tools import skill_mutation_authority as authority


BATCH_PRECONDITION_VERSION = 2
BATCH_MAX_OPS = 20
BATCH_OP_ACTIONS = {"create", "patch", "write_file", "remove_file"}

FindSkill = Callable[[str], Optional[Dict[str, Any]]]
PendingTarget = Callable[..., Tuple[Optional[Path], Optional[str]]]
Preflight = Callable[[str, str], Optional[Dict[str, Any]]]
Mutate = Callable[..., str]


def _tool_error(message: str, **extra: Any) -> str:
    result = {"success": False, "error": message}
    result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def _after_batch_operation_publication(
    index: int,
    name: str,
    skill_dir: Path,
) -> None:
    """Test hook after a generation is recorded and before the next op."""


def normalize_batch_plan(
    operations: Any,
    *,
    default_name: Optional[str],
    preflight: Preflight,
) -> Tuple[
    Optional[list[Dict[str, Any]]],
    Optional[Union[str, Dict[str, Any]]],
]:
    """Validate and normalize the complete ordered mutation plan."""
    if not isinstance(operations, list) or not operations:
        return None, "operations must be a non-empty array."
    if len(operations) > BATCH_MAX_OPS:
        return None, f"operations is capped at {BATCH_MAX_OPS} ops per call."

    normalized: list[Dict[str, Any]] = []
    names_seen: set[str] = set()
    for index, raw in enumerate(operations):
        if not isinstance(raw, dict) or not raw.get("action"):
            return None, f"operations[{index}] needs an 'action'."
        op = dict(raw)
        action = op["action"]
        name = op.get("name") or default_name
        if not name:
            return None, (f"operations[{index}] needs a 'name' (the skill it targets).")
        op["name"] = name
        if action == "delete":
            if len(operations) != 1:
                return None, (
                    "delete must be the SOLE op in its call — it doesn't "
                    "compose with other ops' rollback."
                )
        elif action not in BATCH_OP_ACTIONS:
            return None, (
                f"operations[{index}]: unknown action '{action}'. Batchable: "
                f"{', '.join(sorted(BATCH_OP_ACTIONS))}; delete must be sole."
            )
        if action == "create" and name in names_seen:
            return None, (
                f"operations[{index}]: create for '{name}' must precede that "
                "skill's other ops."
            )
        blocked = preflight(action, name)
        if blocked is not None:
            return None, blocked
        normalized.append(op)
        names_seen.add(name)

    touched_files: set[tuple[str, str]] = set()
    for index, op in enumerate(normalized):
        action = op["action"]
        if action == "delete":
            continue
        file_path = (op.get("file_path") or "").strip()
        target = (
            "SKILL.md"
            if action == "create" or (action == "patch" and op.get("content"))
            else posixpath.normpath(file_path.lstrip("/"))
            if file_path
            else "SKILL.md"
        )
        key = (op["name"], target)
        destructive = action in {"create", "write_file", "remove_file"} or (
            action == "patch" and bool(op.get("content"))
        )
        if destructive and key in touched_files:
            return None, (
                f"operations[{index}]: {action} on '{target}' of skill "
                f"'{op['name']}' — an earlier op in this batch already "
                "touched that file, and this op would silently discard its "
                "work. One destructive op (write_file/remove_file/full "
                "rewrite) per file per batch; put it first, or fold the "
                "change in. Patch chains are fine."
            )
        touched_files.add(key)
    return normalized, None


def _plan_digest(operations: Iterable[Dict[str, Any]]) -> str:
    encoded = json.dumps(
        list(operations),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_batch_precondition(
    operations: list[Dict[str, Any]],
    *,
    find_skill: FindSkill,
    pending_target: PendingTarget,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fingerprint every whole tree plus create-name/destination absence."""
    entries: list[Dict[str, Any]] = []
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for op in operations:
        grouped.setdefault(op["name"], []).append(op)

    try:
        for name in sorted(grouped):
            skill_ops = grouped[name]
            create = next(
                (op for op in skill_ops if op["action"] == "create"),
                None,
            )
            existing = find_skill(name)
            if create is not None:
                destination, error = pending_target(
                    "create",
                    name,
                    content=create.get("content"),
                    category=create.get("category"),
                )
                if error or destination is None:
                    return (
                        None,
                        error or f"could not resolve create destination for '{name}'",
                    )
                logical: Dict[str, Any]
                if existing is None:
                    logical = {"kind": "missing"}
                else:
                    existing_path = Path(existing["path"])
                    logical = {
                        "kind": "present",
                        "target": str(existing_path),
                        "state": authority.snapshot_path_state(existing_path),
                    }
                entries.append({
                    "name": name,
                    "mode": "create",
                    "logical": logical,
                    "destination": str(destination),
                    "destination_state": authority.snapshot_path_state(destination),
                })
                continue

            if existing is not None:
                target = Path(existing["path"])
                mode = "existing"
            else:
                target, error = pending_target("delete", name)
                if error or target is None:
                    return None, error or f"could not resolve target for '{name}'"
                mode = "missing"
            entries.append({
                "name": name,
                "mode": mode,
                "target": str(target),
                "state": authority.snapshot_path_state(target),
            })
    except OSError as exc:
        return None, f"could not fingerprint complete batch plan: {exc}"

    return {
        "version": BATCH_PRECONDITION_VERSION,
        "kind": "skill_batch",
        "plan_sha256": _plan_digest(operations),
        "targets": entries,
    }, None


def mutation_identities(
    precondition: Dict[str, Any],
) -> list[authority.MutationIdentity]:
    """Return the deduplicated logical and physical mutation identities."""
    identities: list[authority.MutationIdentity] = []
    for entry in precondition.get("targets", []):
        if entry.get("mode") == "create":
            identities.append(authority.logical_skill_name_identity(entry["name"]))
            identities.append(Path(entry["destination"]))
        else:
            identities.append(Path(entry["target"]))
    return identities


def _entry_path(entry: Dict[str, Any]) -> Path:
    key = "destination" if entry.get("mode") == "create" else "target"
    return Path(entry[key])


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _rollback_batch(
    snapshots: Dict[str, Dict[str, Any]],
    published: Dict[str, Dict[str, Any]],
    changed_names: set[str],
) -> authority.SettlementResult:
    superseded: list[str] = []
    failed: list[str] = []
    for name in sorted(changed_names):
        snap = snapshots[name]
        path = snap["path"]
        try:
            current = authority.snapshot_path_state(path)
        except Exception as exc:  # noqa: BLE001 - typed failed settlement
            failed.append(f"{name}: current-state fingerprint failed ({exc})")
            continue
        if current != published[name]:
            superseded.append(name)
            continue
        try:
            _remove_tree(path)
            snapshot_dir = snap.get("snapshot_dir")
            if snapshot_dir is not None:
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(snapshot_dir, path, symlinks=True)
            restored = authority.snapshot_path_state(path)
            if restored != snap["original_state"]:
                failed.append(f"{name}: restored fingerprint did not match")
        except Exception as exc:  # noqa: BLE001 - typed failed settlement
            failed.append(f"{name}: restore failed ({exc})")

    if failed:
        return authority.SettlementResult(
            authority.SettlementStatus.FAILED,
            "; ".join(
                failed
                + ([f"superseded: {', '.join(superseded)}"] if superseded else [])
            ),
        )
    if superseded:
        return authority.SettlementResult(
            authority.SettlementStatus.SUPERSEDED,
            f"newer state preserved for: {', '.join(superseded)}",
        )
    return authority.SettlementResult(authority.SettlementStatus.RESTORED)


def _failure_result(
    index: int,
    op: Dict[str, Any],
    parsed: Dict[str, Any],
    settlement: authority.SettlementResult,
) -> str:
    result: Dict[str, Any] = {
        "success": False,
        "error": (
            f"operations[{index}] ({op['action']} on '{op['name']}') failed: "
            f"{parsed.get('error', 'unknown error')} — batch aborted with "
            f"{settlement.status.value} settlement."
        ),
        "failed_index": index,
        "completed_before_failure": index,
        "settlement": settlement.status.value,
    }
    if settlement.detail:
        result["settlement_detail"] = settlement.detail
    if settlement.status is authority.SettlementStatus.FAILED:
        result["error_code"] = "batch_rollback_failed"
    elif settlement.status is authority.SettlementStatus.SUPERSEDED:
        result["error_code"] = "batch_rollback_superseded"
    return json.dumps(result, ensure_ascii=False)


def manage_skill_batch(
    operations: Any,
    *,
    default_name: Optional[str],
    task_id: Optional[str],
    session_id: Optional[str],
    bypass_gate: bool,
    set_gate_bypass: Callable[[bool], Any],
    reset_gate_bypass: Callable[[Any], None],
    find_skill: FindSkill,
    pending_target: PendingTarget,
    preflight: Preflight,
    mutate: Mutate,
) -> str:
    """Stage or execute a complete batch as one authorized transaction."""
    normalized, error = normalize_batch_plan(
        operations,
        default_name=default_name,
        preflight=preflight,
    )
    if error or normalized is None:
        if isinstance(error, dict):
            return json.dumps(error, ensure_ascii=False)
        return _tool_error(error or "invalid batch plan")

    if normalized[0]["action"] == "delete":
        op = normalized[0]
        return mutate(
            action="delete",
            name=op["name"],
            absorbed_into=op.get("absorbed_into"),
            task_id=task_id,
            session_id=session_id,
        )

    if not bypass_gate:
        approval: Any = None
        try:
            from tools import write_approval as approval_module

            approval = approval_module
        except Exception:
            pass
        if approval is not None:
            decision = approval.evaluate_gate(approval.SKILLS)
            if decision.blocked:
                return _tool_error(decision.message)
            if not decision.allow:
                precondition, error = capture_batch_precondition(
                    normalized,
                    find_skill=find_skill,
                    pending_target=pending_target,
                )
                if error or precondition is None:
                    return _tool_error(
                        f"Skill batch could not be safely staged: {error}"
                    )
                actions = ", ".join(op["action"] for op in normalized)
                names = ", ".join(sorted({op["name"] for op in normalized}))
                gist = f"batch({len(normalized)} ops: {actions}) on {names}"
                record = approval.stage_write(
                    approval.SKILLS,
                    {"action": "batch", "operations": normalized},
                    summary=gist,
                    origin=approval.current_origin(),
                    precondition=precondition,
                )
                return json.dumps(
                    {
                        "success": True,
                        "staged": True,
                        "pending_id": record["id"],
                        "gist": gist,
                        "message": decision.message,
                    },
                    ensure_ascii=False,
                )

    expected = authority.bound_pending_precondition()
    if expected is None:
        expected, error = capture_batch_precondition(
            normalized,
            find_skill=find_skill,
            pending_target=pending_target,
        )
        if error or expected is None:
            return _tool_error(f"Could not authorize skill batch: {error}")
    if expected.get("kind") != "skill_batch":
        return _tool_error(
            "Batch replay requires a composite batch precondition.",
            conflict=True,
            error_code="invalid_batch_precondition",
        )

    with authority.multi_skill_mutation_lease(
        mutation_identities(expected)
    ) as admitted:
        if not admitted:
            return json.dumps(
                authority.mutation_lock_timeout_error(), ensure_ascii=False
            )
        current, error = capture_batch_precondition(
            normalized,
            find_skill=find_skill,
            pending_target=pending_target,
        )
        if error or current is None:
            return _tool_error(
                f"Could not verify the pending skill batch safely: {error}",
                conflict=True,
                error_code="precondition_check_failed",
            )
        if current != expected:
            return json.dumps(authority.stale_pending_write_error(), ensure_ascii=False)

        temp_root = Path(tempfile.mkdtemp(prefix="skill_batch_"))
        entries = {entry["name"]: entry for entry in expected["targets"]}
        snapshots: Dict[str, Dict[str, Any]] = {}
        try:
            try:
                for name, entry in entries.items():
                    path = _entry_path(entry)
                    original_state = authority.snapshot_path_state(path)
                    snapshot_dir = None
                    if original_state.get("kind") == "directory":
                        snapshot_dir = (
                            temp_root
                            / hashlib.sha256(
                                name.encode("utf-8", "surrogateescape")
                            ).hexdigest()
                        )
                        shutil.copytree(path, snapshot_dir, symlinks=True)
                    elif original_state.get("kind") != "missing":
                        return _tool_error(
                            f"Could not snapshot '{name}' for atomic batch: "
                            f"target is {original_state.get('kind')}."
                        )
                    snapshots[name] = {
                        "path": path,
                        "original_state": original_state,
                        "snapshot_dir": snapshot_dir,
                    }
            except Exception as exc:  # noqa: BLE001 - no snapshot, no mutation
                return _tool_error(
                    f"Could not snapshot complete skill batch safely: {exc}"
                )

            results = []
            published: Dict[str, Dict[str, Any]] = {}
            changed_names: set[str] = set()
            gate_token = set_gate_bypass(True)
            batch_token = authority.enter_batch_authority()
            try:
                for index, op in enumerate(normalized):
                    try:
                        raw = mutate(
                            action=op["action"],
                            name=op["name"],
                            content=op.get("content"),
                            category=op.get("category"),
                            file_path=op.get("file_path"),
                            file_content=op.get("file_content"),
                            old_string=op.get("old_string"),
                            new_string=op.get("new_string"),
                            replace_all=op.get("replace_all", False),
                            task_id=task_id,
                            session_id=session_id,
                        )
                        try:
                            parsed = json.loads(raw)
                        except Exception:
                            parsed = {
                                "success": False,
                                "error": "unparseable op result",
                            }
                        if parsed.get("success"):
                            path = snapshots[op["name"]]["path"]
                            published[op["name"]] = authority.snapshot_path_state(path)
                            changed_names.add(op["name"])
                            _after_batch_operation_publication(index, op["name"], path)
                    except Exception as exc:  # noqa: BLE001 - settle the transaction
                        parsed = {
                            "success": False,
                            "error": f"batch operation raised unexpectedly: {exc}",
                            "settlement": authority.SettlementStatus.FAILED.value,
                            "settlement_error": (
                                "operation publication could not be fingerprinted"
                            ),
                        }
                    if not parsed.get("success"):
                        settlement = _rollback_batch(
                            snapshots,
                            published,
                            changed_names,
                        )
                        nested = parsed.get("settlement")
                        if nested == authority.SettlementStatus.FAILED.value:
                            details: list[str] = [
                                str(detail)
                                for detail in (
                                    parsed.get("settlement_error"),
                                    settlement.detail,
                                )
                                if detail
                            ]
                            settlement = authority.SettlementResult(
                                authority.SettlementStatus.FAILED,
                                "; ".join(details),
                            )
                        elif (
                            nested == authority.SettlementStatus.SUPERSEDED.value
                            and settlement.status
                            is not authority.SettlementStatus.FAILED
                        ):
                            details = [
                                str(detail)
                                for detail in (
                                    parsed.get("settlement_error"),
                                    settlement.detail,
                                )
                                if detail
                            ]
                            settlement = authority.SettlementResult(
                                authority.SettlementStatus.SUPERSEDED,
                                "; ".join(details),
                            )
                        return _failure_result(index, op, parsed, settlement)
                    results.append({
                        "name": op["name"],
                        "action": op["action"],
                        "file_path": op.get("file_path"),
                        "success": True,
                    })
            finally:
                authority.exit_batch_authority(batch_token)
                reset_gate_bypass(gate_token)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    return json.dumps(
        {
            "success": True,
            "operations_applied": len(results),
            "results": results,
        },
        ensure_ascii=False,
    )
