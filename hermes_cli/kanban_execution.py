"""Workflow v1 leaf registration, readiness, and restart reconciliation.

The module is deliberately an orchestrator seam.  It creates opt-in execution
leaves through :mod:`hermes_cli.kanban_db`; generic Kanban tasks never enter
this path.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_evidence import (
    _hardened_git_command,
    _hardened_git_env,
    normalize_evidence_paths,
)
from hermes_cli.sqlite_util import write_txn

_CAPSULE_SCHEMA = "hermes.execution-capsule.v1"
WORKFLOW_CONTROLLER_STALE_SECONDS = kb.WORKFLOW_CONTROLLER_STALE_SECONDS
_CAPSULE_MAX_BYTES = 32 * 1024
_ID_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_payload(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _workspace_claim_lock_path(workspace_path: Path | str) -> Path:
    canonical = Path(workspace_path).resolve(strict=True)
    digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    return kb.kanban_home() / "kanban" / "workspace-claim-locks" / f"{digest}.lock"


def _workspace_reservation_db_path() -> Path:
    return kb.kanban_home() / "kanban" / "workspace-reservations.db"


def _canonical_workspace(workspace_path: Path | str) -> str:
    return str(Path(workspace_path).resolve(strict=True))


def _board_db_identity(conn: sqlite3.Connection) -> str:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return str(Path(row[2]).resolve())
    raise RuntimeError("kanban board database has no durable path")


def _reservation_conn() -> sqlite3.Connection:
    path = _workspace_reservation_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS workspace_reservations ("
        "workspace TEXT PRIMARY KEY, board_db TEXT NOT NULL, task_id TEXT NOT NULL, "
        "run_id INTEGER, claim_lock TEXT NOT NULL, acquired_at INTEGER NOT NULL)"
    )
    conn.commit()
    return conn


def _workspace_reservation(workspace_path: Path | str) -> Optional[sqlite3.Row]:
    workspace = _canonical_workspace(workspace_path)
    with _reservation_conn() as conn:
        return conn.execute(
            "SELECT * FROM workspace_reservations WHERE workspace = ?",
            (workspace,),
        ).fetchone()


def _reservation_owner_matches(
    reservation: sqlite3.Row,
    conn: sqlite3.Connection,
    task_id: str,
) -> bool:
    return bool(
        reservation["board_db"] == _board_db_identity(conn)
        and reservation["task_id"] == task_id
    )


def _acquire_workspace_reservation(
    conn: sqlite3.Connection,
    *,
    workspace_path: Path | str,
    task_id: str,
    claim_lock: str,
) -> bool:
    workspace = _canonical_workspace(workspace_path)
    board_db = _board_db_identity(conn)
    with _reservation_conn() as reservations:
        reservations.execute("BEGIN IMMEDIATE")
        current = reservations.execute(
            "SELECT * FROM workspace_reservations WHERE workspace = ?",
            (workspace,),
        ).fetchone()
        if current is not None:
            reservations.rollback()
            return False
        reservations.execute(
            "INSERT INTO workspace_reservations "
            "(workspace, board_db, task_id, run_id, claim_lock, acquired_at) "
            "VALUES (?, ?, ?, NULL, ?, ?)",
            (workspace, board_db, task_id, claim_lock, int(time.time())),
        )
        reservations.commit()
    return True


def _finalize_workspace_reservation(
    conn: sqlite3.Connection,
    *,
    workspace_path: Path | str,
    task_id: str,
    claim_lock: str,
    run_id: int,
) -> bool:
    with _reservation_conn() as reservations:
        updated = reservations.execute(
            "UPDATE workspace_reservations SET run_id = ? "
            "WHERE workspace = ? AND board_db = ? AND task_id = ? "
            "AND claim_lock = ? AND run_id IS NULL",
            (
                int(run_id),
                _canonical_workspace(workspace_path),
                _board_db_identity(conn),
                task_id,
                claim_lock,
            ),
        )
        reservations.commit()
        return updated.rowcount == 1


def _release_workspace_reservation(
    conn: sqlite3.Connection,
    *,
    workspace_path: Path | str,
    task_id: str,
    claim_lock: str,
) -> bool:
    with _reservation_conn() as reservations:
        deleted = reservations.execute(
            "DELETE FROM workspace_reservations "
            "WHERE workspace = ? AND board_db = ? AND task_id = ? AND claim_lock = ?",
            (
                _canonical_workspace(workspace_path),
                _board_db_identity(conn),
                task_id,
                claim_lock,
            ),
        )
        reservations.commit()
        return deleted.rowcount == 1


@contextlib.contextmanager
def _workspace_claim_lock(workspace_path: Path | str):
    """Fail-closed, non-blocking lock for one canonical workspace."""

    handle = None
    acquired = False
    try:
        lock_path = _workspace_claim_lock_path(workspace_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        if kb._IS_WINDOWS:
            import msvcrt

            handle.seek(0)
            getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_NBLCK"), 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
    except (OSError, RuntimeError, ValueError):
        acquired = False
    try:
        yield acquired
    finally:
        if handle is not None:
            try:
                if acquired:
                    if kb._IS_WINDOWS:
                        import msvcrt

                        handle.seek(0)
                        getattr(msvcrt, "locking")(
                            handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1
                        )
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _required_text(name: str, value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{name} contains control characters")
    return normalized


def _required_texts(name: str, values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(_required_text(name, item) for item in values)
    if not normalized:
        raise ValueError(f"{name} must contain at least one entry")
    return normalized


@dataclass(frozen=True)
class LeafSpec:
    repository: str
    campaign_issue: str
    leaf_id: str
    version: int
    objective: str
    exclusions: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    dependencies: tuple[str, ...]
    acceptance_checks: tuple[str, ...]
    hazards: tuple[str, ...]
    human_gates: tuple[str, ...]
    pin_sha: str
    first_evidence_seconds: int = 600
    wall_clock_budget_seconds: int = 1500

    def __post_init__(self) -> None:
        repository = _required_text("repository", self.repository).lower()
        campaign_issue_raw = _required_text("campaign_issue", self.campaign_issue)
        leaf_id = _required_text("leaf_id", self.leaf_id)
        repository_parts = repository.split("/")
        if (
            len(repository_parts) != 2
            or not all(repository_parts)
            or not _ID_RE.fullmatch(repository)
        ):
            raise ValueError("repository must be a canonical GitHub owner/name")
        if not campaign_issue_raw.isdecimal() or int(campaign_issue_raw) < 1:
            raise ValueError("campaign_issue must be a positive GitHub issue number")
        campaign_issue = str(int(campaign_issue_raw))
        if not _ID_RE.fullmatch(leaf_id):
            raise ValueError("leaf_id contains unsupported characters")
        if int(self.version) < 1:
            raise ValueError("version must be at least 1")
        _required_text("objective", self.objective)
        _required_texts("exclusions", self.exclusions)
        normalize_evidence_paths(self.allowed_paths)
        _required_texts("acceptance_checks", self.acceptance_checks)
        _required_texts("hazards", self.hazards)
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", self.pin_sha):
            raise ValueError("pin_sha must be a 40-64 character hexadecimal Git SHA")
        if int(self.first_evidence_seconds) < 60:
            raise ValueError("first_evidence_seconds must be at least 60")
        if int(self.wall_clock_budget_seconds) < int(self.first_evidence_seconds):
            raise ValueError(
                "wall_clock_budget_seconds precedes first evidence deadline"
            )
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "campaign_issue", campaign_issue)
        object.__setattr__(self, "leaf_id", leaf_id)
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(
            self, "objective", _required_text("objective", self.objective)
        )
        object.__setattr__(
            self,
            "exclusions",
            tuple(_required_text("exclusions", value) for value in self.exclusions),
        )
        object.__setattr__(
            self, "allowed_paths", normalize_evidence_paths(self.allowed_paths)
        )
        object.__setattr__(
            self,
            "dependencies",
            tuple(_required_text("dependencies", value) for value in self.dependencies),
        )
        object.__setattr__(
            self,
            "acceptance_checks",
            tuple(
                _required_text("acceptance_checks", value)
                for value in self.acceptance_checks
            ),
        )
        object.__setattr__(
            self,
            "hazards",
            tuple(_required_text("hazards", value) for value in self.hazards),
        )
        object.__setattr__(
            self,
            "human_gates",
            tuple(_required_text("human_gates", value) for value in self.human_gates),
        )
        object.__setattr__(self, "pin_sha", self.pin_sha.lower())
        object.__setattr__(
            self, "first_evidence_seconds", int(self.first_evidence_seconds)
        )
        object.__setattr__(
            self, "wall_clock_budget_seconds", int(self.wall_clock_budget_seconds)
        )

    @property
    def leaf_key(self) -> str:
        return (
            f"github:{self.repository}:issue-{self.campaign_issue}:"
            f"leaf-{self.leaf_id}:v{int(self.version)}"
        )

    @property
    def leaf_family_key(self) -> str:
        return (
            f"github:{self.repository}:issue-{self.campaign_issue}:leaf-{self.leaf_id}"
        )

    def payload(self) -> dict:
        value = asdict(self)
        value["allowed_paths"] = list(normalize_evidence_paths(self.allowed_paths))
        value["pin_sha"] = self.pin_sha.lower()
        return value


@dataclass(frozen=True)
class ContextCapsule:
    relevant_files: tuple[str, ...]
    symbols: tuple[str, ...]
    governing_decisions: tuple[str, ...]
    base_assumptions: tuple[str, ...]
    output_schema: tuple[str, ...]
    # Present only on controller-materialized capsules. Legacy callers remain
    # readable, but production dispatch requires these immutable construction
    # coordinates and revalidates them before reserve.
    source_sha: Optional[str] = None
    source_tree_hash: Optional[str] = None
    construction_inputs_hash: Optional[str] = None
    construction_duration_ms: Optional[int] = None

    def __post_init__(self) -> None:
        paths = normalize_evidence_paths(self.relevant_files)
        if any("*" in path or "?" in path or "[" in path for path in paths):
            raise ValueError("relevant_files must name targeted files, not globs")
        _required_texts("symbols", self.symbols)
        _required_texts("governing_decisions", self.governing_decisions)
        _required_texts("base_assumptions", self.base_assumptions)
        _required_texts("output_schema", self.output_schema)
        encoded = _canonical_json(self.payload()).encode("utf-8")
        if len(encoded) > _CAPSULE_MAX_BYTES:
            raise ValueError(f"context capsule exceeds {_CAPSULE_MAX_BYTES} bytes")

    def payload(self) -> dict:
        payload = {
            "relevant_files": list(normalize_evidence_paths(self.relevant_files)),
            "symbols": list(self.symbols),
            "governing_decisions": list(self.governing_decisions),
            "base_assumptions": list(self.base_assumptions),
            "output_schema": list(self.output_schema),
        }
        if self.source_sha is not None:
            payload["materialization"] = {
                "source_sha": self.source_sha,
                "source_tree_hash": self.source_tree_hash,
                "construction_inputs_hash": self.construction_inputs_hash,
                "construction_duration_ms": self.construction_duration_ms,
            }
        return payload


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class ReconciliationReport:
    adopted_task_ids: tuple[str, ...]
    closed_orphan_run_ids: tuple[int, ...]
    quarantined_task_ids: tuple[str, ...]
    findings: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowControllerState:
    version: int
    dispatch_enabled: bool
    broker_ready: bool
    status: str
    controller_epoch: Optional[str]
    heartbeat_at: Optional[int]
    last_reconciled_at: Optional[int]
    last_error: Optional[str]
    updated_at: int


class WorkflowControlConflict(RuntimeError):
    """The operator acted on a stale controller projection."""


class WorkflowControlUnavailable(RuntimeError):
    """A required server-side capability is not ready."""


def _controller_state(row: sqlite3.Row) -> WorkflowControllerState:
    return WorkflowControllerState(
        version=int(row["version"]),
        dispatch_enabled=bool(row["dispatch_enabled"]),
        broker_ready=bool(row["broker_ready"]),
        status=str(row["status"]),
        controller_epoch=row["controller_epoch"],
        heartbeat_at=(
            int(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None
        ),
        last_reconciled_at=(
            int(row["last_reconciled_at"])
            if row["last_reconciled_at"] is not None
            else None
        ),
        last_error=row["last_error"],
        updated_at=int(row["updated_at"]),
    )


def get_workflow_controller_state(
    conn: sqlite3.Connection,
) -> WorkflowControllerState:
    row = conn.execute(
        "SELECT * FROM workflow_controller_state WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("workflow controller state is not initialized")
    return _controller_state(row)


def _append_controller_event(
    conn: sqlite3.Connection,
    *,
    version: int,
    kind: str,
    actor: str,
    payload: Mapping[str, object],
    created_at: int,
) -> None:
    conn.execute(
        "INSERT INTO workflow_controller_events "
        "(version, kind, actor, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (
            int(version),
            kind,
            _required_text("actor", actor),
            _canonical_json(payload),
            int(created_at),
        ),
    )


def set_workflow_dispatch_enabled(
    conn: sqlite3.Connection,
    *,
    enabled: bool,
    expected_version: int,
    actor: str,
    reason: str,
) -> WorkflowControllerState:
    """CAS the durable remote kill switch.

    Resume is impossible until the separately managed broker-readiness gate is
    true. Pause is always available and never depends on Desktop liveness.
    """

    now = int(time.time())
    actor = _required_text("actor", actor)
    reason = _required_text("reason", reason)
    with write_txn(conn):
        current = conn.execute(
            "SELECT * FROM workflow_controller_state WHERE singleton = 1"
        ).fetchone()
        if current is None:
            raise RuntimeError("workflow controller state is not initialized")
        if int(current["version"]) != int(expected_version):
            raise WorkflowControlConflict("stale workflow controller version")
        if enabled and not bool(current["broker_ready"]):
            raise WorkflowControlUnavailable(
                "workflow dispatch cannot resume before the worker broker is ready"
            )
        next_version = int(current["version"]) + 1
        updated = conn.execute(
            "UPDATE workflow_controller_state SET version = ?, "
            "dispatch_enabled = ?, updated_at = ? "
            "WHERE singleton = 1 AND version = ?",
            (next_version, int(bool(enabled)), now, int(expected_version)),
        )
        if updated.rowcount != 1:
            raise WorkflowControlConflict("stale workflow controller version")
        _append_controller_event(
            conn,
            version=next_version,
            kind="dispatch_resumed" if enabled else "dispatch_paused",
            actor=actor,
            payload={"enabled": bool(enabled), "reason": reason},
            created_at=now,
        )
    return get_workflow_controller_state(conn)


def set_workflow_broker_ready(
    conn: sqlite3.Connection,
    *,
    ready: bool,
    expected_version: int,
    actor: str,
    reason: str,
) -> WorkflowControllerState:
    """Controller-only broker gate used by startup verification and tests."""

    now = int(time.time())
    actor = _required_text("actor", actor)
    reason = _required_text("reason", reason)
    with write_txn(conn):
        current = conn.execute(
            "SELECT * FROM workflow_controller_state WHERE singleton = 1"
        ).fetchone()
        if current is None:
            raise RuntimeError("workflow controller state is not initialized")
        if int(current["version"]) != int(expected_version):
            raise WorkflowControlConflict("stale workflow controller version")
        next_version = int(current["version"]) + 1
        dispatch_enabled = bool(current["dispatch_enabled"]) and bool(ready)
        updated = conn.execute(
            "UPDATE workflow_controller_state SET version = ?, broker_ready = ?, "
            "dispatch_enabled = ?, updated_at = ? "
            "WHERE singleton = 1 AND version = ?",
            (
                next_version,
                int(bool(ready)),
                int(dispatch_enabled),
                now,
                int(expected_version),
            ),
        )
        if updated.rowcount != 1:
            raise WorkflowControlConflict("stale workflow controller version")
        _append_controller_event(
            conn,
            version=next_version,
            kind="broker_ready" if ready else "broker_unavailable",
            actor=actor,
            payload={"ready": bool(ready), "reason": reason},
            created_at=now,
        )
    return get_workflow_controller_state(conn)


def begin_workflow_controller_epoch(
    conn: sqlite3.Connection,
    *,
    controller_epoch: str,
    actor: str = "remote-gateway",
) -> WorkflowControllerState:
    """Persist a supervised gateway generation before reconciliation starts."""

    epoch = _required_text("controller_epoch", controller_epoch)
    now = int(time.time())
    with write_txn(conn):
        current = conn.execute(
            "SELECT version FROM workflow_controller_state WHERE singleton = 1"
        ).fetchone()
        if current is None:
            raise RuntimeError("workflow controller state is not initialized")
        next_version = int(current["version"]) + 1
        conn.execute(
            "UPDATE workflow_controller_state SET version = ?, status = 'starting', "
            "controller_epoch = ?, heartbeat_at = ?, dispatch_enabled = 0, "
            "broker_ready = 0, last_error = NULL, "
            "updated_at = ? WHERE singleton = 1",
            (next_version, epoch, now, now),
        )
        _append_controller_event(
            conn,
            version=next_version,
            kind="controller_epoch_started",
            actor=actor,
            payload={"controller_epoch": epoch},
            created_at=now,
        )
    return get_workflow_controller_state(conn)


def _is_scoped(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _git_head(workspace_path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            _hardened_git_command(workspace_path, "rev-parse", "HEAD"),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=_hardened_git_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip().lower()


def _git_branch(workspace_path: str) -> Optional[str]:
    try:
        result = subprocess.run(
            _hardened_git_command(
                workspace_path,
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=_hardened_git_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def _git_workspace_clean(workspace_path: str) -> Optional[bool]:
    try:
        result = subprocess.run(
            _hardened_git_command(
                workspace_path,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=_hardened_git_env(),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return not result.stdout


def _git_is_ancestor(
    workspace_path: str, ancestor_sha: str, descendant_sha: str
) -> Optional[bool]:
    try:
        result = subprocess.run(
            _hardened_git_command(
                workspace_path,
                "merge-base",
                "--is-ancestor",
                ancestor_sha,
                descendant_sha,
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_hardened_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def register_execution_leaf(
    conn: sqlite3.Connection,
    *,
    spec: LeafSpec,
    capsule: ContextCapsule,
    assignee: str,
    workspace_path: str | Path,
    parents: Sequence[str] = (),
    board: Optional[str] = None,
    branch_name: Optional[str] = None,
) -> str:
    """Persist a versioned execution intent without launching a worker."""

    spec_payload = spec.payload()
    capsule_payload = capsule.payload()
    normalized_parents = tuple(
        str(parent).strip() for parent in parents if str(parent).strip()
    )
    if normalized_parents != tuple(spec_payload["dependencies"]):
        raise ValueError("spec dependencies must exactly match persisted parent links")
    patterns = tuple(spec_payload["allowed_paths"])
    # Capsule targets are bounded read context from the pinned source.  They are
    # intentionally independent from ``allowed_paths``, which is the writable
    # evidence boundary.  Requiring every contextual source file to be writable
    # makes a new-file-only pilot impossible and unnecessarily broadens worker
    # mutation authority.

    envelope = {
        "schema": _CAPSULE_SCHEMA,
        "spec": spec_payload,
        "capsule": capsule_payload,
    }
    body = _canonical_json(envelope)
    if len(body.encode("utf-8")) > _CAPSULE_MAX_BYTES:
        raise ValueError(f"execution capsule exceeds {_CAPSULE_MAX_BYTES} bytes")

    workspace = str(Path(workspace_path).resolve())
    expected_branch = branch_name or _git_branch(workspace)
    if expected_branch is None:
        raise ValueError("branch_name is required for a detached workspace")
    try:
        branch_check = subprocess.run(
            _hardened_git_command(
                workspace, "check-ref-format", "--branch", expected_branch
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_hardened_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("branch_name could not be validated") from exc
    if branch_check.returncode != 0:
        raise ValueError("branch_name is not a valid Git branch")

    return kb.create_task(
        conn,
        title=(
            f"[{spec.repository}#{spec.campaign_issue}] "
            f"{spec.leaf_id} v{int(spec.version)}"
        ),
        body=body,
        assignee=assignee,
        parents=normalized_parents,
        created_by="workflow-v1-orchestrator",
        workspace_kind="worktree",
        workspace_path=workspace,
        branch_name=expected_branch,
        max_runtime_seconds=int(spec.wall_clock_budget_seconds),
        board=board,
        leaf_key=spec.leaf_key,
        leaf_family_key=spec.leaf_family_key,
        spec_hash=_hash_payload(spec_payload),
        pin_sha=spec.pin_sha.lower(),
        capsule_hash=_hash_payload(capsule_payload),
        evidence_paths=patterns,
        lease_policy="evidence",
        allow_execution_successor=True,
    )


def _load_envelope(task: kb.Task) -> tuple[Optional[Mapping], list[str]]:
    blockers: list[str] = []
    try:
        envelope = json.loads(task.body or "")
    except (TypeError, ValueError):
        return None, ["capsule_invalid_json"]
    if not isinstance(envelope, dict) or envelope.get("schema") != _CAPSULE_SCHEMA:
        return None, ["capsule_schema_mismatch"]
    spec = envelope.get("spec")
    capsule = envelope.get("capsule")
    if not isinstance(spec, dict) or not isinstance(capsule, dict):
        return None, ["capsule_shape_invalid"]
    if _hash_payload(spec) != task.spec_hash:
        blockers.append("spec_hash_mismatch")
    if _hash_payload(capsule) != task.capsule_hash:
        blockers.append("capsule_hash_mismatch")
    if spec.get("pin_sha", "").lower() != task.pin_sha:
        blockers.append("pin_sha_mismatch")
    if list(spec.get("allowed_paths", [])) != list(task.evidence_paths or []):
        blockers.append("allowed_paths_mismatch")
    return envelope, blockers


def validate_execution_readiness(
    conn: sqlite3.Connection,
    task_id: str,
) -> ReadinessResult:
    """Check immutable capsule, dependencies, workspace, and pinned base."""

    task = kb.get_task(conn, task_id)
    if task is None:
        return ReadinessResult(False, ("task_missing",))
    if not task.leaf_key or task.lease_policy != "evidence":
        return ReadinessResult(False, ("not_execution_leaf",))

    blockers: list[str] = []
    envelope, envelope_blockers = _load_envelope(task)
    blockers.extend(envelope_blockers)
    if task.status not in {"todo", "ready"}:
        blockers.append("task_not_ready")

    unresolved = conn.execute(
        "SELECT COUNT(*) FROM task_links d "
        "JOIN tasks p ON p.id = d.parent_id "
        "WHERE d.child_id = ? AND p.status NOT IN ('done', 'archived')",
        (task_id,),
    ).fetchone()[0]
    if int(unresolved) > 0:
        blockers.append("dependencies_not_done")

    if envelope is not None:
        declared_dependencies = envelope["spec"].get("dependencies")
        linked_dependencies = kb.parent_ids(conn, task_id)
        if (
            not isinstance(declared_dependencies, list)
            or not all(isinstance(value, str) for value in declared_dependencies)
            or len(set(declared_dependencies)) != len(declared_dependencies)
            or sorted(declared_dependencies) != linked_dependencies
        ):
            blockers.append("dependency_snapshot_mismatch")
        elif task.workspace_path:
            child_repository = envelope["spec"].get("repository")
            for dependency_id in declared_dependencies:
                dependency = kb.get_task(conn, dependency_id)
                if (
                    dependency is None
                    or dependency.status not in {"done", "archived"}
                    or not dependency.leaf_key
                ):
                    continue
                dependency_envelope, dependency_blockers = _load_envelope(dependency)
                if dependency_envelope is None or dependency_blockers:
                    blockers.append("dependency_candidate_missing")
                    continue
                if dependency_envelope["spec"].get("repository") != child_repository:
                    continue
                closeout = conn.execute(
                    "SELECT candidate_sha FROM workflow_run_closeout "
                    "WHERE task_id=? AND review_approved=1 AND invalidated_at IS NULL "
                    "ORDER BY run_id DESC LIMIT 1",
                    (dependency_id,),
                ).fetchone()
                if closeout is None:
                    blockers.append("dependency_candidate_missing")
                    continue
                included = _git_is_ancestor(
                    task.workspace_path,
                    str(closeout["candidate_sha"]),
                    str(task.pin_sha),
                )
                if included is False:
                    blockers.append("dependency_candidate_not_in_pin")
                elif included is None:
                    blockers.append("dependency_coordinate_unverifiable")

    if not task.workspace_path:
        blockers.append("workspace_missing")
    else:
        head = _git_head(task.workspace_path)
        if head is None:
            blockers.append("workspace_not_git")
        elif head != task.pin_sha:
            blockers.append("pin_sha_drift")
        if _git_branch(task.workspace_path) != task.branch_name:
            blockers.append("branch_mismatch")
        clean = _git_workspace_clean(task.workspace_path)
        if clean is None:
            blockers.append("workspace_inspection_failed")
        elif not clean:
            blockers.append("workspace_dirty")

        workspace_owner = conn.execute(
            "SELECT owner.id FROM tasks owner "
            "WHERE owner.id != ? AND owner.leaf_key IS NOT NULL "
            "AND owner.workspace_path = ? "
            "AND (owner.status IN ('running', 'review') OR EXISTS ("
            "SELECT 1 FROM task_runs run WHERE run.task_id = owner.id "
            "AND run.status IN ('running', 'reviewing') AND run.ended_at IS NULL"
            ")) LIMIT 1",
            (task_id, task.workspace_path),
        ).fetchone()
        if workspace_owner is not None:
            blockers.append("workspace_in_use")
        reservation = _workspace_reservation(task.workspace_path)
        if reservation is not None and not _reservation_owner_matches(
            reservation, conn, task_id
        ):
            blockers.append("workspace_in_use")

    if envelope is not None:
        spec = envelope["spec"]
        capsule = envelope["capsule"]
        if capsule.get("materialization") is not None:
            # Import lazily to keep the kernel usable without loading the
            # launcher seam. Production capsules are valid only while their
            # exact pinned file/blob inputs still match.
            from hermes_cli.kanban_workflow_runtime import _materialization_valid

            if not _materialization_valid(task, envelope):
                blockers.append("capsule_source_drift")
        if spec.get("human_gates"):
            blockers.append("human_gate_unresolved")

    return ReadinessResult(not blockers, tuple(dict.fromkeys(blockers)))


def claim_execution_leaf(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    dispatch_enabled: bool = False,
    expected_controller_epoch: Optional[str] = None,
) -> Optional[kb.Task]:
    if not dispatch_enabled:
        return None
    controller = get_workflow_controller_state(conn)
    if (
        not expected_controller_epoch
        or controller.controller_epoch != expected_controller_epoch
        or not controller.dispatch_enabled
        or not controller.broker_ready
    ):
        return None
    task = kb.get_task(conn, task_id)
    if task is None or not task.workspace_path:
        return None
    expected_workspace_path = task.workspace_path
    with _workspace_claim_lock(expected_workspace_path) as workspace_locked:
        if not workspace_locked:
            return None
        readiness = validate_execution_readiness(conn, task_id)
        if not readiness.ready:
            return None
        task = kb.get_task(conn, task_id)
        if task is None or task.workspace_path != expected_workspace_path:
            return None
        if task.status == "todo":
            with kb.write_txn(conn):
                promoted = conn.execute(
                    "UPDATE tasks SET status = 'ready' "
                    "WHERE id = ? AND status = 'todo' "
                    "AND leaf_key IS NOT NULL "
                    "AND workspace_path = ? "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_links l "
                    "  JOIN tasks p ON p.id = l.parent_id "
                    "  WHERE l.child_id = tasks.id "
                    "  AND p.status NOT IN ('done', 'archived')"
                    ")",
                    (task_id, expected_workspace_path),
                )
                if promoted.rowcount != 1:
                    return None
                kb._append_event(
                    conn,
                    task_id,
                    "execution_promoted",
                    {"source": "workflow-controller"},
                )
        claim_lock = kb._new_claim_lock()
        if not _acquire_workspace_reservation(
            conn,
            workspace_path=expected_workspace_path,
            task_id=task_id,
            claim_lock=claim_lock,
        ):
            return None
        try:
            claimed = kb.claim_task(
                conn,
                task_id,
                ttl_seconds=ttl_seconds,
                claimer=claim_lock,
                allow_execution_leaf=True,
                expected_controller_epoch=expected_controller_epoch,
                expected_workspace_path=expected_workspace_path,
            )
        except Exception:
            _release_workspace_reservation(
                conn,
                workspace_path=expected_workspace_path,
                task_id=task_id,
                claim_lock=claim_lock,
            )
            raise
        if claimed is None:
            _release_workspace_reservation(
                conn,
                workspace_path=expected_workspace_path,
                task_id=task_id,
                claim_lock=claim_lock,
            )
            return None
        if claimed.current_run_id is None or not _finalize_workspace_reservation(
            conn,
            workspace_path=expected_workspace_path,
            task_id=task_id,
            claim_lock=claim_lock,
            run_id=claimed.current_run_id,
        ):
            raise RuntimeError("workspace reservation lost during execution claim")
        return claimed


def supersede_execution_leaf(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
) -> bool:
    """Fence one active leaf version so a replacement version may register."""

    task = kb.get_task(conn, task_id)
    if task is None or not task.leaf_key or task.lease_policy != "evidence":
        return False
    if task.status in {"done", "archived"}:
        return False

    has_execution_ownership = bool(
        task.current_run_id or task.claim_lock or task.worker_pid is not None
    )
    termination = kb._terminate_reclaimed_worker(
        task.worker_pid,
        task.claim_lock,
    )
    if has_execution_ownership and not termination.get("terminated"):
        with kb.write_txn(conn):
            current = conn.execute(
                "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if current is not None and current["claim_lock"] == task.claim_lock:
                kb._append_event(
                    conn,
                    task_id,
                    "execution_supersession_deferred",
                    {
                        "reason": reason.strip() or None,
                        "termination": termination,
                    },
                    run_id=(
                        int(current["current_run_id"])
                        if current["current_run_id"] is not None
                        else None
                    ),
                )
        return False
    with kb.write_txn(conn):
        current = conn.execute(
            "SELECT status, current_run_id, claim_lock FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if current is None or current["status"] in {"done", "archived"}:
            return False
        if current["claim_lock"] != task.claim_lock:
            return False
        updated = conn.execute(
            "UPDATE tasks SET status = 'archived', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ? AND status = ? AND claim_lock IS ?",
            (task_id, current["status"], current["claim_lock"]),
        )
        if updated.rowcount != 1:
            return False
        run_id = current["current_run_id"]
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET status = 'released', outcome = 'superseded', "
                "summary = ?, metadata = ?, ended_at = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND ended_at IS NULL",
                (
                    reason.strip() or "superseded by material specification edit",
                    _canonical_json(termination),
                    int(time.time()),
                    int(run_id),
                ),
            )
        kb._append_event(
            conn,
            task_id,
            "execution_superseded",
            {
                "reason": reason.strip() or None,
                "termination": termination,
            },
            run_id=int(run_id) if run_id is not None else None,
        )
    return True


def _run_identity_matches(task: sqlite3.Row, run: sqlite3.Row) -> bool:
    return (
        all(
            run[field] == task[field]
            for field in (
                "leaf_key",
                "leaf_family_key",
                "spec_hash",
                "pin_sha",
                "capsule_hash",
            )
        )
        and run["claim_lock"] == task["claim_lock"]
    )


def reconcile_execution_leaves(
    conn: sqlite3.Connection,
    *,
    process_alive: Optional[Callable[[int], bool]] = None,
) -> ReconciliationReport:
    """Deterministically adopt one current attempt or quarantine ambiguity.

    Dead, non-current active runs are closed as fenced orphans. Any current
    attempt whose worker survival or identity cannot be resolved remains owned
    and is reported for operator intervention; reconciliation never treats
    uncertainty as proof that the claim can be released.
    """

    alive = process_alive or kb._pid_alive
    adopted: list[str] = []
    closed: list[int] = []
    quarantined: list[str] = []
    findings: list[str] = []
    now = int(time.time())
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE leaf_key IS NOT NULL ORDER BY created_at, id"
    ).fetchall()

    for task in tasks:
        active_runs = conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? "
            "AND status IN ('running', 'reviewing') AND ended_at IS NULL "
            "ORDER BY id",
            (task["id"],),
        ).fetchall()
        current = next(
            (run for run in active_runs if run["id"] == task["current_run_id"]),
            None,
        )
        task_active = task["status"] in ("running", "review")
        reasons: list[str] = []
        ownership_unresolved = False
        reservation = (
            _workspace_reservation(task["workspace_path"])
            if task["workspace_path"]
            else None
        )
        if task_active:
            if reservation is None:
                reasons.append("workspace_reservation_missing")
                ownership_unresolved = True
            elif not _reservation_owner_matches(reservation, conn, task["id"]):
                reasons.append("workspace_reservation_conflict")
                ownership_unresolved = True
            elif reservation["run_id"] is None and task["current_run_id"] is not None:
                _finalize_workspace_reservation(
                    conn,
                    workspace_path=task["workspace_path"],
                    task_id=task["id"],
                    claim_lock=reservation["claim_lock"],
                    run_id=int(task["current_run_id"]),
                )
        elif reservation is not None and _reservation_owner_matches(
            reservation, conn, task["id"]
        ):
            reserved_run = (
                conn.execute(
                    "SELECT worker_pid FROM task_runs WHERE id = ? AND task_id = ?",
                    (int(reservation["run_id"]), task["id"]),
                ).fetchone()
                if reservation["run_id"] is not None
                else None
            )
            reserved_pid = (
                reserved_run["worker_pid"] if reserved_run is not None else None
            )
            if reserved_pid is None:
                reasons.append("missing_worker_identity")
                ownership_unresolved = True
            elif alive(int(reserved_pid)):
                reasons.append("terminal_worker_still_alive")
                ownership_unresolved = True
            else:
                _release_workspace_reservation(
                    conn,
                    workspace_path=task["workspace_path"],
                    task_id=task["id"],
                    claim_lock=reservation["claim_lock"],
                )
        if task_active:
            if current is None:
                reasons.append("missing_current_run")
                ownership_unresolved = True
            elif not _run_identity_matches(task, current):
                reasons.append("identity_mismatch")
                ownership_unresolved = True
            elif current["worker_pid"] is None:
                reasons.append("missing_worker_identity")
                ownership_unresolved = True
            elif alive(int(current["worker_pid"])) and (
                current["claim_expires"] is None or int(current["claim_expires"]) < now
            ):
                reasons.append("claim_expired")
                ownership_unresolved = True
            # A positively dead current worker is not itself a terminal
            # outcome. The runtime tick still owns its bounded result proposal
            # and must ingest that before closeout; a missing proposal remains
            # fenced until normal evidence-lease expiry handles it.
        elif current is not None:
            pid = current["worker_pid"]
            if pid is not None and alive(int(pid)):
                reasons.append("non_active_live_current_run")
                ownership_unresolved = True
            else:
                reasons.append("non_active_current_run")

        orphan_runs = [
            run for run in active_runs if current is None or run["id"] != current["id"]
        ]
        for orphan in orphan_runs:
            pid = orphan["worker_pid"]
            if pid is not None and alive(int(pid)):
                termination = kb._terminate_reclaimed_worker(
                    int(pid),
                    orphan["claim_lock"],
                )
                if not termination.get("terminated"):
                    reasons.append("live_orphan_run")
                    ownership_unresolved = True
                    continue
            with write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET status = 'reclaimed', ended_at = ?, "
                    "outcome = 'restart_reconciled_orphan' "
                    "WHERE id = ? AND status IN ('running', 'reviewing') "
                    "AND ended_at IS NULL",
                    (int(time.time()), int(orphan["id"])),
                )
            closed.append(int(orphan["id"]))

        if reasons:
            if ownership_unresolved:
                findings.extend(sorted(set(reasons)))
                continue
            reason = "Workflow v1 reconciliation: " + ", ".join(sorted(set(reasons)))
            expected_run_id = int(current["id"]) if current is not None else None
            blocked = kb.block_task(
                conn,
                task["id"],
                reason=reason,
                kind="capability",
                expected_run_id=expected_run_id,
                expected_claim_lock=(
                    current["claim_lock"] if current is not None else None
                ),
                force_execution_admin=True,
            )
            if blocked:
                with write_txn(conn):
                    kb._append_event(
                        conn,
                        task["id"],
                        "execution_quarantined",
                        {"reasons": sorted(set(reasons))},
                        run_id=expected_run_id,
                    )
                quarantined.append(task["id"])
                findings.extend(reasons)
        elif task_active and current is not None:
            adopted.append(task["id"])

    return ReconciliationReport(
        tuple(adopted),
        tuple(closed),
        tuple(quarantined),
        tuple(dict.fromkeys(findings)),
    )


def run_workflow_controller_tick(
    conn: sqlite3.Connection,
    *,
    controller_epoch: str,
    process_alive: Optional[Callable[[int], bool]] = None,
) -> ReconciliationReport:
    """Run one persisted reconciliation tick for the remote gateway epoch."""

    epoch = _required_text("controller_epoch", controller_epoch)
    now = int(time.time())
    with write_txn(conn):
        current = conn.execute(
            "SELECT controller_epoch FROM workflow_controller_state WHERE singleton = 1"
        ).fetchone()
        if current is None or current["controller_epoch"] != epoch:
            raise WorkflowControlConflict("workflow controller epoch is stale")
        conn.execute(
            "UPDATE workflow_controller_state SET status = 'reconciling', "
            "heartbeat_at = ?, updated_at = ? "
            "WHERE singleton = 1 AND controller_epoch = ?",
            (now, now, epoch),
        )

    try:
        kb.release_stale_claims(conn, execution_only=True)
        kb.enforce_max_runtime(conn, execution_only=True)
        report = reconcile_execution_leaves(conn, process_alive=process_alive)
        # Reserved attempts live before the legacy running-state projection and
        # therefore need their own restart seam. Missing PID is ambiguity, not
        # positive death evidence; preserve its fence and workspace reservation.
        from hermes_cli.kanban_workflow_runtime import reconcile_runtime_reservations

        reserved_report = reconcile_runtime_reservations(
            conn, process_alive=process_alive
        )
        report = ReconciliationReport(
            tuple(
                dict.fromkeys(
                    report.adopted_task_ids + reserved_report.adopted_task_ids
                )
            ),
            tuple(
                dict.fromkeys(
                    report.closed_orphan_run_ids + reserved_report.closed_orphan_run_ids
                )
            ),
            tuple(
                dict.fromkeys(
                    report.quarantined_task_ids + reserved_report.quarantined_task_ids
                )
            ),
            tuple(dict.fromkeys(report.findings + reserved_report.findings)),
        )
    except Exception as exc:
        failed_at = int(time.time())
        with write_txn(conn):
            conn.execute(
                "UPDATE workflow_controller_state SET status = 'degraded', "
                "heartbeat_at = ?, last_error = ?, updated_at = ? "
                "WHERE singleton = 1 AND controller_epoch = ?",
                (failed_at, str(exc)[:1000], failed_at, epoch),
            )
        raise

    reconciled_at = int(time.time())
    summary = ", ".join(report.findings) if report.findings else None
    with write_txn(conn):
        updated = conn.execute(
            "UPDATE workflow_controller_state SET status = ?, heartbeat_at = ?, "
            "last_reconciled_at = ?, last_error = ?, updated_at = ? "
            "WHERE singleton = 1 AND controller_epoch = ?",
            (
                "degraded" if report.findings else "healthy",
                reconciled_at,
                reconciled_at,
                summary,
                reconciled_at,
                epoch,
            ),
        )
        if updated.rowcount != 1:
            raise WorkflowControlConflict(
                "workflow controller epoch changed during tick"
            )
    return report
