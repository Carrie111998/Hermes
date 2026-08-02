"""Production Workflow v1 reserve/launch/evidence runtime seam.

This module intentionally does not generalize the legacy Kanban dispatcher.  It
owns the one protected-leaf saga and keeps process creation injectable so the
Phase 0 harness can exercise identical persistence and fencing without launch.
Workers are cooperative (not hostile); controller-side Git and acceptance
verification remains authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Protocol, Sequence

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_evidence import _hardened_git_env
from hermes_cli.kanban_execution import (
    ContextCapsule,
    LeafSpec,
    ReconciliationReport,
    _acquire_workspace_reservation,
    _canonical_json,
    _finalize_workspace_reservation,
    _hash_payload,
    _load_envelope,
    _release_workspace_reservation,
    _required_text,
    _workspace_claim_lock,
    get_workflow_controller_state,
    validate_execution_readiness,
)
from hermes_cli.sqlite_util import write_txn

_NORMAL_TOOLSETS = ("terminal", "file")
_OUTPUT_SCHEMA = (
    "status",
    "summary",
    "changed_files",
    "checks",
    "blocker",
    "proposed_follow_ups",
)
_MAX_CHECK_OUTPUT = 16 * 1024
_MAX_PROPOSAL_BYTES = 64 * 1024
_MAX_PROGRESS_BYTES = 16 * 1024
_PROGRESS_SCHEMA = frozenset({
    "schema",
    "task_id",
    "run_id",
    "fence",
    "controller_epoch",
    "state",
    "sequence",
    "summary",
})
_WORKER_ENV_ALLOWLIST = frozenset({
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "TMP",
    "TEMP",
})
_PROVIDER_ENV_KEYS = {
    "nous": ("NOUS_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
}


@dataclass(frozen=True)
class WorkerInvocation:
    task_id: str
    run_id: int
    fence: str
    controller_epoch: str
    leaf_key: str
    spec_hash: str
    pin_sha: str
    capsule_hash: str
    cwd: str
    prompt: str
    toolsets: tuple[str, ...]
    receipt_path: str
    proposal_path: str
    progress_directory: str
    first_evidence_seconds: int
    wall_clock_budget_seconds: int
    model_turn_budget: int = 200
    output_byte_budget: int = _MAX_PROPOSAL_BYTES


@dataclass(frozen=True)
class LaunchHandle:
    launch_id: str
    pid: int
    process_group: Optional[int]
    process_start_identity: str
    receipt_path: str
    launcher_kind: str


class WorkflowLauncher(Protocol):
    def launch(self, invocation: WorkerInvocation) -> LaunchHandle: ...


class LaunchFailure(RuntimeError):
    """Positive launcher result about whether a process ever existed."""

    def __init__(self, message: str, *, process_created: bool):
        super().__init__(message)
        self.process_created = bool(process_created)


class HermesWorkflowLauncher:
    """Cooperative Hermes worker launcher for one immutable invocation."""

    def __init__(
        self,
        *,
        model: str = "",
        provider: str = "",
        auth_source: Optional[Path] = None,
    ):
        self.model = str(model or "").strip()
        self.provider = str(provider or "").strip()
        if auth_source is None:
            profile_home = Path(
                os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
            )
            auth_source = profile_home / "auth.json"
        self.auth_source = Path(auth_source)

    @staticmethod
    def _process_start_identity(pid: int) -> str:
        if os.name == "posix":
            try:
                fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
                if len(fields) > 21:
                    return f"linux-start:{fields[21]}"
            except (OSError, UnicodeError):
                pass
        return f"pid:{pid}:observed:{time.time_ns()}"

    def _provision_worker_home(self, invocation: WorkerInvocation) -> Path:
        worker_home = Path(invocation.receipt_path).parent / "worker-home"
        worker_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = worker_home.lstat()
        if worker_home.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise OSError("Workflow worker home is not a directory")
        worker_home.chmod(0o700)
        # A profile .env contains channel, GitHub, deploy, and unrelated product
        # credentials. Workers receive only their selected model credential.
        (worker_home / ".env").unlink(missing_ok=True)
        if self.auth_source.is_file():
            raw = json.loads(self.auth_source.read_text(encoding="utf-8"))
            pools = raw.get("credential_pool", {}) if isinstance(raw, dict) else {}
            providers = raw.get("providers", {}) if isinstance(raw, dict) else {}
            filtered = {
                "version": raw.get("version", 1),
                "updated_at": raw.get("updated_at"),
                "active_provider": self.provider,
                "credential_pool": (
                    {self.provider: pools[self.provider]}
                    if isinstance(pools, dict) and self.provider in pools
                    else {}
                ),
                "providers": (
                    {self.provider: providers[self.provider]}
                    if isinstance(providers, dict) and self.provider in providers
                    else {}
                ),
            }
            auth_path = worker_home / "auth.json"
            temporary = worker_home / f".auth-{invocation.run_id}.tmp"
            temporary.write_text(_canonical_json(filtered), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(auth_path)
            auth_path.chmod(0o600)
        for child in (worker_home / "gh", worker_home / "xdg"):
            child.mkdir(parents=True, exist_ok=True, mode=0o700)
            child.chmod(0o700)
        return worker_home

    def _worker_env(
        self, invocation: WorkerInvocation, worker_home: Path
    ) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _WORKER_ENV_ALLOWLIST
        }
        for key in _PROVIDER_ENV_KEYS.get(self.provider, ()):
            if os.environ.get(key):
                env[key] = os.environ[key]
        # Process cwd alone is not authoritative: a profile's configured
        # terminal.cwd can otherwise route file/terminal tools back to the
        # dispatcher's checkout. Pin every worker-facing cwd coordinate.
        env.update({
            "TERMINAL_CWD": invocation.cwd,
            "HERMES_KANBAN_WORKSPACE": invocation.cwd,
            "HERMES_KANBAN_TASK": invocation.task_id,
            "HERMES_KANBAN_RUN_ID": str(invocation.run_id),
            "HERMES_KANBAN_CLAIM_LOCK": invocation.fence,
            "HERMES_SESSION_SOURCE": "workflow-worker",
            # Workflow workers report through the fenced proposal channel;
            # they must not mutate Kanban through a nested CLI invocation.
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HOME": str(worker_home),
            "HERMES_HOME": str(worker_home),
            "GH_CONFIG_DIR": str(worker_home / "gh"),
            "XDG_CONFIG_HOME": str(worker_home / "xdg"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        })
        return env

    def launch(self, invocation: WorkerInvocation) -> LaunchHandle:
        if not self.model or not self.provider:
            raise LaunchFailure(
                "Workflow worker model/provider are not configured",
                process_created=False,
            )
        try:
            channel_directories = {
                Path(invocation.receipt_path).parent,
                Path(invocation.proposal_path).parent,
                Path(invocation.progress_directory),
            }
            for directory in channel_directories:
                directory.mkdir(parents=True, exist_ok=True)
                info = directory.lstat()
                if directory.is_symlink() or not stat.S_ISDIR(info.st_mode):
                    raise OSError(f"Workflow channel is not a directory: {directory}")
            worker_home = self._provision_worker_home(invocation)
            env = self._worker_env(invocation, worker_home)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise LaunchFailure(str(exc), process_created=False) from exc
        cmd = [
            *kb._resolve_hermes_argv(),
            "chat",
            "-Q",
            "--source",
            "workflow-worker",
            "--model",
            self.model,
            "--provider",
            self.provider,
            "--ignore-user-config",
            "--max-turns",
            "50",
            "--toolsets",
            ",".join(invocation.toolsets),
            "-q",
            invocation.prompt,
        ]
        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv plus immutable prompt
                cmd,
                cwd=invocation.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                creationflags=subprocess.CREATE_NO_WINDOW if kb._IS_WINDOWS else 0,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise LaunchFailure(str(exc), process_created=False) from exc
        pid = int(proc.pid)
        launch_id = uuid.uuid4().hex
        start_identity = self._process_start_identity(pid)
        receipt = {
            "schema": "hermes.workflow-launch-receipt.v1",
            "launch_id": launch_id,
            "pid": pid,
            "process_group": pid if os.name == "posix" else None,
            "process_start_identity": start_identity,
            "cwd": invocation.cwd,
            "task_id": invocation.task_id,
            "run_id": invocation.run_id,
        }
        receipt_path = Path(invocation.receipt_path)
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = receipt_path.with_name(f".{receipt_path.name}.{pid}.tmp")
            temporary.write_text(_canonical_json(receipt), encoding="utf-8")
            temporary.replace(receipt_path)
        except OSError as exc:
            raise LaunchFailure(str(exc), process_created=True) from exc
        return LaunchHandle(
            launch_id=launch_id,
            pid=pid,
            process_group=pid if os.name == "posix" else None,
            process_start_identity=start_identity,
            receipt_path=str(receipt_path),
            launcher_kind="hermes-cli",
        )


class RecordingLauncher:
    """Test/Phase 0 launcher with observable invocations and no subprocess."""

    def __init__(
        self,
        *,
        pid: int = 424242,
        error: Optional[BaseException] = None,
        after_spawn_error: Optional[BaseException] = None,
        on_launch: Optional[Callable[[], None]] = None,
    ):
        self.pid = int(pid)
        self.error = error
        self.after_spawn_error = after_spawn_error
        self.on_launch = on_launch
        self.invocations: list[WorkerInvocation] = []
        # This launcher returns an observed synthetic PID but never creates a
        # process. Phase 0 derives worker_launched from this observation.
        self.processes_created = 0

    def launch(self, invocation: WorkerInvocation) -> LaunchHandle:
        self.invocations.append(invocation)
        if self.error is not None:
            raise self.error
        if self.after_spawn_error is not None:
            raise self.after_spawn_error
        if self.on_launch is not None:
            self.on_launch()
        return LaunchHandle(
            launch_id=f"synthetic-{uuid.uuid4().hex}",
            pid=self.pid,
            process_group=self.pid,
            process_start_identity=f"synthetic:{self.pid}",
            receipt_path=invocation.receipt_path,
            launcher_kind="recording",
        )


@dataclass(frozen=True)
class DispatchOutcome:
    status: str
    task_id: str
    run_id: Optional[int] = None
    fence: Optional[str] = None
    pid: Optional[int] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class AcceptanceCheck:
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


@dataclass(frozen=True)
class ResultOutcome:
    accepted: bool
    reason: str
    status: Optional[str] = None
    checks: tuple[AcceptanceCheck, ...] = ()


@dataclass(frozen=True)
class ReviewCoordinate:
    candidate_sha: str
    diff_digest: str
    required_ci: bool


@dataclass(frozen=True)
class CloseoutOutcome:
    accepted: bool
    reason: str
    status: Optional[str] = None


@dataclass(frozen=True)
class FailureOutcome:
    failure_class: str
    count: int
    action: str


@dataclass(frozen=True)
class ProgressOutcome:
    accepted: bool
    reason: str
    sequence: int
    evidence_digest: Optional[str] = None


@dataclass(frozen=True)
class ReviewObservation:
    reviewer: str
    candidate_sha: str
    diff_digest: str
    checklist: Mapping[str, object]
    approved: bool = True


@dataclass(frozen=True)
class CIObservation:
    candidate_sha: str
    check_suite: str
    conclusion: str


@dataclass(frozen=True)
class ProjectionRequest:
    status: str
    expected_updated_at: str


@dataclass(frozen=True)
class GitHubSnapshot:
    repository_node_id: str
    issue_node_id: str
    project_item_id: str
    source_updated_at: str
    source_version: str
    issue: Mapping[str, object]
    project: Mapping[str, object]
    pull_requests: tuple[Mapping[str, object], ...] = ()

    def payload(self) -> dict[str, object]:
        return {
            "repository_node_id": _required_text(
                "repository_node_id", self.repository_node_id
            ),
            "issue_node_id": _required_text("issue_node_id", self.issue_node_id),
            "project_item_id": _required_text("project_item_id", self.project_item_id),
            "source_updated_at": _required_text(
                "source_updated_at", self.source_updated_at
            ),
            "source_version": _required_text("source_version", self.source_version),
            "issue": dict(self.issue),
            "project": dict(self.project),
            "pull_requests": [dict(pr) for pr in self.pull_requests],
        }


@dataclass(frozen=True)
class GitHubSnapshotOutcome:
    version: int
    content_hash: str
    changed: bool
    material_change: bool


@dataclass(frozen=True)
class GitHubProjectionOutcome:
    status: str
    observed_updated_at: str
    verified: bool


class GitHubProjectionWriter(Protocol):
    def write_status(
        self, *, project_item_id: str, status: str, expected_updated_at: str
    ) -> None: ...
    def read_status(self, *, project_item_id: str) -> Mapping[str, object]: ...


class ProcessInspector(Protocol):
    def inspect(self, process_identity: Mapping[str, object]) -> str: ...


class HostProcessInspector:
    """Classify the persisted launch identity as alive, dead, or unknown."""

    def inspect(self, process_identity: Mapping[str, object]) -> str:
        try:
            raw_pid = process_identity["pid"]
            if not isinstance(raw_pid, (str, int)) or isinstance(raw_pid, bool):
                return "unknown"
            pid = int(raw_pid)
            expected_start = str(process_identity["process_start_identity"])
        except (KeyError, TypeError, ValueError):
            return "unknown"
        if pid < 1 or not expected_start:
            return "unknown"
        if os.name == "posix":
            stat_path = Path(f"/proc/{pid}/stat")
            try:
                fields = stat_path.read_text(encoding="utf-8").split()
            except FileNotFoundError:
                return "dead"
            except (OSError, UnicodeError):
                return "unknown"
            if len(fields) <= 21:
                return "unknown"
            observed_start = f"linux-start:{fields[21]}"
            if expected_start.startswith("linux-start:"):
                return "alive" if observed_start == expected_start else "dead"
            # A synthetic/test launcher identity can prove absence, but cannot
            # safely claim ownership of a live PID it did not start.
            return "unknown"
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "dead"
        except (PermissionError, OSError):
            return "unknown"
        return "unknown"


class WorkflowCoordinatorAdapter(GitHubProjectionWriter, Protocol):
    enabled: bool

    def fetch_snapshot(self, task: kb.Task) -> Optional[GitHubSnapshot]: ...
    def requires_protected_ci(
        self, task: kb.Task, snapshot: GitHubSnapshot
    ) -> bool: ...
    def verify_review(
        self, task: kb.Task, coordinate: ReviewCoordinate
    ) -> Optional[ReviewObservation]: ...
    def protected_ci(
        self, task: kb.Task, coordinate: ReviewCoordinate
    ) -> Optional[CIObservation]: ...
    def projection_request(
        self, task: kb.Task, snapshot: GitHubSnapshot, coordinate: ReviewCoordinate
    ) -> Optional[ProjectionRequest]: ...


class PausedWorkflowCoordinatorAdapter:
    """Fail-closed production default: no API or subprocess activity."""

    enabled = False

    def fetch_snapshot(self, task: kb.Task) -> Optional[GitHubSnapshot]:
        return None

    def requires_protected_ci(self, task: kb.Task, snapshot: GitHubSnapshot) -> bool:
        return True

    def verify_review(
        self, task: kb.Task, coordinate: ReviewCoordinate
    ) -> Optional[ReviewObservation]:
        return None

    def protected_ci(
        self, task: kb.Task, coordinate: ReviewCoordinate
    ) -> Optional[CIObservation]:
        return None

    def projection_request(
        self, task: kb.Task, snapshot: GitHubSnapshot, coordinate: ReviewCoordinate
    ) -> Optional[ProjectionRequest]:
        return None

    def write_status(
        self, *, project_item_id: str, status: str, expected_updated_at: str
    ) -> None:
        raise GitHubProjectionConflict("Workflow GitHub adapter is paused")

    def read_status(self, *, project_item_id: str) -> Mapping[str, object]:
        raise GitHubProjectionConflict("Workflow GitHub adapter is paused")


class WorkflowCoordinationFailure(RuntimeError):
    def __init__(self, failure_class: str, detail: str):
        super().__init__(detail)
        self.failure_class = failure_class
        self.detail = detail


class GitHubProjectionConflict(RuntimeError):
    """A canonical GitHub object changed or failed read-back verification."""


def _git(repo: Path, *args: str, timeout: int = 10) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_hardened_git_env(),
    )
    return result.stdout


def _safe_repo_path(raw: str) -> str:
    path = PurePosixPath(str(raw))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"relevant file escapes repository: {raw}")
    return path.as_posix()


def materialize_context_capsule(
    repository_path: str | Path,
    *,
    spec: LeafSpec,
    relevant_files: Sequence[str],
    symbols: Sequence[str],
    governing_decisions: Sequence[str] = ("Pinned source is authoritative.",),
    base_assumptions: Sequence[str] = ("No canonical-source drift is permitted.",),
    output_schema: Sequence[str] = _OUTPUT_SCHEMA,
) -> ContextCapsule:
    """Build a bounded capsule from regular files at exactly ``spec.pin_sha``."""

    started = time.monotonic()
    repo = Path(repository_path).resolve(strict=True)
    try:
        source_sha = (
            _git(repo, "rev-parse", f"{spec.pin_sha}^{{commit}}").strip().lower()
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("pin_sha is missing from repository") from exc
    if source_sha != spec.pin_sha:
        raise ValueError("pin_sha does not resolve to the exact requested source SHA")

    paths = tuple(_safe_repo_path(path) for path in relevant_files)
    if not paths:
        raise ValueError("relevant_files must contain at least one file")
    contents: list[str] = []
    tree_inputs: list[dict[str, str]] = []
    for path in paths:
        try:
            entry = _git(repo, "ls-tree", source_sha, "--", path).strip()
        except (
            OSError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise ValueError(f"relevant file is missing at pin: {path}") from exc
        fields = entry.split(None, 3)
        if len(fields) != 4:
            raise ValueError(f"relevant file is missing at pin: {path}")
        mode, kind, blob_sha, entry_path = fields
        if kind != "blob" or not mode.startswith("100") or entry_path != path:
            raise ValueError(f"relevant target is not a regular file at pin: {path}")
        try:
            raw = subprocess.run(
                ["git", "-C", str(repo), "show", f"{source_sha}:{path}"],
                check=True,
                capture_output=True,
                timeout=10,
                env=_hardened_git_env(),
            ).stdout
            text = raw.decode("utf-8", errors="strict")
        except (
            OSError,
            UnicodeError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise ValueError(
                f"relevant file cannot be read as UTF-8 at pin: {path}"
            ) from exc
        contents.append(text)
        tree_inputs.append({"path": path, "blob_sha": blob_sha})

    normalized_symbols = tuple(_required_text("symbols", value) for value in symbols)
    if not normalized_symbols:
        raise ValueError("symbols must contain at least one entry")
    corpus = "\n".join(contents)
    unresolved = [
        symbol
        for symbol in normalized_symbols
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])", corpus)
        is None
    ]
    if unresolved:
        raise ValueError("required symbol unresolved at pin: " + ", ".join(unresolved))

    construction_inputs = {
        "pin_sha": source_sha,
        "relevant_files": list(paths),
        "symbols": list(normalized_symbols),
        "governing_decisions": list(governing_decisions),
        "base_assumptions": list(base_assumptions),
        "output_schema": list(output_schema),
    }
    return ContextCapsule(
        relevant_files=paths,
        symbols=normalized_symbols,
        governing_decisions=tuple(governing_decisions),
        base_assumptions=tuple(base_assumptions),
        output_schema=tuple(output_schema),
        source_sha=source_sha,
        source_tree_hash=_hash_payload(tree_inputs),
        construction_inputs_hash=_hash_payload(construction_inputs),
        construction_duration_ms=max(0, int((time.monotonic() - started) * 1000)),
    )


def _materialization_valid(task: kb.Task, envelope: Mapping) -> bool:
    capsule = envelope.get("capsule", {})
    materialization = (
        capsule.get("materialization") if isinstance(capsule, dict) else None
    )
    if (
        not isinstance(materialization, dict)
        or not task.pin_sha
        or materialization.get("source_sha") != task.pin_sha
    ):
        return False
    if not task.workspace_path:
        return False
    tree_inputs: list[dict[str, str]] = []
    repo = Path(task.workspace_path)
    try:
        if (
            _git(repo, "rev-parse", f"{task.pin_sha}^{{commit}}").strip().lower()
            != task.pin_sha
        ):
            return False
        for path in capsule.get("relevant_files", []):
            safe = _safe_repo_path(path)
            entry = (
                _git(repo, "ls-tree", task.pin_sha, "--", safe).strip().split(None, 3)
            )
            if (
                len(entry) != 4
                or entry[1] != "blob"
                or not entry[0].startswith("100")
                or entry[3] != safe
            ):
                return False
            tree_inputs.append({"path": safe, "blob_sha": entry[2]})
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        return False
    construction_inputs = {
        "pin_sha": task.pin_sha,
        "relevant_files": list(capsule.get("relevant_files", [])),
        "symbols": list(capsule.get("symbols", [])),
        "governing_decisions": list(capsule.get("governing_decisions", [])),
        "base_assumptions": list(capsule.get("base_assumptions", [])),
        "output_schema": list(capsule.get("output_schema", [])),
    }
    return bool(
        materialization.get("source_tree_hash") == _hash_payload(tree_inputs)
        and materialization.get("construction_inputs_hash")
        == _hash_payload(construction_inputs)
    )


def _controller_dispatchable(conn: sqlite3.Connection, epoch: str) -> bool:
    state = get_workflow_controller_state(conn)
    return bool(
        state.dispatch_enabled
        and state.broker_ready
        and state.status == "healthy"
        and state.controller_epoch == epoch
        and state.heartbeat_at is not None
        and state.heartbeat_at
        >= int(time.time()) - kb.WORKFLOW_CONTROLLER_STALE_SECONDS
    )


def _reserve_execution_leaf(
    conn: sqlite3.Connection, task_id: str, *, controller_epoch: str
) -> DispatchOutcome:
    if not _controller_dispatchable(conn, controller_epoch):
        return DispatchOutcome("rejected", task_id, reason="controller_unavailable")
    task = kb.get_task(conn, task_id)
    if task is None or not task.workspace_path:
        return DispatchOutcome("rejected", task_id, reason="task_or_workspace_missing")
    workspace = task.workspace_path
    with _workspace_claim_lock(workspace) as locked:
        if not locked:
            return DispatchOutcome(
                "rejected", task_id, reason="workspace_lock_contended"
            )
        readiness = validate_execution_readiness(conn, task_id)
        if not readiness.ready:
            return DispatchOutcome(
                "rejected", task_id, reason=",".join(readiness.blockers)
            )
        task = kb.get_task(conn, task_id)
        envelope, blockers = (
            _load_envelope(task) if task is not None else (None, ["task_missing"])
        )
        if (
            task is None
            or envelope is None
            or blockers
            or not _materialization_valid(task, envelope)
        ):
            return DispatchOutcome(
                "rejected", task_id, reason="capsule_rematerialization_required"
            )
        if task.status == "todo":
            with write_txn(conn):
                promoted = conn.execute(
                    "UPDATE tasks SET status='ready' WHERE id=? AND status='todo' AND leaf_key IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM task_links l JOIN tasks p ON p.id=l.parent_id "
                    "WHERE l.child_id=tasks.id AND p.status NOT IN ('done','archived'))",
                    (task_id,),
                )
                if promoted.rowcount != 1:
                    return DispatchOutcome("rejected", task_id, reason="promotion_race")
        fence = kb._new_claim_lock()
        if not _acquire_workspace_reservation(
            conn, workspace_path=workspace, task_id=task_id, claim_lock=fence
        ):
            return DispatchOutcome("rejected", task_id, reason="workspace_reserved")
        now = int(time.time())
        spec = envelope["spec"]
        expires = now + int(spec["first_evidence_seconds"])
        try:
            with write_txn(conn):
                if not _controller_dispatchable(conn, controller_epoch):
                    raise RuntimeError("controller_unavailable")
                final_readiness = validate_execution_readiness(conn, task_id)
                if not final_readiness.ready:
                    raise RuntimeError(
                        "final_readiness_failed:" + ",".join(final_readiness.blockers)
                    )
                row = conn.execute(
                    "SELECT status, current_run_id, claim_lock, workspace_path, assignee, max_runtime_seconds, "
                    "leaf_key, leaf_family_key, spec_hash, pin_sha, capsule_hash FROM tasks WHERE id=?",
                    (task_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "ready"
                    or row["current_run_id"] is not None
                    or row["claim_lock"] is not None
                    or row["workspace_path"] != workspace
                ):
                    raise RuntimeError("reserve_cas_failed")
                run_cur = conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
                    "max_runtime_seconds, started_at, reserved_at, leaf_key, leaf_family_key, "
                    "spec_hash, pin_sha, capsule_hash) "
                    "VALUES (?, ?, 'reserved', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        row["assignee"],
                        fence,
                        expires,
                        row["max_runtime_seconds"],
                        now,
                        now,
                        row["leaf_key"],
                        row["leaf_family_key"],
                        row["spec_hash"],
                        row["pin_sha"],
                        row["capsule_hash"],
                    ),
                )
                run_id = int(run_cur.lastrowid)
                updated = conn.execute(
                    "UPDATE tasks SET current_run_id=?, claim_lock=?, claim_expires=?, started_at=COALESCE(started_at,?) "
                    "WHERE id=? AND status='ready' AND current_run_id IS NULL AND claim_lock IS NULL",
                    (run_id, fence, expires, now, task_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("reserve_cas_failed")
                kb._append_event(
                    conn,
                    task_id,
                    "execution_reserved",
                    {
                        "run_id": run_id,
                        "fence_digest": hashlib.sha256(fence.encode()).hexdigest(),
                    },
                    run_id=run_id,
                )
        except Exception as exc:
            _release_workspace_reservation(
                conn, workspace_path=workspace, task_id=task_id, claim_lock=fence
            )
            return DispatchOutcome("rejected", task_id, reason=str(exc))
        if not _finalize_workspace_reservation(
            conn,
            workspace_path=workspace,
            task_id=task_id,
            claim_lock=fence,
            run_id=run_id,
        ):
            return DispatchOutcome(
                "ambiguous",
                task_id,
                run_id,
                fence,
                reason="reservation_finalize_failed",
            )
        return DispatchOutcome("reserved", task_id, run_id, fence)


def _invocation(
    conn: sqlite3.Connection, task_id: str, run_id: int, fence: str, epoch: str
) -> WorkerInvocation:
    task = kb.get_task(conn, task_id)
    if task is None or not task.workspace_path:
        raise ValueError("task workspace missing")
    envelope, blockers = _load_envelope(task)
    if envelope is None or blockers:
        raise ValueError("immutable capsule invalid")
    spec = envelope["spec"]
    capsule = envelope["capsule"]
    prompt = "\n".join([
        "# Immutable Workflow v1 leaf capsule",
        _canonical_json({
            "schema": "hermes.workflow-worker.v1",
            "task_id": task_id,
            "run_id": run_id,
            "fence": fence,
            "controller_epoch": epoch,
            "spec": spec,
            "capsule": capsule,
        }),
        f"Your exact worktree is {_canonical_json(str(Path(task.workspace_path).resolve(strict=True)))}. Before any file operation, use the terminal to cd to that exact path and verify git rev-parse --show-toplevel matches it.",
        "Work only in that exact worktree and the allowed paths. Treat controller evidence as authoritative.",
        "You must not create successors or Kanban cards; merge, release, publish, or deploy; administer GitHub Projects or product intent; request or expose secrets; or inspect/change unrelated repositories.",
        'Return only a proposal matching the capsule output schema. For successful work, status must be exactly "done"; for an unresolved blocker, status must be exactly "blocked". Completion and blocking are controller decisions.',
    ])
    receipt_path = (
        kb.kanban_home()
        / "kanban"
        / "workflow-launch-receipts"
        / f"run-{run_id}-{hashlib.sha256(fence.encode()).hexdigest()[:16]}.json"
    )
    proposal_path = (
        kb.kanban_home()
        / "kanban"
        / "workflow-worker-proposals"
        / f"run-{run_id}-{hashlib.sha256(fence.encode()).hexdigest()[:16]}.json"
    )
    progress_directory = (
        kb.kanban_home()
        / "kanban"
        / "workflow-worker-progress"
        / f"run-{run_id}-{hashlib.sha256(fence.encode()).hexdigest()[:16]}"
    )
    prompt = (
        prompt
        + "\n"
        + (
            "Publish progress/evidence proposals atomically before the first-evidence budget expires "
            f"({int(spec['first_evidence_seconds'])} seconds), after each new in-scope artifact/test/commit delta, "
            "and during long work before the evidence lease can expire. Use monotonically increasing sequence "
            "numbers starting at 1. For sequence N, atomically replace progress-N.json in this exact directory: "
            f'{progress_directory}. Each file must be one UTF-8 JSON object with exactly schema="hermes.workflow-progress.v1", '
            f"task_id={_canonical_json(task_id)}, run_id={run_id}, fence={_canonical_json(fence)}, "
            f'controller_epoch={_canonical_json(epoch)}, state="running", sequence=N, and optional summary. '
            "Write a temporary file in that same directory and rename it into place. Repeated identical heartbeats "
            "do not renew the lease; publish only after meaningful work or when reporting a long-running check. "
            "This progress channel is distinct from the terminal proposal. "
            "Write the final proposal as one UTF-8 JSON object, atomically, to this exact path: "
            f"{proposal_path}. Do not write any other controller artifact."
        )
    )
    return WorkerInvocation(
        task_id=task_id,
        run_id=run_id,
        fence=fence,
        controller_epoch=epoch,
        leaf_key=str(task.leaf_key),
        spec_hash=str(task.spec_hash),
        pin_sha=str(task.pin_sha),
        capsule_hash=str(task.capsule_hash),
        cwd=str(Path(task.workspace_path).resolve(strict=True)),
        prompt=prompt,
        toolsets=_NORMAL_TOOLSETS,
        receipt_path=str(receipt_path),
        proposal_path=str(proposal_path),
        progress_directory=str(progress_directory),
        first_evidence_seconds=int(spec["first_evidence_seconds"]),
        wall_clock_budget_seconds=int(spec["wall_clock_budget_seconds"]),
    )


def _positive_launch_failure(
    conn: sqlite3.Connection, outcome: DispatchOutcome, error: str
) -> None:
    task = kb.get_task(conn, outcome.task_id)
    if (
        task is None
        or outcome.run_id is None
        or not outcome.fence
        or not task.workspace_path
    ):
        return
    with write_txn(conn):
        run_update = conn.execute(
            "UPDATE task_runs SET status='failed', outcome='spawn_failed', failure_class='launch', error=?, ended_at=? WHERE id=? AND task_id=? AND status='reserved' AND claim_lock=? AND worker_pid IS NULL",
            (
                error[:1000],
                int(time.time()),
                outcome.run_id,
                outcome.task_id,
                outcome.fence,
            ),
        )
        task_update = conn.execute(
            "UPDATE tasks SET current_run_id=NULL, claim_lock=NULL, claim_expires=NULL, worker_pid=NULL WHERE id=? AND status='ready' AND current_run_id=? AND claim_lock=?",
            (outcome.task_id, outcome.run_id, outcome.fence),
        )
        _increment_failure_count(conn, outcome.task_id, "launch", now=int(time.time()))
        if run_update.rowcount != 1 or task_update.rowcount != 1:
            raise RuntimeError("launch-failure release lost its fence")
        kb._append_event(
            conn,
            outcome.task_id,
            "execution_launch_failed",
            {"error": error[:1000], "process_created": False},
            run_id=outcome.run_id,
        )
    _release_workspace_reservation(
        conn,
        workspace_path=task.workspace_path,
        task_id=outcome.task_id,
        claim_lock=outcome.fence,
    )


def dispatch_execution_leaf(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    controller_epoch: str,
    launcher: WorkflowLauncher,
    no_launch: bool = False,
) -> DispatchOutcome:
    """Reserve, optionally launch, and CAS one protected leaf to running."""

    reserved = _reserve_execution_leaf(conn, task_id, controller_epoch=controller_epoch)
    if reserved.status != "reserved" or no_launch:
        return reserved
    assert reserved.run_id is not None and reserved.fence is not None
    try:
        invocation = _invocation(
            conn, task_id, reserved.run_id, reserved.fence, controller_epoch
        )
        prelaunch_readiness = validate_execution_readiness(conn, task_id)
        if not prelaunch_readiness.ready:
            raise LaunchFailure(
                "prelaunch_readiness_failed:" + ",".join(prelaunch_readiness.blockers),
                process_created=False,
            )
        handle = launcher.launch(invocation)
        if handle.pid < 1:
            raise LaunchFailure("launcher returned invalid PID", process_created=False)
    except LaunchFailure as exc:
        if not exc.process_created:
            _positive_launch_failure(conn, reserved, str(exc))
            return DispatchOutcome(
                "launch_failed",
                task_id,
                reserved.run_id,
                reserved.fence,
                reason=str(exc),
            )
        return DispatchOutcome(
            "ambiguous", task_id, reserved.run_id, reserved.fence, reason=str(exc)
        )
    except BaseException as exc:
        # A generic exception after entering an injectable launcher cannot prove
        # that no process exists. Keep the fence and reservation fail-closed.
        with write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "execution_launch_ambiguous",
                {"error": str(exc)[:1000]},
                run_id=reserved.run_id,
            )
        return DispatchOutcome(
            "ambiguous", task_id, reserved.run_id, reserved.fence, reason=str(exc)
        )

    process_identity = _canonical_json({
        "pid": handle.pid,
        "process_group": handle.process_group,
        "process_start_identity": handle.process_start_identity,
        "launcher_kind": handle.launcher_kind,
    })
    now = int(time.time())
    try:
        with write_txn(conn):
            if not _controller_dispatchable(conn, controller_epoch):
                raise RuntimeError("activation_cas_failed")
            run_update = conn.execute(
                "UPDATE task_runs SET status='running', worker_pid=?, spawned_at=?, "
                "launch_id=?, launch_receipt_path=?, process_identity=?, quarantine_reason=NULL "
                "WHERE id=? AND task_id=? AND status='reserved' AND claim_lock=? "
                "AND worker_pid IS NULL AND ended_at IS NULL",
                (
                    handle.pid,
                    now,
                    handle.launch_id,
                    handle.receipt_path,
                    process_identity,
                    reserved.run_id,
                    task_id,
                    reserved.fence,
                ),
            )
            task_update = conn.execute(
                "UPDATE tasks SET status='running', worker_pid=? WHERE id=? AND status='ready' "
                "AND current_run_id=? AND claim_lock=? AND worker_pid IS NULL",
                (handle.pid, task_id, reserved.run_id, reserved.fence),
            )
            if run_update.rowcount != 1 or task_update.rowcount != 1:
                raise RuntimeError("activation_cas_failed")
            kb._append_event(
                conn,
                task_id,
                "execution_launched",
                {"pid": handle.pid, "launch_id": handle.launch_id},
                run_id=reserved.run_id,
            )
    except RuntimeError:
        # The process exists, so rollback any partial activation and then bind
        # its identity to the still-reserved fence for later reconciliation.
        with write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET worker_pid=?, launch_id=?, launch_receipt_path=?, "
                "process_identity=?, quarantine_reason='activation_cas_failed' "
                "WHERE id=? AND task_id=? AND status='reserved' AND claim_lock=? AND ended_at IS NULL",
                (
                    handle.pid,
                    handle.launch_id,
                    handle.receipt_path,
                    process_identity,
                    reserved.run_id,
                    task_id,
                    reserved.fence,
                ),
            )
            conn.execute(
                "UPDATE tasks SET worker_pid=? WHERE id=? AND status='ready' "
                "AND current_run_id=? AND claim_lock=?",
                (handle.pid, task_id, reserved.run_id, reserved.fence),
            )
            kb._append_event(
                conn,
                task_id,
                "execution_launch_ambiguous",
                {
                    "pid": handle.pid,
                    "launch_id": handle.launch_id,
                    "reason": "activation_cas_failed",
                },
                run_id=reserved.run_id,
            )
        return DispatchOutcome(
            "ambiguous",
            task_id,
            reserved.run_id,
            reserved.fence,
            handle.pid,
            "activation_cas_failed",
        )
    return DispatchOutcome(
        "running", task_id, reserved.run_id, reserved.fence, handle.pid
    )


def _fence_reason(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
    fence: str,
    epoch: str,
    *,
    allow_review: bool = False,
    check_expiry: bool = True,
    published_at: Optional[float] = None,
) -> Optional[str]:
    row = conn.execute(
        "SELECT t.status, t.current_run_id, t.claim_lock, t.claim_expires, r.status AS run_status, c.controller_epoch "
        "FROM tasks t JOIN task_runs r ON r.id=t.current_run_id CROSS JOIN workflow_controller_state c WHERE t.id=?",
        (task_id,),
    ).fetchone()
    if row is None or row["current_run_id"] != int(run_id):
        return "stale_run"
    if row["claim_lock"] != fence:
        return "stale_fence"
    if row["controller_epoch"] != epoch:
        return "stale_controller_epoch"
    # The worker lease no longer governs controller-owned review/CI closeout.
    # The run id, opaque fence, controller epoch, and exact candidate SHA still
    # fence every mutation after the worker has handed control back.
    if check_expiry and not (allow_review and row["status"] == "review"):
        claim_expires = row["claim_expires"]
        timely_publication = (
            published_at is not None
            and claim_expires is not None
            and float(published_at) < int(claim_expires) + 1.0
        )
        if (claim_expires is None or int(claim_expires) < int(time.time())) and not (
            timely_publication
        ):
            return "lease_expired"
    allowed_statuses = {"running", "review"} if allow_review else {"running"}
    if row["status"] not in allowed_statuses or row["run_status"] not in {
        "running",
        "reviewing",
    }:
        return "attempt_not_running"
    return None


def submit_evidence_proposal(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    published_at: Optional[float] = None,
    terminal_publication: bool = False,
) -> kb.EvidenceResult:
    reason = _fence_reason(
        conn,
        task_id,
        run_id,
        fence,
        controller_epoch,
        published_at=published_at,
    )
    if reason is not None:
        return kb.EvidenceResult(False, reason)
    result = kb.record_workspace_evidence(
        conn,
        task_id,
        expected_run_id=run_id,
        expected_controller_epoch=controller_epoch,
        claim_lock=fence,
        published_at=published_at,
        terminal_publication=terminal_publication,
    )
    with write_txn(conn):
        kb._append_event(
            conn,
            task_id,
            "execution_evidence_proposal",
            {
                "accepted": result.accepted,
                "reason": result.reason,
                "digest": result.digest,
                "paths": list(result.paths),
            },
            run_id=run_id,
        )
    return result


def _run_acceptance(
    task: kb.Task, commands: Sequence[str]
) -> tuple[AcceptanceCheck, ...]:
    assert task.workspace_path
    deadline = time.monotonic() + max(1, int(task.max_runtime_seconds or 60))
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        **_hardened_git_env(),
    }
    results: list[AcceptanceCheck] = []
    for command in commands:
        remaining = max(1, int(deadline - time.monotonic()))
        started = time.monotonic()
        try:
            completed = subprocess.run(
                shlex.split(command),
                cwd=task.workspace_path,
                env=env,
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
            rc, stdout, stderr = (
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            rc, stdout, stderr = (
                124 if isinstance(exc, subprocess.TimeoutExpired) else 127,
                "",
                str(exc),
            )
        results.append(
            AcceptanceCheck(
                command,
                int(rc),
                stdout[-_MAX_CHECK_OUTPUT:],
                stderr[-_MAX_CHECK_OUTPUT:],
                max(0, int((time.monotonic() - started) * 1000)),
            )
        )
        if rc != 0:
            break
    return tuple(results)


def submit_result_proposal(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    proposal: Mapping[str, object],
    published_at: Optional[float] = None,
) -> ResultOutcome:
    reason = _fence_reason(
        conn,
        task_id,
        run_id,
        fence,
        controller_epoch,
        published_at=published_at,
    )
    if reason is not None:
        return ResultOutcome(False, reason)
    if not isinstance(proposal, Mapping) or any(
        key not in _OUTPUT_SCHEMA for key in proposal
    ):
        return ResultOutcome(False, "invalid_proposal_schema")
    try:
        proposal_json = _canonical_json(dict(proposal))
    except (TypeError, ValueError):
        return ResultOutcome(False, "invalid_proposal_schema")
    if len(proposal_json.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
        return ResultOutcome(False, "proposal_too_large")
    status = str(proposal.get("status") or "").lower()
    if status != "done":
        with write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "execution_result_proposal",
                {
                    "accepted": False,
                    "proposal": dict(proposal),
                    "reason": "controller_review_required",
                },
                run_id=run_id,
            )
        return ResultOutcome(False, "controller_review_required")
    evidence = submit_evidence_proposal(
        conn,
        task_id,
        run_id=run_id,
        fence=fence,
        controller_epoch=controller_epoch,
        published_at=published_at,
        terminal_publication=True,
    )
    if not evidence.accepted and evidence.reason not in {"duplicate_evidence"}:
        return ResultOutcome(False, f"evidence_{evidence.reason}")
    task = kb.get_task(conn, task_id)
    if task is None:
        return ResultOutcome(False, "task_missing")
    envelope, blockers = _load_envelope(task)
    if envelope is None or blockers:
        return ResultOutcome(False, "capsule_invalid")
    checks = _run_acceptance(task, envelope["spec"].get("acceptance_checks", []))
    if not checks or any(check.returncode != 0 for check in checks):
        with write_txn(conn):
            failure = _increment_failure_count(
                conn, task_id, "content", now=int(time.time())
            )
            kb._append_event(
                conn,
                task_id,
                "execution_acceptance_failed",
                {
                    "checks": [check.__dict__ for check in checks],
                    "failure_count": failure.count,
                    "action": failure.action,
                },
                run_id=run_id,
            )
        return ResultOutcome(False, "acceptance_failed", checks=checks)
    now = int(time.time())
    metadata = _canonical_json({
        "worker_proposal": dict(proposal),
        "controller_checks": [check.__dict__ for check in checks],
        "evidence_digest": evidence.digest,
    })
    with write_txn(conn):
        if (
            _fence_reason(
                conn,
                task_id,
                run_id,
                fence,
                controller_epoch,
                published_at=published_at,
            )
            is not None
        ):
            return ResultOutcome(False, "stale_fence", checks=checks)
        task_update = conn.execute(
            "UPDATE tasks SET status='review', result=? WHERE id=? AND status='running' AND current_run_id=? AND claim_lock=?",
            (str(proposal.get("summary") or ""), task_id, run_id, fence),
        )
        run_update = conn.execute(
            "UPDATE task_runs SET status='reviewing', summary=?, metadata=? WHERE id=? AND task_id=? AND status='running' AND claim_lock=? AND ended_at IS NULL",
            (str(proposal.get("summary") or ""), metadata, run_id, task_id, fence),
        )
        if task_update.rowcount != 1 or run_update.rowcount != 1:
            return ResultOutcome(False, "stale_fence", checks=checks)
        kb._append_event(
            conn,
            task_id,
            "execution_acceptance_verified",
            {
                "status": "review",
                "verified_at": now,
                "checks": [check.__dict__ for check in checks],
            },
            run_id=run_id,
        )
    return ResultOutcome(True, "verified", "review", checks)


def _progress_directory(run_id: int, fence: str) -> Path:
    return (
        kb.kanban_home()
        / "kanban"
        / "workflow-worker-progress"
        / f"run-{int(run_id)}-{hashlib.sha256(fence.encode()).hexdigest()[:16]}"
    )


def _read_bounded_regular_file(path: Path, *, maximum: int) -> tuple[bytes, float]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not_regular_file")
        if info.st_size < 2 or info.st_size > maximum:
            raise ValueError("file_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("file_too_large")
        after = os.fstat(fd)
        before_coordinate = (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        after_coordinate = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if after_coordinate != before_coordinate or len(raw) != after.st_size:
            raise ValueError("file_changed_during_read")
        published_at = max(float(after.st_mtime), float(after.st_ctime))
        return raw, published_at
    finally:
        os.close(fd)


def ingest_worker_progress(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
) -> tuple[ProgressOutcome, ...]:
    """Consume sequenced worker progress and derive lease evidence ourselves."""

    reason = _fence_reason(
        conn,
        task_id,
        run_id,
        fence,
        controller_epoch,
        check_expiry=False,
    )
    if reason is not None:
        return (ProgressOutcome(False, reason, 0),)
    directory = _progress_directory(run_id, fence)
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        return ()
    if directory.is_symlink() or not stat.S_ISDIR(directory_info.st_mode):
        return (ProgressOutcome(False, "progress_directory_not_regular", 0),)
    candidates: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        match = re.fullmatch(r"progress-([1-9][0-9]*)\.json", path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    outcomes: list[ProgressOutcome] = []
    for sequence, path in sorted(candidates)[:32]:
        consumed = conn.execute(
            "SELECT outcome, evidence_digest FROM workflow_progress_proposals "
            "WHERE run_id=? AND sequence=?",
            (run_id, sequence),
        ).fetchone()
        if consumed is not None:
            path.unlink(missing_ok=True)
            outcomes.append(
                ProgressOutcome(
                    False,
                    "progress_sequence_consumed",
                    sequence,
                    consumed["evidence_digest"],
                )
            )
            continue
        try:
            raw, published_at = _read_bounded_regular_file(
                path, maximum=_MAX_PROGRESS_BYTES
            )
            proposal = json.loads(raw.decode("utf-8", errors="strict"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            outcomes.append(
                ProgressOutcome(False, "invalid_progress_proposal", sequence)
            )
            continue
        reason = _fence_reason(
            conn,
            task_id,
            run_id,
            fence,
            controller_epoch,
            published_at=published_at,
        )
        if reason is not None:
            outcomes.append(ProgressOutcome(False, reason, sequence))
            continue
        required = _PROGRESS_SCHEMA - {"summary"}
        if (
            not isinstance(proposal, dict)
            or not required.issubset(proposal)
            or any(key not in _PROGRESS_SCHEMA for key in proposal)
            or proposal.get("schema") != "hermes.workflow-progress.v1"
            or proposal.get("task_id") != task_id
            or type(proposal.get("run_id")) is not int
            or proposal.get("run_id") != run_id
            or proposal.get("fence") != fence
            or proposal.get("controller_epoch") != controller_epoch
            or proposal.get("state") != "running"
            or type(proposal.get("sequence")) is not int
            or proposal.get("sequence") != sequence
            or ("summary" in proposal and not isinstance(proposal["summary"], str))
        ):
            outcomes.append(
                ProgressOutcome(False, "invalid_progress_proposal", sequence)
            )
            continue
        evidence = submit_evidence_proposal(
            conn,
            task_id,
            run_id=run_id,
            fence=fence,
            controller_epoch=controller_epoch,
            published_at=published_at,
        )
        proposal_digest = hashlib.sha256(raw).hexdigest()
        outcome_reason = "evidence_accepted" if evidence.accepted else evidence.reason
        with write_txn(conn):
            conn.execute(
                "INSERT INTO workflow_progress_proposals "
                "(run_id, sequence, proposal_digest, evidence_digest, outcome, consumed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    sequence,
                    proposal_digest,
                    evidence.digest,
                    outcome_reason,
                    int(time.time()),
                ),
            )
            kb._append_event(
                conn,
                task_id,
                "execution_progress_consumed",
                {
                    "sequence": sequence,
                    "accepted": evidence.accepted,
                    "reason": outcome_reason,
                    "evidence_digest": evidence.digest,
                },
                run_id=run_id,
            )
        path.unlink(missing_ok=True)
        outcomes.append(
            ProgressOutcome(
                evidence.accepted, outcome_reason, sequence, evidence.digest
            )
        )
    return tuple(outcomes)


def _proposal_path(run_id: int, fence: str) -> Path:
    return (
        kb.kanban_home()
        / "kanban"
        / "workflow-worker-proposals"
        / f"run-{int(run_id)}-{hashlib.sha256(fence.encode()).hexdigest()[:16]}.json"
    )


def ingest_worker_proposal(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
) -> ResultOutcome:
    """Consume one bounded regular-file proposal produced by the real worker."""

    reason = _fence_reason(
        conn,
        task_id,
        run_id,
        fence,
        controller_epoch,
        check_expiry=False,
    )
    if reason is not None:
        return ResultOutcome(False, reason)
    path = _proposal_path(run_id, fence)
    try:
        raw, published_at = _read_bounded_regular_file(
            path, maximum=_MAX_PROPOSAL_BYTES
        )
        proposal = json.loads(raw.decode("utf-8", errors="strict"))
    except FileNotFoundError:
        return ResultOutcome(False, "proposal_missing")
    except ValueError as exc:
        reason = str(exc)
        if reason == "not_regular_file":
            return ResultOutcome(False, "proposal_not_regular_file")
        if reason == "file_too_large":
            return ResultOutcome(False, "proposal_too_large")
        return ResultOutcome(False, "invalid_proposal_schema")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ResultOutcome(False, "invalid_proposal_schema")
    reason = _fence_reason(
        conn,
        task_id,
        run_id,
        fence,
        controller_epoch,
        published_at=published_at,
    )
    if reason is not None:
        return ResultOutcome(False, reason)
    if not isinstance(proposal, Mapping):
        return ResultOutcome(False, "invalid_proposal_schema")
    outcome = submit_result_proposal(
        conn,
        task_id,
        run_id=run_id,
        fence=fence,
        controller_epoch=controller_epoch,
        proposal=proposal,
        published_at=published_at,
    )
    if outcome.accepted:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return outcome


def _candidate_coordinate(task: kb.Task) -> tuple[str, str]:
    if not task.workspace_path or not task.pin_sha:
        raise ValueError("task source coordinate missing")
    repo = Path(task.workspace_path)
    candidate_sha = _git(repo, "rev-parse", "HEAD^{commit}").strip().lower()
    if candidate_sha == task.pin_sha:
        raise ValueError("candidate head has no commit beyond pin")
    diff = _git(repo, "diff", "--binary", "--no-ext-diff", task.pin_sha, candidate_sha)
    return candidate_sha, hashlib.sha256(diff.encode("utf-8")).hexdigest()


def begin_review_closeout(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    required_ci: bool,
) -> ReviewCoordinate:
    reason = _fence_reason(
        conn, task_id, run_id, fence, controller_epoch, allow_review=True
    )
    if reason is not None:
        raise ValueError(reason)
    task = kb.get_task(conn, task_id)
    if task is None or task.status != "review":
        raise ValueError("task_not_in_review")
    candidate_sha, diff_digest = _candidate_coordinate(task)
    now = int(time.time())
    with write_txn(conn):
        existing = conn.execute(
            "SELECT candidate_sha, diff_digest, required_ci FROM workflow_run_closeout WHERE run_id=? AND task_id=?",
            (run_id, task_id),
        ).fetchone()
        if existing is not None and (
            existing["candidate_sha"] != candidate_sha
            or existing["diff_digest"] != diff_digest
            or bool(existing["required_ci"]) != bool(required_ci)
        ):
            raise ValueError("review_coordinate_already_frozen")
        conn.execute(
            "INSERT OR IGNORE INTO workflow_run_closeout "
            "(run_id, task_id, candidate_sha, diff_digest, required_ci, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, task_id, candidate_sha, diff_digest, int(required_ci), now, now),
        )
        kb._append_event(
            conn,
            task_id,
            "execution_review_frozen",
            {
                "candidate_sha": candidate_sha,
                "diff_digest": diff_digest,
                "required_ci": bool(required_ci),
            },
            run_id=run_id,
        )
    return ReviewCoordinate(candidate_sha, diff_digest, bool(required_ci))


def record_review_verdict(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    reviewer: str,
    candidate_sha: str,
    diff_digest: str,
    checklist: Mapping[str, object],
    approved: bool,
) -> CloseoutOutcome:
    reason = _fence_reason(
        conn, task_id, run_id, fence, controller_epoch, allow_review=True
    )
    if reason is not None:
        return CloseoutOutcome(False, reason)
    reviewer = _required_text("reviewer", reviewer)
    if not checklist or any(value is not True for value in checklist.values()):
        return CloseoutOutcome(False, "review_checklist_incomplete")
    task = kb.get_task(conn, task_id)
    if task is None:
        return CloseoutOutcome(False, "task_missing")
    try:
        live_sha, live_digest = _candidate_coordinate(task)
    except (ValueError, OSError, subprocess.SubprocessError):
        return CloseoutOutcome(False, "candidate_unavailable")
    row = conn.execute(
        "SELECT candidate_sha, diff_digest FROM workflow_run_closeout WHERE run_id=? AND task_id=?",
        (run_id, task_id),
    ).fetchone()
    if (
        row is None
        or candidate_sha != row["candidate_sha"]
        or diff_digest != row["diff_digest"]
    ):
        return CloseoutOutcome(False, "review_coordinate_mismatch")
    if live_sha != candidate_sha or live_digest != diff_digest:
        return CloseoutOutcome(False, "candidate_head_changed")
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            "UPDATE workflow_run_closeout SET reviewer=?, review_approved=?, review_checklist=?, "
            "review_recorded_at=?, updated_at=? WHERE run_id=? AND task_id=?",
            (
                reviewer,
                int(bool(approved)),
                _canonical_json(dict(checklist)),
                now,
                now,
                run_id,
                task_id,
            ),
        )
        kb._append_event(
            conn,
            task_id,
            "execution_review_recorded",
            {
                "reviewer": reviewer,
                "candidate_sha": candidate_sha,
                "approved": bool(approved),
            },
            run_id=run_id,
        )
    return CloseoutOutcome(True, "recorded", "review")


def record_ci_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    candidate_sha: str,
    check_suite: str,
    conclusion: str,
) -> CloseoutOutcome:
    reason = _fence_reason(
        conn, task_id, run_id, fence, controller_epoch, allow_review=True
    )
    if reason is not None:
        return CloseoutOutcome(False, reason)
    row = conn.execute(
        "SELECT candidate_sha FROM workflow_run_closeout WHERE run_id=? AND task_id=?",
        (run_id, task_id),
    ).fetchone()
    if row is None or row["candidate_sha"] != candidate_sha:
        return CloseoutOutcome(False, "ci_sha_mismatch")
    conclusion = _required_text("conclusion", conclusion).lower()
    if conclusion not in {"success", "failure", "cancelled", "timed_out", "skipped"}:
        return CloseoutOutcome(False, "invalid_ci_conclusion")
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            "UPDATE workflow_run_closeout SET ci_sha=?, ci_suite=?, ci_conclusion=?, ci_recorded_at=?, updated_at=? "
            "WHERE run_id=? AND task_id=?",
            (
                candidate_sha,
                _required_text("check_suite", check_suite),
                conclusion,
                now,
                now,
                run_id,
                task_id,
            ),
        )
        failure = None
        if conclusion != "success":
            failure = _increment_failure_count(conn, task_id, "ci", now=now)
        kb._append_event(
            conn,
            task_id,
            "execution_ci_recorded",
            {
                "candidate_sha": candidate_sha,
                "check_suite": check_suite,
                "conclusion": conclusion,
                "failure_action": failure.action if failure else None,
            },
            run_id=run_id,
        )
    return CloseoutOutcome(True, "recorded", "review")


def _workspace_is_clean(task: kb.Task) -> bool:
    if not task.workspace_path:
        return False
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            task.workspace_path,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
        check=True,
        capture_output=True,
        timeout=10,
        env=_hardened_git_env(),
    )
    return result.stdout == b""


def _record_closeout_failure(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    run_id: int,
    fence: str,
    reason: str,
    failure_class: str,
) -> None:
    existing = conn.execute(
        "SELECT quarantine_reason FROM task_runs WHERE id=? AND task_id=? AND claim_lock=?",
        (run_id, task_id, fence),
    ).fetchone()
    if existing is None or existing["quarantine_reason"] == reason:
        return
    now = int(time.time())
    with write_txn(conn):
        failure = _increment_failure_count(conn, task_id, failure_class, now=now)
        conn.execute(
            "UPDATE task_runs SET failure_class=?, error=?, quarantine_reason=? "
            "WHERE id=? AND task_id=? AND status='reviewing' AND claim_lock=? AND ended_at IS NULL",
            (failure_class, reason, reason, run_id, task_id, fence),
        )
        kb._append_event(
            conn,
            task_id,
            "execution_closeout_quarantined",
            {
                "reason": reason,
                "failure_class": failure_class,
                "action": failure.action,
            },
            run_id=run_id,
        )


def close_reviewed_leaf(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    process_inspector: Optional[ProcessInspector] = None,
) -> CloseoutOutcome:
    reason = _fence_reason(
        conn, task_id, run_id, fence, controller_epoch, allow_review=True
    )
    if reason is not None:
        return CloseoutOutcome(False, reason)
    task = kb.get_task(conn, task_id)
    if task is None:
        return CloseoutOutcome(False, "task_missing")
    row = conn.execute(
        "SELECT * FROM workflow_run_closeout WHERE run_id=? AND task_id=?",
        (run_id, task_id),
    ).fetchone()
    if row is None or row["review_approved"] != 1:
        return CloseoutOutcome(False, "review_not_approved")
    run = conn.execute(
        "SELECT worker_pid, process_identity FROM task_runs "
        "WHERE id=? AND task_id=? AND status='reviewing' AND claim_lock=? AND ended_at IS NULL",
        (run_id, task_id, fence),
    ).fetchone()
    process_state = "unknown"
    if run is not None and run["worker_pid"] is not None and run["process_identity"]:
        try:
            identity = json.loads(run["process_identity"])
        except (TypeError, ValueError, json.JSONDecodeError):
            identity = None
        if isinstance(identity, dict) and identity.get("pid") == run["worker_pid"]:
            process_state = (process_inspector or HostProcessInspector()).inspect(
                identity
            )
    if process_state != "dead":
        closeout_reason = (
            "worker_still_alive" if process_state == "alive" else "worker_state_unknown"
        )
        _record_closeout_failure(
            conn,
            task_id=task_id,
            run_id=run_id,
            fence=fence,
            reason=closeout_reason,
            failure_class="scope_ambiguity",
        )
        return CloseoutOutcome(False, closeout_reason, "review")
    try:
        clean = _workspace_is_clean(task)
    except (OSError, subprocess.SubprocessError):
        clean = False
    if not clean:
        _record_closeout_failure(
            conn,
            task_id=task_id,
            run_id=run_id,
            fence=fence,
            reason="workspace_dirty",
            failure_class="content",
        )
        return CloseoutOutcome(False, "workspace_dirty", "review")
    try:
        live_sha, live_digest = _candidate_coordinate(task)
    except (ValueError, OSError, subprocess.SubprocessError):
        return CloseoutOutcome(False, "candidate_unavailable")
    if live_sha != row["candidate_sha"] or live_digest != row["diff_digest"]:
        now = int(time.time())
        with write_txn(conn):
            conn.execute(
                "UPDATE workflow_run_closeout SET invalidated_at=?, invalidation_reason='candidate_head_changed', updated_at=? WHERE run_id=?",
                (now, now, run_id),
            )
            kb._append_event(
                conn,
                task_id,
                "execution_review_invalidated",
                {"reason": "candidate_head_changed", "observed_sha": live_sha},
                run_id=run_id,
            )
        return CloseoutOutcome(False, "candidate_head_changed")
    if row["required_ci"] and not (
        row["ci_sha"] == row["candidate_sha"] and row["ci_conclusion"] == "success"
    ):
        return CloseoutOutcome(False, "protected_ci_not_green")
    now = int(time.time())
    with write_txn(conn):
        task_update = conn.execute(
            "UPDATE tasks SET status='done', completed_at=?, current_run_id=NULL, claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=? AND status='review' AND current_run_id=? AND claim_lock=?",
            (now, task_id, run_id, fence),
        )
        run_update = conn.execute(
            "UPDATE task_runs SET status='done', outcome='completed', ended_at=?, "
            "claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, quarantine_reason=NULL "
            "WHERE id=? AND task_id=? AND status='reviewing' AND claim_lock=? AND ended_at IS NULL",
            (now, run_id, task_id, fence),
        )
        if task_update.rowcount != 1 or run_update.rowcount != 1:
            raise RuntimeError("closeout lost its execution fence")
        kb._append_event(
            conn,
            task_id,
            "execution_closed",
            {"candidate_sha": live_sha},
            run_id=run_id,
        )
    if task.workspace_path:
        _release_workspace_reservation(
            conn, workspace_path=task.workspace_path, task_id=task_id, claim_lock=fence
        )
    return CloseoutOutcome(True, "closed", "done")


_FAILURE_POLICIES = {
    "launch": (2, "retry", "inspect_infrastructure"),
    "content": (1, "replan", "block"),
    "ci": (1, "flake_rerun", "implementation_correction"),
    "scope_ambiguity": (0, "quarantine", "quarantine"),
    "owner_decision": (0, "await_owner", "await_owner"),
    "external_dependency": (0, "block_external", "block_external"),
}


def _increment_failure_count(
    conn: sqlite3.Connection, task_id: str, failure_class: str, *, now: int
) -> FailureOutcome:
    ceiling, retry_action, exhausted_action = _FAILURE_POLICIES[failure_class]
    conn.execute(
        "INSERT INTO workflow_failure_counts (task_id, failure_class, count, updated_at) VALUES (?, ?, 1, ?) "
        "ON CONFLICT(task_id, failure_class) DO UPDATE SET count=count+1, updated_at=excluded.updated_at",
        (task_id, failure_class, now),
    )
    row = conn.execute(
        "SELECT count FROM workflow_failure_counts WHERE task_id=? AND failure_class=?",
        (task_id, failure_class),
    ).fetchone()
    count = int(row["count"])
    action = retry_action if ceiling > 0 and count < ceiling else exhausted_action
    return FailureOutcome(failure_class, count, action)


def record_workflow_failure(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: int,
    fence: str,
    controller_epoch: str,
    failure_class: str,
    detail: str,
) -> FailureOutcome:
    reason = _fence_reason(
        conn, task_id, run_id, fence, controller_epoch, allow_review=True
    )
    if reason is not None:
        raise ValueError(reason)
    failure_class = _required_text("failure_class", failure_class).lower()
    if failure_class not in _FAILURE_POLICIES:
        raise ValueError("unknown Workflow failure class")
    now = int(time.time())
    with write_txn(conn):
        outcome = _increment_failure_count(conn, task_id, failure_class, now=now)
        conn.execute(
            "UPDATE task_runs SET failure_class=?, error=? WHERE id=? AND task_id=? AND claim_lock=?",
            (failure_class, detail[:1000], run_id, task_id, fence),
        )
        kb._append_event(
            conn,
            task_id,
            "execution_failure_recorded",
            {
                "failure_class": failure_class,
                "count": outcome.count,
                "action": outcome.action,
                "detail": detail[:1000],
            },
            run_id=run_id,
        )
    return outcome


def ingest_github_snapshot(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    snapshot: GitHubSnapshot,
) -> GitHubSnapshotOutcome:
    if kb.get_task(conn, task_id) is None:
        raise ValueError("task_missing")
    payload = snapshot.payload()
    content_hash = _hash_payload(payload)
    material_payload = {
        "repository_node_id": payload["repository_node_id"],
        "issue_node_id": payload["issue_node_id"],
        "project_item_id": payload["project_item_id"],
        "issue": payload["issue"],
        "project": payload["project"],
        "pull_requests": payload["pull_requests"],
    }
    material_hash = _hash_payload(material_payload)
    latest = conn.execute(
        "SELECT version, content_hash, material_hash FROM workflow_github_snapshots WHERE task_id=? ORDER BY version DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest is not None and latest["content_hash"] == content_hash:
        return GitHubSnapshotOutcome(int(latest["version"]), content_hash, False, False)
    version = 1 if latest is None else int(latest["version"]) + 1
    material_change = latest is None or latest["material_hash"] != material_hash
    with write_txn(conn):
        conn.execute(
            "INSERT INTO workflow_github_snapshots "
            "(task_id, version, repository_node_id, issue_node_id, project_item_id, source_updated_at, source_version, content_hash, material_hash, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                version,
                payload["repository_node_id"],
                payload["issue_node_id"],
                payload["project_item_id"],
                payload["source_updated_at"],
                payload["source_version"],
                content_hash,
                material_hash,
                _canonical_json(payload),
                int(time.time()),
            ),
        )
        kb._append_event(
            conn,
            task_id,
            "github_snapshot_ingested",
            {
                "version": version,
                "content_hash": content_hash,
                "material_change": material_change,
            },
        )
    return GitHubSnapshotOutcome(version, content_hash, True, material_change)


def project_github_status(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    writer: GitHubProjectionWriter,
    status: str,
    expected_updated_at: str,
) -> GitHubProjectionOutcome:
    latest = conn.execute(
        "SELECT project_item_id FROM workflow_github_snapshots WHERE task_id=? ORDER BY version DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest is None:
        raise GitHubProjectionConflict("canonical GitHub snapshot missing")
    project_item_id = str(latest["project_item_id"])
    status = _required_text("status", status)
    expected_updated_at = _required_text("expected_updated_at", expected_updated_at)
    writer.write_status(
        project_item_id=project_item_id,
        status=status,
        expected_updated_at=expected_updated_at,
    )
    observed = writer.read_status(project_item_id=project_item_id)
    observed_status = str(observed.get("status") or "")
    observed_updated_at = str(observed.get("updated_at") or "")
    if observed_status != status or not observed_updated_at:
        raise GitHubProjectionConflict("GitHub Project write read-back mismatch")
    with write_txn(conn):
        conn.execute(
            "INSERT INTO workflow_github_projections (task_id, project_item_id, expected_updated_at, observed_updated_at, status, verified, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (
                task_id,
                project_item_id,
                expected_updated_at,
                observed_updated_at,
                status,
                int(time.time()),
            ),
        )
        kb._append_event(
            conn,
            task_id,
            "github_projection_verified",
            {
                "project_item_id": project_item_id,
                "status": status,
                "observed_updated_at": observed_updated_at,
            },
        )
    return GitHubProjectionOutcome(status, observed_updated_at, True)


def _latest_github_snapshot(
    conn: sqlite3.Connection, task_id: str
) -> Optional[GitHubSnapshot]:
    row = conn.execute(
        "SELECT payload FROM workflow_github_snapshots WHERE task_id=? "
        "ORDER BY version DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return GitHubSnapshot(
        repository_node_id=payload["repository_node_id"],
        issue_node_id=payload["issue_node_id"],
        project_item_id=payload["project_item_id"],
        source_updated_at=payload["source_updated_at"],
        source_version=payload["source_version"],
        issue=payload["issue"],
        project=payload["project"],
        pull_requests=tuple(payload["pull_requests"]),
    )


class WorkflowProductionCoordinator:
    """Small controller seam joining canonical GitHub facts to exact closeout."""

    def __init__(
        self,
        adapter: WorkflowCoordinatorAdapter,
        *,
        process_inspector: Optional[ProcessInspector] = None,
    ):
        self.adapter = adapter
        self.process_inspector = process_inspector or HostProcessInspector()

    def _record_failure(
        self,
        conn: sqlite3.Connection,
        task: kb.Task,
        *,
        failure_class: str,
        detail: str,
        controller_epoch: str,
    ) -> None:
        if task.current_run_id is None or not task.claim_lock:
            return
        record_workflow_failure(
            conn,
            task.id,
            run_id=int(task.current_run_id),
            fence=str(task.claim_lock),
            controller_epoch=controller_epoch,
            failure_class=failure_class,
            detail=detail,
        )

    def tick(self, conn: sqlite3.Connection, *, controller_epoch: str) -> None:
        if self.adapter.enabled is not True:
            return
        task_ids = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM tasks WHERE leaf_key IS NOT NULL "
                "AND status IN ('running','review') ORDER BY created_at, id"
            ).fetchall()
        ]
        for task_id in task_ids:
            task = kb.get_task(conn, task_id)
            if task is None:
                continue
            try:
                observed = self.adapter.fetch_snapshot(task)
                if observed is not None:
                    ingest_github_snapshot(conn, task_id=task_id, snapshot=observed)
                snapshot = observed or _latest_github_snapshot(conn, task_id)
                if task.status != "review" or snapshot is None:
                    continue
                assert task.current_run_id is not None and task.claim_lock
                run_id = int(task.current_run_id)
                fence = str(task.claim_lock)
                required_ci = self.adapter.requires_protected_ci(task, snapshot)
                coordinate = begin_review_closeout(
                    conn,
                    task_id,
                    run_id=run_id,
                    fence=fence,
                    controller_epoch=controller_epoch,
                    required_ci=required_ci,
                )
                closeout = conn.execute(
                    "SELECT review_approved, ci_sha, ci_conclusion FROM workflow_run_closeout "
                    "WHERE run_id=? AND task_id=?",
                    (run_id, task_id),
                ).fetchone()
                if closeout is None or closeout["review_approved"] != 1:
                    review = self.adapter.verify_review(task, coordinate)
                    if review is None:
                        continue
                    recorded = record_review_verdict(
                        conn,
                        task_id,
                        run_id=run_id,
                        fence=fence,
                        controller_epoch=controller_epoch,
                        reviewer=review.reviewer,
                        candidate_sha=review.candidate_sha,
                        diff_digest=review.diff_digest,
                        checklist=review.checklist,
                        approved=review.approved,
                    )
                    if not recorded.accepted or not review.approved:
                        raise WorkflowCoordinationFailure("content", recorded.reason)
                closeout = conn.execute(
                    "SELECT review_approved, ci_sha, ci_conclusion FROM workflow_run_closeout "
                    "WHERE run_id=? AND task_id=?",
                    (run_id, task_id),
                ).fetchone()
                if required_ci and not (
                    closeout is not None
                    and closeout["ci_sha"] == coordinate.candidate_sha
                    and closeout["ci_conclusion"] == "success"
                ):
                    ci = self.adapter.protected_ci(task, coordinate)
                    if ci is None:
                        continue
                    ci_recorded = record_ci_result(
                        conn,
                        task_id,
                        run_id=run_id,
                        fence=fence,
                        controller_epoch=controller_epoch,
                        candidate_sha=ci.candidate_sha,
                        check_suite=ci.check_suite,
                        conclusion=ci.conclusion,
                    )
                    if not ci_recorded.accepted:
                        raise WorkflowCoordinationFailure("ci", ci_recorded.reason)
                    if ci.conclusion != "success":
                        continue
                projection = self.adapter.projection_request(task, snapshot, coordinate)
                if projection is not None:
                    project_github_status(
                        conn,
                        task_id=task_id,
                        writer=self.adapter,
                        status=projection.status,
                        expected_updated_at=projection.expected_updated_at,
                    )
                closed = close_reviewed_leaf(
                    conn,
                    task_id,
                    run_id=run_id,
                    fence=fence,
                    controller_epoch=controller_epoch,
                    process_inspector=self.process_inspector,
                )
                if not closed.accepted and closed.reason not in {
                    "worker_still_alive",
                    "worker_state_unknown",
                    "workspace_dirty",
                }:
                    raise WorkflowCoordinationFailure("content", closed.reason)
            except WorkflowCoordinationFailure as exc:
                self._record_failure(
                    conn,
                    task,
                    failure_class=exc.failure_class,
                    detail=exc.detail,
                    controller_epoch=controller_epoch,
                )
            except GitHubProjectionConflict as exc:
                self._record_failure(
                    conn,
                    task,
                    failure_class="scope_ambiguity",
                    detail=str(exc),
                    controller_epoch=controller_epoch,
                )


def reconcile_runtime_reservations(
    conn: sqlite3.Connection, *, process_alive=None
) -> ReconciliationReport:
    """Quarantine PID-less reservations; close only positive pre-spawn failures."""

    _ = process_alive  # missing PID is intentionally not interpreted via this hook
    quarantined: list[str] = []
    findings: list[str] = []
    rows = conn.execute(
        "SELECT t.id, t.current_run_id, r.worker_pid, r.quarantine_reason "
        "FROM tasks t JOIN task_runs r ON r.id=t.current_run_id "
        "WHERE t.leaf_key IS NOT NULL AND t.claim_lock IS NOT NULL AND r.status='reserved' "
        "AND r.ended_at IS NULL"
    ).fetchall()
    for row in rows:
        reason = (
            "pidless_reservation_ambiguous"
            if row["worker_pid"] is None
            else str(row["quarantine_reason"] or "reserved_process_ambiguous")
        )
        if row["quarantine_reason"] != reason:
            with write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET quarantine_reason=? WHERE id=? AND status='reserved'",
                    (reason, int(row["current_run_id"])),
                )
                kb._append_event(
                    conn,
                    row["id"],
                    "execution_quarantined",
                    {"reasons": [reason]},
                    run_id=int(row["current_run_id"]),
                )
        quarantined.append(row["id"])
        findings.append(reason)
    return ReconciliationReport(
        (), (), tuple(quarantined), tuple(dict.fromkeys(findings))
    )


def run_workflow_runtime_tick(
    conn: sqlite3.Connection,
    *,
    controller_epoch: str,
    launcher: WorkflowLauncher,
    max_launch: int = 1,
    launch_enabled: bool = True,
    coordinator: Optional[WorkflowProductionCoordinator] = None,
) -> tuple[DispatchOutcome, ...]:
    """Consume worker channels and coordinate closeout before gated dispatch."""

    running = conn.execute(
        "SELECT t.id, t.current_run_id, t.claim_lock FROM tasks t "
        "WHERE t.leaf_key IS NOT NULL AND t.lease_policy='evidence' "
        "AND t.status='running' AND t.current_run_id IS NOT NULL AND t.claim_lock IS NOT NULL"
    ).fetchall()
    for row in running:
        run_id = int(row["current_run_id"])
        fence = str(row["claim_lock"])
        ingest_worker_progress(
            conn,
            row["id"],
            run_id=run_id,
            fence=fence,
            controller_epoch=controller_epoch,
        )
        if _proposal_path(run_id, fence).exists():
            ingest_worker_proposal(
                conn,
                row["id"],
                run_id=run_id,
                fence=fence,
                controller_epoch=controller_epoch,
            )
    (
        coordinator or WorkflowProductionCoordinator(PausedWorkflowCoordinatorAdapter())
    ).tick(conn, controller_epoch=controller_epoch)
    if not launch_enabled or not _controller_dispatchable(conn, controller_epoch):
        return ()
    outcomes: list[DispatchOutcome] = []
    rows = conn.execute(
        "SELECT id FROM tasks WHERE leaf_key IS NOT NULL AND lease_policy='evidence' AND status IN ('todo','ready') AND current_run_id IS NULL ORDER BY priority DESC, created_at, id"
    ).fetchall()
    for row in rows:
        if len(outcomes) >= max(0, int(max_launch)):
            break
        if validate_execution_readiness(conn, row["id"]).ready:
            outcomes.append(
                dispatch_execution_leaf(
                    conn,
                    row["id"],
                    controller_epoch=controller_epoch,
                    launcher=launcher,
                )
            )
    return tuple(outcomes)
