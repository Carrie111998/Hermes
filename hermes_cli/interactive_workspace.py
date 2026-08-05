"""Native interactive browser-session start for a Project-linked Kanban task.

This is deliberately an edge service rather than a model tool.  It composes the
existing Project, Kanban workspace and Session stores without claiming a task or
creating an autonomous run.  The durable task event is both the lifecycle
receipt and the idempotency record used after browser retries or process restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home
from hermes_state import SessionDB

from hermes_cli import kanban_db as kdb
from hermes_cli import projects_db as pdb
from hermes_cli import web_git


_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,240}$")
_START_LOCKS: dict[str, threading.Lock] = {}
_START_LOCKS_GUARD = threading.Lock()
_PREFLIGHT_CONFIG = Path(".hermes/workspace-start.json")
_MAX_PREFLIGHT_OUTPUT = 2_000


class InteractiveWorkspaceError(RuntimeError):
    """Bounded, owner-visible workspace-start failure."""

    def __init__(self, code: str, message: str, *, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


@dataclass(frozen=True)
class InteractiveWorkspaceRequest:
    project_id: str
    task_id: str
    workstream_id: str
    idempotency_key: str
    write_scope: str
    profile_name: str = ""


@dataclass(frozen=True)
class InteractiveWorkspaceResult:
    project_id: str
    task_id: str
    workstream_id: str
    session_id: str
    repo_root: str
    workspace_path: str
    branch: str
    base_ref: str
    preflight_status: str
    preflight_summary: str
    reused: bool = False


@dataclass(frozen=True)
class InteractiveWorkspaceConnectedResult:
    project_id: str
    task_id: str
    workstream_id: str
    session_id: str
    reused: bool = False


@dataclass(frozen=True)
class _PreparedWorktree:
    repo_root: Path
    path: Path
    branch: str
    base_ref: str
    created_worktree: bool
    created_branch: bool


def _start_lock(key: str) -> threading.Lock:
    with _START_LOCKS_GUARD:
        return _START_LOCKS.setdefault(key, threading.Lock())


@contextmanager
def _cross_process_start_lock(lock_path: Path):
    """Serialize one start intent across dashboard processes.

    The file is only an inode anchor; the kernel owns lock liveness, so a
    crashed process cannot leave a stale logical lock behind. The process-local
    lock remains the first layer because POSIX ``flock`` alone does not
    serialize sibling threads reliably.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        lock_path.parent.chmod(0o700)
    except OSError:
        pass
    with lock_path.open("a+b") as lock_file:
        try:
            lock_path.chmod(0o600)
        except OSError:
            pass
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        else:
            # Match Hermes' existing Windows cross-process lock strategy.
            import portalocker

            portalocker.lock(lock_file, portalocker.LOCK_EX)
            try:
                yield
            finally:
                portalocker.unlock(lock_file)


@contextmanager
def _serialized_start(lock_key: str):
    digest = hashlib.sha256(lock_key.encode("utf-8")).hexdigest()
    lock_path = (
        Path(get_hermes_home())
        / "runtime"
        / "interactive-workspace-locks"
        / f"{digest}.lock"
    )
    with _start_lock(lock_key):
        with _cross_process_start_lock(lock_path):
            yield


def _clean_identifier(name: str, value: str) -> str:
    clean = str(value or "").strip()
    if not _ID_RE.fullmatch(clean):
        raise InteractiveWorkspaceError("invalid_request", f"invalid {name}")
    return clean


def _run_git(cwd: Path, args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InteractiveWorkspaceError("git_failed", f"git {' '.join(args[:2])} failed: {exc}") from exc


def _git_ok(cwd: Path, args: list[str], *, timeout: int = 60) -> str:
    result = _run_git(cwd, args, timeout=timeout)
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "git failed").strip()
        raise InteractiveWorkspaceError("git_failed", reason[:500])
    return result.stdout.strip()


def _git_branch(path: Path) -> str:
    return _git_ok(path, ["branch", "--show-current"])


def _validate_repo(repo_root: Path) -> Path:
    root = repo_root.expanduser().resolve()
    actual = _git_ok(root, ["rev-parse", "--show-toplevel"])
    if Path(actual).resolve() != root:
        raise InteractiveWorkspaceError("project_repo_invalid", "project primary path is not a git root")
    return root


