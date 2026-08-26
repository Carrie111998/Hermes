"""Fail-closed retention for Hermes Kanban worker workspaces.

This module is deliberately runnable out-of-process.  It never needs the
Gateway process and therefore can be installed behind a separate timer.
Deletion is exact-path, oldest-first, bounded, and conditional on complete
inventory plus live-process, filesystem, and Git proof.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import errno
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from hermes_cli import kanban_db as kb

GIB = 1024 ** 3
VERSION = 1
RECOVERY_RUN_STATES = {
    "blocked", "crashed", "failed", "interrupted", "gave_up", "timed_out",
    "cancelled", "unknown", "reclaimed", "stale",
}
TERMINAL_TASK_STATES = {"done", "archived", "failed", "cancelled"}
ACTIVE_TASK_STATES = {"triage", "todo", "ready", "running", "review"}
SECRET_WORDS = ("token", "secret", "password", "credential", "api_key", "authorization")
RECEIPT_METADATA_KEY_WORDS = (
    "artifact", "evidence", "callback", "log", "session", "commit", "branch",
    "test", "verification",
)


@dataclasses.dataclass(frozen=True)
class Policy:
    completed_ttl_seconds: int = 6 * 3600
    recovery_ttl_seconds: int = 72 * 3600
    active_heartbeat_seconds: int = 15 * 60
    workspace_cap_bytes: int = 50 * GIB
    workspace_release_bytes: int = 40 * GIB
    free_floor_bytes: int = 25 * GIB
    free_release_bytes: int = 30 * GIB
    max_removals: int = 64
    max_reclaimed_bytes: int = 32 * GIB
    lsof_timeout_seconds: int = 15


@dataclasses.dataclass
class TreeInfo:
    bytes: int = 0
    dev: int = 0
    ino: int = 0
    git_roots: list[Path] = dataclasses.field(default_factory=list)
    reason: Optional[str] = None


@dataclasses.dataclass
class Candidate:
    board: str
    db_path: Path
    root: Path
    task_id: str
    status: str
    kind: str
    path: Path
    branch_name: Optional[str]
    terminal_at: int
    latest_run_id: Optional[int]
    latest_run_status: Optional[str]
    latest_run_outcome: Optional[str]
    size: int = 0
    dev: int = 0
    ino: int = 0
    git_roots: list[Path] = dataclasses.field(default_factory=list)
    receipt_evidence: dict[str, Any] = dataclasses.field(default_factory=dict)


class RetentionError(RuntimeError):
    pass


class LockBusy(RetentionError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _run(args: list[str], *, cwd: Optional[Path] = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


def _activity_probe(path: Path, timeout: int) -> tuple[bool, Optional[str]]:
    """Return (active, uncertainty). Any uncertainty must preserve the tree."""
    lsof = shutil.which("lsof")
    if lsof:
        try:
            result = _run([lsof, "-n", "-P", "+D", str(path)], timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "activity_probe_timeout"
        # lsof returns 1 when no matching open file exists.
        if result.returncode == 0 and result.stdout.strip():
            return True, None
        if result.returncode == 1 and result.stderr.strip():
            return False, "activity_probe_failed"
        if result.returncode not in (0, 1):
            return False, "activity_probe_failed"
        return False, None

    proc = Path("/proc")
    if proc.is_dir():
        try:
            target = path.resolve()
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                links = [entry / "cwd"]
                fd_dir = entry / "fd"
                with contextlib.suppress(OSError):
                    links.extend(fd_dir.iterdir())
                for link in links:
                    try:
                        resolved = link.resolve(strict=True)
                        if resolved == target or resolved.is_relative_to(target):
                            return True, None
                    except (OSError, ValueError):
                        continue
            return False, None
        except OSError:
            return False, "activity_probe_failed"
    return False, "activity_probe_unavailable"


def _inspect_tree(path: Path, root: Path) -> TreeInfo:
    """Inventory without following links; reject escape links and nested mounts."""
    info = TreeInfo()
    try:
        root_real = root.resolve(strict=True)
        path_lstat = path.lstat()
        if stat.S_ISLNK(path_lstat.st_mode) or not stat.S_ISDIR(path_lstat.st_mode):
            info.reason = "workspace_not_real_directory"
            return info
        path_real = path.resolve(strict=True)
        if path_real == root_real or not path_real.is_relative_to(root_real):
            info.reason = "containment"
            return info
        if path.parent.resolve(strict=True) != root_real or path.name == "":
            info.reason = "path_identity"
            return info
        if os.path.ismount(path):
            info.reason = "mount_boundary"
            return info
        if not os.access(path.parent, os.W_OK | os.X_OK) or not os.access(path, os.W_OK | os.X_OK):
            info.reason = "delete_permission"
            return info
        info.dev, info.ino = path_lstat.st_dev, path_lstat.st_ino
        stack = [path]
        seen_git: set[Path] = set()
        while stack:
            current = stack.pop()
            for entry in os.scandir(current):
                st = entry.stat(follow_symlinks=False)
                info.bytes += st.st_blocks * 512
                p = Path(entry.path)
                immutable = getattr(stat, "UF_IMMUTABLE", 0) | getattr(stat, "SF_IMMUTABLE", 0)
                if immutable and getattr(st, "st_flags", 0) & immutable:
                    info.reason = "immutable_entry"
                    return info
                if stat.S_ISLNK(st.st_mode):
                    try:
                        target = p.resolve(strict=False)
                        if not target.is_relative_to(path_real):
                            info.reason = "symlink_escape"
                            return info
                    except (OSError, ValueError):
                        info.reason = "symlink_unresolved"
                        return info
                    continue
                if st.st_dev != info.dev or (stat.S_ISDIR(st.st_mode) and os.path.ismount(p)):
                    info.reason = "nested_mount"
                    return info
                if stat.S_ISDIR(st.st_mode):
                    if not os.access(p, os.W_OK | os.X_OK):
                        info.reason = "delete_permission"
                        return info
                    if entry.name == ".git":
                        owner = p.parent
                        if owner not in seen_git:
                            seen_git.add(owner)
                            info.git_roots.append(owner)
                        continue
                    stack.append(p)
                elif entry.name == ".git":
                    owner = p.parent
                    if owner not in seen_git:
                        seen_git.add(owner)
                        info.git_roots.append(owner)
        return info
    except OSError:
        info.reason = "inventory_io_error"
        return info


def _git_check(repo: Path) -> tuple[bool, str, dict[str, Any]]:
    status = _run(["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"])
    if status.returncode != 0:
        return False, "git_error", {}
    lines = [line for line in status.stdout.splitlines() if line]
    if lines:
        if any(line.startswith("??") for line in lines):
            return False, "git_untracked", {}
        return False, "git_dirty", {}
    symbolic = _run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"])
    if symbolic.returncode != 0 or not symbolic.stdout.strip():
        return False, "git_detached", {}
    remote = _run(["git", "-C", str(repo), "remote"])
    if remote.returncode != 0 or not remote.stdout.strip():
        return False, "git_no_remote", {}
    contains = _run(["git", "-C", str(repo), "branch", "-r", "--contains", "HEAD"])
    if contains.returncode != 0:
        return False, "git_error", {}
    refs = [line.strip() for line in contains.stdout.splitlines() if line.strip() and "->" not in line]
    if not refs:
        return False, "git_unpushed", {}
    return True, "ok", {"named_ref": True, "remote_reachable": True, "remote_ref_count": len(refs)}


def _worktree_owner(path: Path) -> tuple[Optional[Path], str]:
    common = _run(["git", "-C", str(path), "rev-parse", "--git-common-dir"])
    if common.returncode != 0 or not common.stdout.strip():
        return None, "worktree_owner_unknown"
    common_path = Path(common.stdout.strip())
    if not common_path.is_absolute():
        common_path = (path / common_path).resolve(strict=False)
    owner = common_path.parent
    listing = _run(["git", "-C", str(owner), "worktree", "list", "--porcelain"])
    if listing.returncode != 0:
        return None, "worktree_registration_unknown"
    registered = []
    for line in listing.stdout.splitlines():
        if line.startswith("worktree "):
            with contextlib.suppress(OSError):
                registered.append(Path(line[9:]).resolve(strict=True))
    try:
        exact = path.resolve(strict=True)
    except OSError:
        return None, "worktree_path_changed"
    if exact == owner.resolve(strict=False):
        return None, "main_checkout"
    if exact not in registered:
        return None, "worktree_unregistered"
    return owner, "ok"


def _remove_tree_exact(path: Path, root: Path, expected_dev: int, expected_ino: int) -> tuple[bool, str]:
    """Exact bottom-up unlink/rmdir. Never follows links or crosses devices."""
    check = _inspect_tree(path, root)
    if check.reason:
        return False, check.reason
    if (check.dev, check.ino) != (expected_dev, expected_ino):
        return False, "changed_path_identity"
    mutated = False
    try:
        for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
            cur = Path(current)
            for name in files:
                child = cur / name
                st = child.lstat()
                if st.st_dev != expected_dev and not stat.S_ISLNK(st.st_mode):
                    return False, "partial_removal" if mutated else "nested_mount"
                child.unlink()
                mutated = True
            for name in dirs:
                child = cur / name
                st = child.lstat()
                if stat.S_ISLNK(st.st_mode):
                    child.unlink()
                    mutated = True
                else:
                    if st.st_dev != expected_dev or os.path.ismount(child):
                        return False, "partial_removal" if mutated else "nested_mount"
                    child.rmdir()
                    mutated = True
        path.rmdir()
        return (not path.exists()), "removed" if not path.exists() else "partial_removal"
    except OSError:
        return False, "partial_removal"


def remove_exact_tree_for_lifecycle(path: Path, root: Path) -> tuple[bool, str]:
    """Narrow helper used by the immediate completion hook."""
    inspected = _inspect_tree(path, root)
    if inspected.reason:
        return False, inspected.reason
    return _remove_tree_exact(path, root, inspected.dev, inspected.ino)


def _board_specs(home: Path) -> list[tuple[str, Path, Path]]:
    specs: list[tuple[str, Path, Path]] = []
    default_db = home / "kanban.db"
    if default_db.is_file():
        override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
        default_root = Path(override).expanduser() if override else home / "kanban" / "workspaces"
        specs.append((kb.DEFAULT_BOARD, default_db, default_root))
    boards = home / "kanban" / "boards"
    if boards.is_dir():
        for entry in sorted(boards.iterdir(), key=lambda p: p.name):
            db = entry / "kanban.db"
            if entry.is_dir() and db.is_file() and not entry.name.startswith("_"):
                specs.append((entry.name, db, entry / "workspaces"))
    return specs


def _latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id,status,outcome,started_at,ended_at,last_heartbeat_at,worker_pid,claim_expires "
        "FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()


def _terminal_class(row: sqlite3.Row, run: Optional[sqlite3.Row]) -> tuple[Optional[str], int]:
    status = str(row["status"] or "")
    if status in {"done", "archived"}:
        return "completed", int(row["completed_at"] or (run["ended_at"] if run else 0) or row["created_at"] or 0)
    if status in {"failed", "cancelled"}:
        return "recovery", int(row["completed_at"] or (run["ended_at"] if run else 0) or row["last_heartbeat_at"] or row["created_at"] or 0)
    run_status = str((run["status"] if run else "") or "")
    run_outcome = str((run["outcome"] if run else "") or "")
    # Production persists separate run status and outcome vocabularies. Match
    # both; neither is a fallback for the other.
    if (
        status == "blocked"
        and run is not None
        and run["ended_at"] is not None
        and (run_status in RECOVERY_RUN_STATES or run_outcome in RECOVERY_RUN_STATES)
    ):
        return "recovery", int(run["ended_at"] or row["last_heartbeat_at"] or row["created_at"] or 0)
    return None, 0


def _receipt_evidence(
    conn: sqlite3.Connection, task_id: str, run: Optional[sqlite3.Row]
) -> dict[str, Any]:
    """Summarize durable board evidence without copying values or paths."""
    event_rows = conn.execute(
        "SELECT kind,COUNT(*) AS n FROM task_events WHERE task_id=? GROUP BY kind",
        (task_id,),
    ).fetchall()
    event_counts = {str(row["kind"]): int(row["n"]) for row in event_rows}
    task_row = conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()
    metadata_keys: list[str] = []
    if run is not None:
        metadata_row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id=?", (run["id"],)
        ).fetchone()
        try:
            parsed = json.loads(metadata_row["metadata"] or "{}") if metadata_row else {}
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            metadata_keys = sorted(
                key for key in parsed
                if isinstance(key, str)
                and not any(secret in key.lower() for secret in SECRET_WORDS)
                and any(allowed in key.lower() for allowed in RECEIPT_METADATA_KEY_WORDS)
            )[:32]
    names = " ".join([*event_counts.keys(), *metadata_keys]).lower()
    return {
        "result_present": bool(task_row and task_row["result"]),
        "callback_receipt_in_board": "callback" in names or "delivered" in names,
        "evidence_receipt_in_board": "evidence" in names or "artifact" in names or "completed" in event_counts,
        "event_counts": event_counts,
        "run_metadata_keys": metadata_keys,
    }


def _receipt_path(home: Path, candidate: Candidate) -> Path:
    key = str(candidate.latest_run_id or candidate.terminal_at or "terminal")
    return home / "kanban" / "retention" / "receipts" / f"{candidate.task_id}-{key}.json"


def _write_receipt(home: Path, candidate: Candidate, git_proofs: list[dict[str, Any]]) -> tuple[bool, bool]:
    path = _receipt_path(home, candidate)
    if path.exists():
        existing = _read_json(path)
        if existing.get("task_id") == candidate.task_id and existing.get("run_id") == candidate.latest_run_id:
            return True, False
        return False, False
    payload = {
        "version": VERSION,
        "task_id": candidate.task_id,
        "run_id": candidate.latest_run_id,
        "terminal_state": candidate.status,
        "terminal_at": candidate.terminal_at,
        "workspace_bytes": candidate.size,
        "git": git_proofs,
        "preserved": {
            "branch_or_ref_proof": bool(git_proofs),
            **candidate.receipt_evidence,
            "logs_retained_by_log_policy": True,
        },
    }
    try:
        _atomic_json(path, payload)
        return True, True
    except OSError:
        return False, False


def _policy_dict(policy: Policy) -> dict[str, int]:
    return dataclasses.asdict(policy)


def sweep(
    *,
    home: Path,
    policy: Policy,
    apply: bool,
    now: Optional[int] = None,
    activity_probe: Callable[[Path, int], tuple[bool, Optional[str]]] = _activity_probe,
    free_probe: Callable[[Path], int] = _free_bytes,
    hold_lock: bool = True,
) -> tuple[dict[str, Any], int]:
    now = int(now or time.time())
    run_id = uuid.uuid4().hex
    state_path = home / "kanban" / "retention" / "state.json"
    lock_path = home / "kanban" / "retention.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+")
    if hold_lock:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                report = {"version": VERSION, "run_id": run_id, "healthy": False,
                          "lock_state": "overlap", "error": "retention_lock_busy"}
                return report, 3
            raise

    previous = _read_json(state_path)
    skipped: Counter[str] = Counter()
    candidates: list[Candidate] = []
    gate_blocked_eligible = 0
    gate_blocked_bytes = 0
    scanned = 0
    workspace_before = 0
    inventory_partial = False
    free_before = free_probe(home)
    specs = _board_specs(home)
    if not specs:
        inventory_partial = True
        skipped["no_boards"] += 1

    try:
        for board, db_path, root in specs:
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id,status,workspace_kind,workspace_path,branch_name,created_at,completed_at,"
                    "last_heartbeat_at,worker_pid,current_run_id,claim_expires FROM tasks "
                    "WHERE workspace_path IS NOT NULL ORDER BY id"
                ).fetchall()
            except sqlite3.Error:
                inventory_partial = True
                skipped["db_read_error"] += 1
                continue
            try:
                for row in rows:
                    path = Path(row["workspace_path"]).expanduser()
                    if not path.exists():
                        continue
                    scanned += 1
                    run = _latest_run(conn, row["id"])
                    terminal_class, terminal_at = _terminal_class(row, run)
                    ttl = (
                        policy.completed_ttl_seconds
                        if terminal_class == "completed"
                        else policy.recovery_ttl_seconds
                    )
                    expired_terminal = bool(
                        terminal_class is not None
                        and terminal_at > 0
                        and now - terminal_at >= ttl
                    )
                    scan_root = root
                    precheck_reason: Optional[str] = None
                    if (row["workspace_kind"] or "scratch") == "worktree":
                        if "CloudStorage" in path.parts:
                            precheck_reason = "cloudstorage_refused"
                        else:
                            owner, owner_reason = _worktree_owner(path)
                            if owner is None:
                                precheck_reason = owner_reason
                            else:
                                scan_root = path.parent
                    inspected = (
                        TreeInfo(reason=precheck_reason)
                        if precheck_reason
                        else _inspect_tree(path, scan_root)
                    )
                    workspace_before += inspected.bytes
                    if inspected.reason:
                        skipped[inspected.reason] += 1
                        if expired_terminal:
                            gate_blocked_eligible += 1
                            gate_blocked_bytes += inspected.bytes
                        if inspected.reason in {"inventory_io_error"}:
                            inventory_partial = True
                        continue
                    if terminal_class is None:
                        skipped["nonterminal"] += 1
                        continue
                    if terminal_at <= 0 or now - terminal_at < ttl:
                        skipped["ttl"] += 1
                        continue
                    if row["current_run_id"] is not None or _pid_alive(row["worker_pid"]):
                        skipped["active_task"] += 1
                        continue
                    if row["claim_expires"] and int(row["claim_expires"]) > now:
                        skipped["active_lease"] += 1
                        continue
                    if row["last_heartbeat_at"] and now - int(row["last_heartbeat_at"]) < policy.active_heartbeat_seconds:
                        skipped["active_heartbeat"] += 1
                        continue
                    if run and (run["ended_at"] is None or _pid_alive(run["worker_pid"]) or
                                (run["claim_expires"] and int(run["claim_expires"]) > now)):
                        skipped["active_run"] += 1
                        continue
                    candidates.append(Candidate(
                        board=board, db_path=db_path, root=scan_root, task_id=row["id"], status=row["status"],
                        kind=row["workspace_kind"] or "scratch", path=path,
                        branch_name=row["branch_name"], terminal_at=terminal_at,
                        latest_run_id=int(run["id"]) if run else None,
                        latest_run_status=str(run["status"]) if run else None,
                        latest_run_outcome=str(run["outcome"]) if run and run["outcome"] else None,
                        size=inspected.bytes, dev=inspected.dev, ino=inspected.ino,
                        git_roots=inspected.git_roots,
                        receipt_evidence=_receipt_evidence(conn, row["id"], run),
                    ))
            finally:
                conn.close()
    except Exception:
        inventory_partial = True
        skipped["inventory_exception"] += 1

    candidates.sort(key=lambda c: (c.terminal_at, c.task_id, c.board))
    eligible = len(candidates) + gate_blocked_eligible
    removed = 0
    reclaimed = 0
    receipt_created = 0
    removed_ids: set[tuple[str, str]] = set()

    for candidate in candidates:
        if removed >= policy.max_removals or reclaimed >= policy.max_reclaimed_bytes:
            skipped["sweep_bound"] += 1
            continue
        active, uncertainty = activity_probe(candidate.path, policy.lsof_timeout_seconds)
        if uncertainty:
            skipped[uncertainty] += 1
            inventory_partial = True
            continue
        if active:
            skipped["open_fd_or_cwd"] += 1
            continue
        git_proofs: list[dict[str, Any]] = []
        git_ok = True
        git_reason = "ok"
        for repo in candidate.git_roots:
            ok, reason, proof = _git_check(repo)
            if not ok:
                git_ok, git_reason = False, reason
                break
            git_proofs.append(proof)
        if not git_ok:
            skipped[git_reason] += 1
            continue
        owner: Optional[Path] = None
        if candidate.kind == "worktree":
            owner, reason = _worktree_owner(candidate.path)
            if owner is None:
                skipped[reason] += 1
                continue
            if not candidate.git_roots:
                ok, reason, proof = _git_check(candidate.path)
                if not ok:
                    skipped[reason] += 1
                    continue
                git_proofs.append(proof)
        if not apply:
            skipped["dry_run"] += 1
            continue
        ok_receipt, created = _write_receipt(home, candidate, git_proofs)
        if not ok_receipt:
            skipped["receipt_failure"] += 1
            inventory_partial = True
            continue
        receipt_created += int(created)
        # Recheck activity and exact path identity immediately before mutation.
        active, uncertainty = activity_probe(candidate.path, policy.lsof_timeout_seconds)
        if uncertainty:
            skipped[uncertainty] += 1
            inventory_partial = True
            continue
        if active:
            skipped["open_fd_or_cwd"] += 1
            continue
        inspected = _inspect_tree(candidate.path, candidate.root)
        if inspected.reason:
            skipped[inspected.reason] += 1
            continue
        if (inspected.dev, inspected.ino) != (candidate.dev, candidate.ino):
            skipped["changed_path_identity"] += 1
            continue
        if candidate.kind == "worktree":
            result = _run(["git", "-C", str(owner), "worktree", "remove", str(candidate.path)], timeout=60)
            if result.returncode != 0 or candidate.path.exists():
                skipped["partial_removal"] += 1
                continue
            reason = "removed"
            success = True
        elif candidate.kind == "scratch":
            success, reason = _remove_tree_exact(candidate.path, candidate.root, candidate.dev, candidate.ino)
        else:
            skipped["preserved_workspace_kind"] += 1
            continue
        if not success:
            skipped[reason] += 1
            continue
        removed += 1
        reclaimed += candidate.size
        removed_ids.add((candidate.board, candidate.task_id))

    remaining_eligible = gate_blocked_eligible + sum(
        1 for c in candidates if (c.board, c.task_id) not in removed_ids
    )
    backlog_bytes = gate_blocked_bytes + sum(
        c.size for c in candidates if (c.board, c.task_id) not in removed_ids
    )
    free_after = free_probe(home)
    workspace_after = max(0, workspace_before - reclaimed)
    prior_pressure = bool(previous.get("pressure_active"))
    breached = workspace_before > policy.workspace_cap_bytes or free_before < policy.free_floor_bytes
    released = workspace_after <= policy.workspace_release_bytes and free_after >= policy.free_release_bytes
    pressure_active = (prior_pressure or breached) and not released
    prior_after = int(previous.get("workspace_bytes_after") or 0)
    growth = max(0, workspace_before - prior_after) if prior_after else 0
    growth_streak = int(previous.get("growth_streak") or 0)
    if growth > reclaimed:
        growth_streak += 1
    else:
        growth_streak = 0
    repeated_growth = growth_streak >= 2
    unhealthy_reasons = []
    if inventory_partial:
        unhealthy_reasons.append("inventory_partial")
    if remaining_eligible > 0:
        unhealthy_reasons.append("eligible_backlog")
    if free_after < policy.free_floor_bytes:
        unhealthy_reasons.append("free_floor_missed")
    if repeated_growth:
        unhealthy_reasons.append("repeated_growth_exceeds_cleanup")
    if skipped.get("partial_removal"):
        unhealthy_reasons.append("partial_removal")
    report = {
        "version": VERSION, "run_id": run_id, "apply": apply,
        "healthy": not unhealthy_reasons, "unhealthy_reasons": unhealthy_reasons,
        "lock_state": "held", "scanned": scanned, "eligible": eligible,
        "removed": removed, "skipped_by_reason": dict(sorted(skipped.items())),
        "bytes_reclaimed": reclaimed, "free_before": free_before, "free_after": free_after,
        "workspace_bytes_before": workspace_before, "workspace_bytes_after": workspace_after,
        "terminal_backlog_count": remaining_eligible, "terminal_backlog_bytes": backlog_bytes,
        "inventory_partial": inventory_partial,
        "pressure_active": pressure_active, "repeated_growth": repeated_growth,
        "growth_bytes": growth, "growth_streak": growth_streak,
        "receipts_created": receipt_created, "policy": _policy_dict(policy),
    }
    state = {
        "version": VERSION, "last_run_id": run_id, "workspace_bytes_after": workspace_after,
        "free_after": free_after, "pressure_active": pressure_active,
        "growth_streak": growth_streak, "last_healthy": report["healthy"],
    }
    if apply:
        try:
            _atomic_json(state_path, state)
        except OSError:
            report["healthy"] = False
            report["inventory_partial"] = True
            report["unhealthy_reasons"] = sorted(set(report["unhealthy_reasons"] + ["state_write_failure"]))
    if hold_lock:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    lock_handle.close()
    return report, 0 if report["healthy"] else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fail-closed Kanban workspace retention")
    p.add_argument("--apply", action="store_true", help="Remove independently eligible exact paths")
    p.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    p.add_argument("--completed-ttl-hours", type=int, default=6)
    p.add_argument("--recovery-ttl-hours", type=int, default=72)
    p.add_argument("--active-heartbeat-minutes", type=int, default=15)
    p.add_argument("--workspace-cap-gib", type=int, default=50)
    p.add_argument("--workspace-release-gib", type=int, default=40)
    p.add_argument("--free-floor-gib", type=int, default=25)
    p.add_argument("--free-release-gib", type=int, default=30)
    p.add_argument("--max-removals", type=int, default=64)
    p.add_argument("--max-reclaimed-gib", type=int, default=32)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    home = (args.home or kb.kanban_home()).expanduser().resolve(strict=False)
    policy = Policy(
        completed_ttl_seconds=max(1, args.completed_ttl_hours) * 3600,
        recovery_ttl_seconds=max(1, args.recovery_ttl_hours) * 3600,
        active_heartbeat_seconds=max(1, args.active_heartbeat_minutes) * 60,
        workspace_cap_bytes=max(1, args.workspace_cap_gib) * GIB,
        workspace_release_bytes=max(1, args.workspace_release_gib) * GIB,
        free_floor_bytes=max(1, args.free_floor_gib) * GIB,
        free_release_bytes=max(1, args.free_release_gib) * GIB,
        max_removals=max(1, args.max_removals),
        max_reclaimed_bytes=max(1, args.max_reclaimed_gib) * GIB,
    )
    report, code = sweep(home=home, policy=policy, apply=bool(args.apply))
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