def _fetch_and_resolve_base(repo_root: Path) -> str:
    fetch = _run_git(repo_root, ["fetch", "--prune", "origin"], timeout=120)
    if fetch.returncode != 0:
        reason = (fetch.stderr or fetch.stdout or "git fetch failed").strip()
        raise InteractiveWorkspaceError("fetch_failed", reason[:500])

    upstream = _run_git(
        repo_root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
    )
    candidates: list[str] = []
    if upstream.returncode == 0 and upstream.stdout.strip().startswith("origin/"):
        candidates.append(upstream.stdout.strip())
    symbolic = _run_git(repo_root, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    if symbolic.returncode == 0:
        candidates.append(symbolic.stdout.strip().replace("refs/remotes/", "", 1))
    candidates.extend(["origin/main", "origin/master"])
    for candidate in dict.fromkeys(candidates):
        if _run_git(repo_root, ["rev-parse", "--verify", f"{candidate}^{{commit}}"]).__dict__.get("returncode") == 0:
            return candidate
    raise InteractiveWorkspaceError("base_not_found", "no fetched origin default branch is available")


def _branch_checkout_paths(repo_root: Path, branch: str) -> list[Path]:
    listing = _git_ok(repo_root, ["worktree", "list", "--porcelain"])
    paths: list[Path] = []
    current_path: Optional[Path] = None
    wanted = f"refs/heads/{branch}"
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ")).resolve()
        elif line == f"branch {wanted}" and current_path is not None:
            paths.append(current_path)
    return paths


def _prepare_worktree(repo_root: Path, target: Path, branch: str) -> _PreparedWorktree:
    repo_root = _validate_repo(repo_root)
    branch_check = _run_git(repo_root, ["check-ref-format", "--branch", branch])
    if branch_check.returncode != 0:
        raise InteractiveWorkspaceError("branch_invalid", "task branch is not a valid git branch")

    allowed_root = (repo_root / ".worktrees").resolve()
    target = target.expanduser().resolve(strict=False)
    try:
        target.relative_to(allowed_root)
    except ValueError as exc:
        raise InteractiveWorkspaceError(
            "workspace_path_unsafe",
            f"task worktree must live under {allowed_root}",
        ) from exc

    base_ref = _fetch_and_resolve_base(repo_root)
    existing_branch = (
        _run_git(
            repo_root,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        ).returncode
        == 0
    )
    before_paths = _branch_checkout_paths(repo_root, branch) if existing_branch else []
    if target.exists() and target not in before_paths:
        raise InteractiveWorkspaceError(
            "workspace_collision",
            f"workspace path is occupied: {target}",
        )
    if before_paths and target not in before_paths:
        raise InteractiveWorkspaceError(
            "workspace_collision",
            f"branch {branch} is already bound to {before_paths[0]}",
        )
    created_worktree = not target.exists() and not before_paths

    try:
        added = web_git.worktree_add(
            str(repo_root),
            {
                "name": target.name,
                "directoryName": target.name,
                "branch": branch,
                "base": base_ref,
            },
        )
    except RuntimeError as exc:
        raise InteractiveWorkspaceError(
            "worktree_create_failed",
            str(exc)[:500],
        ) from exc

    canonical_path = Path(str(added["path"])).resolve()
    canonical_root = Path(str(added["repoRoot"])).resolve()
    if canonical_root != repo_root or canonical_path != target:
        raise InteractiveWorkspaceError(
            "workspace_collision",
            f"branch {branch} is already bound to {canonical_path}",
        )
    return _PreparedWorktree(
        repo_root,
        canonical_path,
        str(added["branch"]),
        base_ref,
        created_worktree,
        created_worktree and not existing_branch,
    )


def _cleanup_new_worktree(prepared: _PreparedWorktree) -> str:
    if not prepared.created_worktree:
        return "not_created"
    status = _run_git(prepared.path, ["status", "--porcelain"])
    if status.returncode != 0 or status.stdout.strip():
        return "dirty_preserved"
    _run_git(prepared.repo_root, ["worktree", "unlock", str(prepared.path)])
    removed = _run_git(prepared.repo_root, ["worktree", "remove", str(prepared.path)], timeout=120)
    if removed.returncode != 0:
        return "remove_failed"
    if prepared.created_branch:
        deleted = _run_git(prepared.repo_root, ["branch", "-D", prepared.branch])
        if deleted.returncode != 0:
            return "branch_delete_failed"
    return "removed"


def _preflight_environment(request: InteractiveWorkspaceRequest, prepared: _PreparedWorktree) -> dict[str, str]:
    try:
        from tools.environments.local import build_subprocess_env

        env = build_subprocess_env(scrub_secrets=True, inherit_profile_home=True)
    except Exception:
        env = {name: value for name, value in os.environ.items() if name in {"HOME", "PATH", "LANG", "LC_ALL"}}
    env.update(
        {
            "HERMES_WORKSPACE_ROOT": str(prepared.path),
            "HERMES_WORKSPACE_SCOPE": request.write_scope,
            "HERMES_PROJECT_ID": request.project_id,
            "HERMES_TASK_ID": request.task_id,
            "HERMES_WORKSTREAM_ID": request.workstream_id,
        }
    )
    return env


def _run_preflight(
    request: InteractiveWorkspaceRequest,
    prepared: _PreparedWorktree,
) -> tuple[str, str]:
    config_path = prepared.path / _PREFLIGHT_CONFIG
    if not config_path.is_file():
        return "not_configured", "Repository has no .hermes/workspace-start.json preflight."
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        spec = config["preflight"]
        command = spec["command"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InteractiveWorkspaceError("preflight_config_invalid", f"invalid workspace preflight config: {exc}") from exc
    if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
        raise InteractiveWorkspaceError("preflight_config_invalid", "preflight command must be a non-empty argv list")
    required = spec.get("required_inputs") or []
    if "write_scope" in required and not request.write_scope.strip():
        raise InteractiveWorkspaceError("write_scope_required", "repository preflight requires a write scope")
    try:
        timeout = max(5, min(300, int(spec.get("timeout_seconds", 120))))
    except (TypeError, ValueError):
        raise InteractiveWorkspaceError("preflight_config_invalid", "preflight timeout must be an integer")

    env = _preflight_environment(request, prepared)
    value_map = {
        "workspace_path": str(prepared.path),
        "write_scope": request.write_scope,
        "project_id": request.project_id,
        "task_id": request.task_id,
        "workstream_id": request.workstream_id,
    }
    aliases = spec.get("env") or {}
    if not isinstance(aliases, dict):
        raise InteractiveWorkspaceError("preflight_config_invalid", "preflight env must be an object")
    for env_name, source_name in aliases.items():
        if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", env_name):
            raise InteractiveWorkspaceError("preflight_config_invalid", "preflight env name is invalid")
        if source_name not in value_map:
            raise InteractiveWorkspaceError("preflight_config_invalid", f"unsupported preflight env source: {source_name}")
        env[env_name] = value_map[source_name]

    try:
        result = subprocess.run(
            command,
            cwd=str(prepared.path),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InteractiveWorkspaceError("preflight_failed", f"workspace preflight failed: {exc}") from exc
    summary = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    summary = summary[:_MAX_PREFLIGHT_OUTPUT]
    if result.returncode != 0:
        raise InteractiveWorkspaceError(
            "preflight_failed",
            f"workspace preflight exited {result.returncode}: {summary or 'no output'}",
            details={"preflight_status": "failed", "preflight_summary": summary},
        )
    return "passed", summary or "Preflight passed."


def _new_session_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"


def _result_from_event(payload: dict[str, Any], *, reused: bool) -> InteractiveWorkspaceResult:
    return InteractiveWorkspaceResult(
        project_id=str(payload["project_id"]),
        task_id=str(payload["task_id"]),
        workstream_id=str(payload["workstream_id"]),
        session_id=str(payload["session_id"]),
        repo_root=str(payload["repo_root"]),
        workspace_path=str(payload["workspace_path"]),
        branch=str(payload["branch"]),
        base_ref=str(payload.get("base_ref") or ""),
        preflight_status=str(payload.get("preflight_status") or "unknown"),
        preflight_summary=str(payload.get("preflight_summary") or ""),
        reused=reused,
    )


def _existing_success(
    conn,
    request: InteractiveWorkspaceRequest,
    session_db: SessionDB,
) -> Optional[InteractiveWorkspaceResult]:
    for event in reversed(kdb.list_events(conn, request.task_id)):
        payload = event.payload or {}
        if event.kind != "interactive_workspace_prepared" or payload.get("idempotency_key") != request.idempotency_key:
            continue
        expected_intent = {
            "project_id": request.project_id,
            "task_id": request.task_id,
            "workstream_id": request.workstream_id,
            "write_scope": request.write_scope,
        }
        mismatched = [
            field
            for field, expected in expected_intent.items()
            if str(payload.get(field) or "") != expected
        ]
        if mismatched:
            raise InteractiveWorkspaceError(
                "idempotency_mismatch",
                "idempotency key is already bound to a different start intent",
                details={"mismatched_fields": mismatched},
            )
        result = _result_from_event(payload, reused=True)
        row = session_db.get_session(result.session_id)
        if row is None:
            raise InteractiveWorkspaceError("stale_start_receipt", "the prior start session no longer exists")
        path = Path(result.workspace_path)
        if not path.is_dir() or _git_branch(path) != result.branch or str(row.get("cwd") or "") != result.workspace_path:
            raise InteractiveWorkspaceError("stale_start_receipt", "the prior start workspace no longer matches its receipt")
        return result
    return None


def _failure_payload(
    request: InteractiveWorkspaceRequest,
    exc: InteractiveWorkspaceError,
    prepared: Optional[_PreparedWorktree],
    cleanup: str,
) -> dict[str, Any]:
    return {
        "project_id": request.project_id,
        "task_id": request.task_id,
        "workstream_id": request.workstream_id,
        "idempotency_key": request.idempotency_key,
        "code": exc.code,
        "message": str(exc)[:500],
        "workspace_path": str(prepared.path) if prepared else None,
        "branch": prepared.branch if prepared else None,
        "preflight_status": exc.details.get("preflight_status", "not_run"),
        "preflight_summary": str(exc.details.get("preflight_summary") or "")[:_MAX_PREFLIGHT_OUTPUT],
        "cleanup": cleanup,
    }


def start_interactive_task_workspace(request: InteractiveWorkspaceRequest) -> InteractiveWorkspaceResult:
    """Prepare and persist one idempotent interactive session for a native task."""
    request = InteractiveWorkspaceRequest(
        project_id=_clean_identifier("project_id", request.project_id),
        task_id=_clean_identifier("task_id", request.task_id),
        workstream_id=_clean_identifier("workstream_id", request.workstream_id),
        idempotency_key=_clean_identifier("idempotency_key", request.idempotency_key),
        write_scope=str(request.write_scope or "").strip()[:500],
        profile_name=str(request.profile_name or "").strip()[:100],
    )
    lock_key = f"{request.project_id}\0{request.task_id}\0{request.idempotency_key}"
    with _serialized_start(lock_key):
        with pdb.connect_closing() as project_conn:
            project = pdb.get_project(project_conn, request.project_id)
        if project is None or project.archived:
            raise InteractiveWorkspaceError("project_not_found", f"project not found: {request.project_id}")
        repo_value = str(project.primary_path or "").strip()
        if not repo_value:
            raise InteractiveWorkspaceError("project_repo_missing", "project has no primary repository")
        board = str(project.board_slug or kdb.DEFAULT_BOARD)

        prepared: Optional[_PreparedWorktree] = None
        session_db = SessionDB()
        session_id: Optional[str] = None
        try:
            with kdb.connect_closing(board=board) as conn:
                task = kdb.get_task(conn, request.task_id)
                if task is None:
                    raise InteractiveWorkspaceError("task_not_found", f"task not found: {request.task_id}")
                if str(task.project_id or "") != request.project_id:
                    raise InteractiveWorkspaceError(
                        "project_task_mismatch",
                        f"task {request.task_id} does not belong to project {request.project_id}",
                    )
                if task.workspace_kind != "worktree":
                    raise InteractiveWorkspaceError("task_workspace_invalid", "interactive coding task must use a worktree workspace")
                existing = _existing_success(conn, request, session_db)
                if existing is not None:
                    return existing

                repo_root = Path(repo_value)
                target = Path(task.workspace_path) if task.workspace_path else repo_root / ".worktrees" / task.id
                branch = str(task.branch_name or f"{project.slug}/{task.id}")
                prepared = _prepare_worktree(repo_root, target, branch)
                preflight_status, preflight_summary = _run_preflight(request, prepared)

                session_id = _new_session_id()
                session_db.create_session(
                    session_id,
                    source="dashboard",
                    session_key=session_id,
                    cwd=str(prepared.path),
                    profile_name=request.profile_name or None,
                    git_repo_root=str(prepared.repo_root),
                )
                session_db.update_session_cwd(
                    session_id,
                    str(prepared.path),
                    git_branch=prepared.branch,
                    git_repo_root=str(prepared.repo_root),
                    replace_git_meta=True,
                )
                payload = {
                    "project_id": request.project_id,
                    "task_id": request.task_id,
                    "workstream_id": request.workstream_id,
                    "idempotency_key": request.idempotency_key,
                    "write_scope": request.write_scope,
                    "session_id": session_id,
                    "repo_root": str(prepared.repo_root),
                    "workspace_path": str(prepared.path),
                    "branch": prepared.branch,
                    "base_ref": prepared.base_ref,
                    "preflight_status": preflight_status,
                    "preflight_summary": preflight_summary,
                    "execution_mode": "interactive",
                    "task_claimed": False,
                    "run_created": False,
                }
                kdb.bind_workspace_and_append_task_event(
                    conn,
                    task.id,
                    prepared.path,
                    prepared.branch,
                    "interactive_workspace_prepared",
                    payload,
                )
                return _result_from_event(payload, reused=False)
        except InteractiveWorkspaceError as exc:
            cleanup = _cleanup_new_worktree(prepared) if prepared else "not_created"
            if session_id:
                try:
                    session_db.delete_session_if_empty(session_id)
                except Exception:
                    pass
            try:
                with kdb.connect_closing(board=board) as failure_conn:
                    if kdb.get_task(failure_conn, request.task_id) is not None:
                        kdb.append_task_event(
                            failure_conn,
                            request.task_id,
                            "interactive_session_start_failed",
                            _failure_payload(request, exc, prepared, cleanup),
                        )
            except Exception:
                pass
            raise
        except Exception as raw_exc:
            cleanup = _cleanup_new_worktree(prepared) if prepared else "not_created"
            if session_id:
                try:
                    session_db.delete_session_if_empty(session_id)
                except Exception:
                    pass
            exc = InteractiveWorkspaceError("start_persistence_failed", f"interactive start failed: {raw_exc}")
            try:
                with kdb.connect_closing(board=board) as failure_conn:
                    if kdb.get_task(failure_conn, request.task_id) is not None:
                        kdb.append_task_event(
                            failure_conn,
                            request.task_id,
                            "interactive_session_start_failed",
                            _failure_payload(request, exc, prepared, cleanup),
                        )
            except Exception:
                pass
            raise exc from raw_exc
        finally:
            session_db.close()


def mark_interactive_task_session_connected(
    request: InteractiveWorkspaceRequest,
    session_id: str,
) -> InteractiveWorkspaceConnectedResult:
    """Persist the first browser-observed PTY frame for a prepared session."""
    request = InteractiveWorkspaceRequest(
        project_id=_clean_identifier("project_id", request.project_id),
        task_id=_clean_identifier("task_id", request.task_id),
        workstream_id=_clean_identifier("workstream_id", request.workstream_id),
        idempotency_key=_clean_identifier("idempotency_key", request.idempotency_key),
        write_scope=str(request.write_scope or "").strip()[:500],
        profile_name=str(request.profile_name or "").strip()[:100],
    )
    clean_session_id = _clean_identifier("session_id", session_id)
    lock_key = f"{request.project_id}\0{request.task_id}\0{request.idempotency_key}"
    with _serialized_start(lock_key):
        with pdb.connect_closing() as project_conn:
            project = pdb.get_project(project_conn, request.project_id)
        if project is None or project.archived:
            raise InteractiveWorkspaceError("project_not_found", f"project not found: {request.project_id}")
        board = str(project.board_slug or kdb.DEFAULT_BOARD)
        session_db = SessionDB()
        try:
            with kdb.connect_closing(board=board) as conn:
                task = kdb.get_task(conn, request.task_id)
                if task is None:
                    raise InteractiveWorkspaceError("task_not_found", f"task not found: {request.task_id}")
                if str(task.project_id or "") != request.project_id:
                    raise InteractiveWorkspaceError(
                        "project_task_mismatch",
                        f"task {request.task_id} does not belong to project {request.project_id}",
                    )
                prepared = _existing_success(conn, request, session_db)
                if prepared is None:
                    raise InteractiveWorkspaceError(
                        "workspace_not_prepared",
                        "interactive workspace has no matching prepared receipt",
                    )
                if prepared.session_id != clean_session_id:
                    raise InteractiveWorkspaceError(
                        "session_intent_mismatch",
                        "session does not match the prepared workspace receipt",
                    )
                for event in reversed(kdb.list_events(conn, request.task_id)):
                    payload = event.payload or {}
                    if (
                        event.kind == "interactive_session_connected"
                        and payload.get("idempotency_key") == request.idempotency_key
                        and payload.get("session_id") == clean_session_id
                    ):
                        return InteractiveWorkspaceConnectedResult(
                            request.project_id,
                            request.task_id,
                            request.workstream_id,
                            clean_session_id,
                            reused=True,
                        )
                kdb.append_task_event(
                    conn,
                    request.task_id,
                    "interactive_session_connected",
                    {
                        "project_id": request.project_id,
                        "task_id": request.task_id,
                        "workstream_id": request.workstream_id,
                        "idempotency_key": request.idempotency_key,
                        "session_id": clean_session_id,
                        "execution_mode": "interactive",
                        "consumer_evidence": "browser_pty_first_frame",
                        "task_claimed": False,
                        "run_created": False,
                    },
                )
                return InteractiveWorkspaceConnectedResult(
                    request.project_id,
                    request.task_id,
                    request.workstream_id,
                    clean_session_id,
                )
        finally:
            session_db.close()