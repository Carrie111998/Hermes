"""Local, read-only operator diagnostics for long-running Hermes jobs.

The runtime writes small, profile-scoped JSON records under
``$HERMES_HOME/jobs/diagnostics``.  Operator commands only read those files:
they never launch, stop, retry, resume, or otherwise mutate a running job.

The public ``JobRun`` helper is intentionally separate from the operator
commands.  Long-job runners can use it to persist phase checkpoints and make
phase execution idempotent.  A resume is permitted only when the exact
worktree, branch, HEAD, dirty-tree fingerprint, and checkpoint evidence still
match.  Repository drift fails closed before the callback is invoked.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar

from hermes_constants import get_hermes_home


SCHEMA_VERSION = 1
DEFAULT_IDLE_AFTER_SECONDS = 300.0
DEFAULT_STALE_AFTER_SECONDS = 900.0
DEFAULT_MEANINGFUL_OUTPUT_WARNING_SECONDS = 600.0
DEFAULT_HEARTBEAT_REPEAT_SECONDS = 540.0
_MAX_STEP_CHARS = 240
_MAX_SUMMARY_CHARS = 500
_GIT_TIMEOUT_SECONDS = 5.0


class TimingCategory(str, Enum):
    """Canonical timing buckets persisted for every job."""

    MODEL_WAIT = "model_wait"
    TOOL_EXECUTION = "tool_execution"
    TEST = "test"
    REVIEW = "review"
    BLOCKED_IDLE = "blocked_idle"
    EVIDENCE_GENERATION = "evidence_generation"
    COMPRESSION = "compression"


TIMING_CATEGORY_ORDER = tuple(category.value for category in TimingCategory)


class BlockerKind(str, Enum):
    """Closed blocker vocabulary used by diagnostics and resume checks."""

    CODE_FAILURE = "code_failure"
    TEST_FAILURE = "test_failure"
    MISSING_AUTHORIZATION = "missing_authorization"
    OPERATOR_PRESENCE_REQUIREMENT = "operator_presence_requirement"
    EXTERNAL_PROCESS_CONFLICT = "external_process_conflict"
    WRONG_WORKTREE_OR_BRANCH = "wrong_worktree_or_branch"
    HASH_MISMATCH = "hash_mismatch"
    REMOTE_PROVIDER_FAILURE = "remote_or_provider_failure"
    STALE_SESSION = "stale_session"
    INFRASTRUCTURE_ISSUE = "infrastructure_issue"


class LaneStatus(str, Enum):
    PENDING = "pending"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    DEAD = "dead"


_TERMINAL_TOOL_NAMES = {"terminal", "execute_code"}
_TEST_COMMAND_RE = re.compile(
    r"(?:^|[\s/;|&])(?:"
    r"pytest|python\s+-m\s+pytest|scripts/run_tests\.sh|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+test|"
    r"cargo\s+test|go\s+test|dotnet\s+test|swift\s+test"
    r")(?:\s|$)",
    re.IGNORECASE,
)
_REVIEW_COMMAND_RE = re.compile(
    r"(?:^|[\s/;|&])(?:"
    r"git\s+(?:diff|status|show)|ruff\s+check|"
    r"ty\s+check|mypy|pyright|tsc(?:\s|$)|npm\s+run\s+(?:lint|typecheck)"
    r")",
    re.IGNORECASE,
)
_EVIDENCE_COMMAND_RE = re.compile(
    r"(?:^|[\s/;|&])(?:"
    r"sha256sum|shasum|openssl\s+dgst|git\s+rev-parse|"
    r"render_docx\.py|pdftoppm|build-report|evidence"
    r")(?:\s|$)",
    re.IGNORECASE,
)

_AUTH_RE = re.compile(
    r"\b(?:unauthori[sz]ed|forbidden|permission denied|approval required|"
    r"credentials? missing|missing credentials?|authentication required|"
    r"requires? authori[sz]ation)\b",
    re.IGNORECASE,
)
_OPERATOR_RE = re.compile(
    r"\b(?:operator presence|human input|user input|required interaction|"
    r"touch id|confirm locally|manual approval|waiting for (?:the )?user)\b",
    re.IGNORECASE,
)
_PROCESS_CONFLICT_RE = re.compile(
    r"\b(?:already running|lock(?:ed)? by|resource busy|address already in use|"
    r"another process|concurrent writer|duplicate execution)\b",
    re.IGNORECASE,
)
_WORKTREE_RE = re.compile(
    r"\b(?:wrong worktree|wrong branch|branch mismatch|head mismatch|"
    r"repository drift|not a git repository|detached head)\b",
    re.IGNORECASE,
)
_HASH_RE = re.compile(
    r"\b(?:hash mismatch|checksum mismatch|digest mismatch|sha256 mismatch)\b",
    re.IGNORECASE,
)
_REMOTE_RE = re.compile(
    r"\b(?:provider|upstream|remote|rate limit|429|502|503|504|"
    r"connection reset|timed? out|overloaded|service unavailable)\b",
    re.IGNORECASE,
)
_STALE_RE = re.compile(
    r"\b(?:stale session|session expired|process disappeared|lost process|"
    r"pid reused|heartbeat stale)\b",
    re.IGNORECASE,
)
_INFRA_RE = re.compile(
    r"\b(?:no space left|disk full|out of memory|oom|dns|network unreachable|"
    r"connection refused|filesystem|i/o error|infrastructure)\b",
    re.IGNORECASE,
)


class JobDiagnosticsError(RuntimeError):
    """Base error for the diagnostics state layer."""


class JobNotFoundError(JobDiagnosticsError):
    pass


class MalformedJobStateError(JobDiagnosticsError):
    pass


class RepositoryDriftError(JobDiagnosticsError):
    """Raised before a phase callback when checkpoint identity drifted."""

    def __init__(self, reasons: Sequence[str], blocker: BlockerKind):
        self.reasons = tuple(reasons)
        self.blocker = blocker
        super().__init__("; ".join(self.reasons))


class DuplicatePhaseError(JobDiagnosticsError):
    pass


@dataclass(frozen=True)
class StateIssue:
    path: str
    kind: str
    detail: str


@dataclass(frozen=True)
class PhaseExecution:
    executed: bool
    value: Any = None
    reason: str = ""


@dataclass(frozen=True)
class ResumePlan:
    safe: bool
    job_id: str
    lane_id: str
    phase_id: str | None
    command: str | None
    worktree: str | None
    evidence_paths: tuple[str, ...]
    blocker: str | None
    reasons: tuple[str, ...]
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe": self.safe,
            "job_id": self.job_id,
            "lane_id": self.lane_id,
            "phase_id": self.phase_id,
            "command": self.command,
            "worktree": self.worktree,
            "evidence_paths": list(self.evidence_paths),
            "blocker": self.blocker,
            "reasons": list(self.reasons),
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class HeartbeatUpdate:
    status: str
    text: str
    signature: tuple[Any, ...]


_T = TypeVar("_T")
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _now_iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _clean_text(value: Any, limit: int = _MAX_SUMMARY_CHARS) -> str:
    text = " ".join(str(value or "").split())
    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _slug(value: str, limit: int = 28) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return (cleaned or "job")[:limit]


def _state_filename(job_id: str) -> str:
    digest = hashlib.sha256(job_id.encode("utf-8", "replace")).hexdigest()[:20]
    return f"{_slug(job_id)}-{digest}.json"


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _exclusive_file_lock(path: Path):
    """Cross-process writer lock; readers remain side-effect free."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _thread_lock(lock_path):
        mode = "a+"
        handle = lock_path.open(mode, encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                if lock_path.stat().st_size == 0:
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _run_git(worktree: Path, *args: str) -> tuple[int, bytes]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, b""
    return proc.returncode, proc.stdout


def capture_repository_identity(worktree: str | Path | None) -> dict[str, Any]:
    """Capture local git identity without contacting a remote."""

    if worktree is None:
        return {"available": False, "worktree": None, "error": "worktree unavailable"}
    try:
        resolved = Path(worktree).expanduser().resolve()
    except (OSError, RuntimeError):
        return {
            "available": False,
            "worktree": str(worktree),
            "error": "worktree could not be resolved",
        }
    if not resolved.exists():
        return {
            "available": False,
            "worktree": str(resolved),
            "error": "worktree does not exist",
        }

    rc, root_raw = _run_git(resolved, "rev-parse", "--show-toplevel")
    if rc != 0:
        return {
            "available": False,
            "worktree": str(resolved),
            "error": "not a git repository",
        }
    repo_root = root_raw.decode("utf-8", "replace").strip()
    rc, head_raw = _run_git(resolved, "rev-parse", "HEAD")
    head = head_raw.decode("ascii", "replace").strip() if rc == 0 else None
    rc, branch_raw = _run_git(resolved, "symbolic-ref", "--short", "-q", "HEAD")
    branch = branch_raw.decode("utf-8", "replace").strip() if rc == 0 else "DETACHED"
    rc, status_raw = _run_git(
        resolved,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if rc != 0:
        return {
            "available": False,
            "worktree": str(resolved),
            "repo_root": repo_root,
            "branch": branch,
            "head": head,
            "error": "git status failed",
        }
    return {
        "available": True,
        "worktree": str(resolved),
        "repo_root": repo_root,
        "branch": branch,
        "head": head,
        "dirty": bool(status_raw),
        "status_digest": hashlib.sha256(status_raw).hexdigest(),
    }


def repository_drift_reasons(
    expected: Mapping[str, Any] | None,
    current: Mapping[str, Any] | None,
) -> list[str]:
    if not expected:
        return ["checkpoint repository identity is missing"]
    if not current or not current.get("available"):
        return [
            "current repository identity is unavailable"
            + (f": {current.get('error')}" if current and current.get("error") else "")
        ]
    if not expected.get("available"):
        expected_path = expected.get("worktree")
        current_path = current.get("worktree")
        return (
            []
            if expected_path == current_path
            else [f"worktree changed: expected {expected_path}, found {current_path}"]
        )

    reasons: list[str] = []
    comparisons = (
        ("worktree", "worktree"),
        ("repo_root", "repository root"),
        ("branch", "branch"),
        ("head", "HEAD"),
        ("status_digest", "working-tree fingerprint"),
    )
    for key, label in comparisons:
        if expected.get(key) != current.get(key):
            reasons.append(
                f"{label} changed: expected {expected.get(key)!r}, "
                f"found {current.get(key)!r}"
            )
    return reasons


def capture_process_identity(pid: int | None = None) -> dict[str, Any]:
    pid = int(pid or os.getpid())
    identity: dict[str, Any] = {
        "pid": pid,
        "host": socket.gethostname(),
    }
    try:
        import psutil

        proc = psutil.Process(pid)
        identity["create_time"] = proc.create_time()
        identity["name"] = proc.name()
    except Exception:
        identity["create_time"] = None
    return identity


def process_identity_status(identity: Mapping[str, Any] | None) -> str:
    """Return ``alive``, ``dead``, ``reused``, or ``unknown``."""

    if not identity or identity.get("pid") in (None, ""):
        return "unknown"
    try:
        import psutil

        pid = int(identity["pid"])
        if not psutil.pid_exists(pid):
            return "dead"
        proc = psutil.Process(pid)
        expected = identity.get("create_time")
        if expected is not None and abs(proc.create_time() - float(expected)) > 0.01:
            return "reused"
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return "dead"
        return "alive"
    except Exception:
        return "unknown"


def _evidence_identity(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    result: dict[str, Any] = {
        "path": str(resolved),
        "exists": resolved.exists(),
    }
    if not resolved.exists():
        return result
    result["kind"] = "directory" if resolved.is_dir() else "file"
    try:
        stat = resolved.stat()
        result["size"] = stat.st_size
        result["mtime_ns"] = stat.st_mtime_ns
        if resolved.is_file():
            digest = hashlib.sha256()
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result["sha256"] = digest.hexdigest()
    except OSError as exc:
        result["error"] = str(exc)
    return result


def evidence_drift_reasons(records: Iterable[Mapping[str, Any]]) -> list[str]:
    reasons: list[str] = []
    for expected in records:
        path = expected.get("path")
        if not path:
            reasons.append("checkpoint evidence path is missing")
            continue
        current = _evidence_identity(str(path))
        if not current.get("exists"):
            reasons.append(f"checkpoint evidence is missing: {path}")
            continue
        expected_hash = expected.get("sha256")
        if expected_hash and current.get("sha256") != expected_hash:
            reasons.append(f"checkpoint evidence hash changed: {path}")
    return reasons


def classify_blocker(
    detail: str,
    *,
    test_command: bool = False,
    default: BlockerKind = BlockerKind.CODE_FAILURE,
) -> BlockerKind:
    """Classify a failure using the closed operator-facing vocabulary."""

    text = str(detail or "")
    if test_command:
        return BlockerKind.TEST_FAILURE
    if _HASH_RE.search(text):
        return BlockerKind.HASH_MISMATCH
    if _WORKTREE_RE.search(text):
        return BlockerKind.WRONG_WORKTREE_OR_BRANCH
    if _AUTH_RE.search(text):
        return BlockerKind.MISSING_AUTHORIZATION
    if _OPERATOR_RE.search(text):
        return BlockerKind.OPERATOR_PRESENCE_REQUIREMENT
    if _PROCESS_CONFLICT_RE.search(text):
        return BlockerKind.EXTERNAL_PROCESS_CONFLICT
    if _STALE_RE.search(text):
        return BlockerKind.STALE_SESSION
    if _REMOTE_RE.search(text):
        return BlockerKind.REMOTE_PROVIDER_FAILURE
    if _INFRA_RE.search(text):
        return BlockerKind.INFRASTRUCTURE_ISSUE
    return default


def classify_tool_timing(tool_name: str, args: Mapping[str, Any] | None) -> str:
    if tool_name == "clarify":
        return TimingCategory.BLOCKED_IDLE.value
    if tool_name not in _TERMINAL_TOOL_NAMES:
        return TimingCategory.TOOL_EXECUTION.value
    command = str((args or {}).get("command") or (args or {}).get("code") or "")
    if _TEST_COMMAND_RE.search(command):
        return TimingCategory.TEST.value
    if _EVIDENCE_COMMAND_RE.search(command):
        return TimingCategory.EVIDENCE_GENERATION.value
    if _REVIEW_COMMAND_RE.search(command):
        return TimingCategory.REVIEW.value
    return TimingCategory.TOOL_EXECUTION.value


def _new_job_state(job_id: str, title: str, now: float) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "title": title or job_id,
        "status": LaneStatus.PENDING.value,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "completed_at": None,
        "lanes": {},
        "spans": [],
    }


def _new_lane(
    lane_id: str,
    now: float,
    *,
    title: str,
    provider: str | None,
    model: str | None,
    worktree: str | Path | None,
    process: Mapping[str, Any] | None,
    session_id: str | None,
    task_id: str | None,
    platform: str | None,
    command: str | None,
    read_only: bool,
    depends_on: Sequence[str],
    resources: Sequence[str],
    repository_probe: Callable[[str | Path | None], dict[str, Any]],
) -> dict[str, Any]:
    repository = repository_probe(worktree)
    return {
        "lane_id": lane_id,
        "title": title or lane_id,
        "status": LaneStatus.PENDING.value,
        "status_since": now,
        "started_at": now,
        "updated_at": now,
        "completed_at": None,
        "current_step": "pending",
        "last_meaningful_output": "",
        "last_meaningful_output_at": now,
        "heartbeat_at": now,
        "retry_count": 0,
        "blocker": None,
        "next_expected_action": "",
        "provider": provider or "",
        "model": model or "",
        "process": dict(process or {}),
        "session_id": session_id or "",
        "task_id": task_id or "",
        "platform": platform or "",
        "repository": repository,
        "checkpoint": {
            "repository": repository,
            "evidence": [],
            "phase_id": None,
            "created_at": now,
        },
        "command": _clean_text(command) if command else None,
        "read_only": bool(read_only),
        "depends_on": list(dict.fromkeys(str(item) for item in depends_on)),
        "resources": list(dict.fromkeys(str(item) for item in resources)),
        "phases": [],
    }


def _phase_by_id(lane: Mapping[str, Any], phase_id: str) -> dict[str, Any] | None:
    for phase in lane.get("phases") or []:
        if isinstance(phase, dict) and phase.get("phase_id") == phase_id:
            return phase
    return None


def _append_span(
    state: dict[str, Any],
    *,
    category: str,
    lane_id: str,
    started_at: float,
    ended_at: float,
    phase_id: str | None = None,
    label: str = "",
) -> None:
    if category not in TIMING_CATEGORY_ORDER:
        raise ValueError(f"unknown timing category: {category}")
    start = float(started_at)
    end = max(start, float(ended_at))
    state.setdefault("spans", []).append({
        "category": category,
        "lane_id": lane_id,
        "phase_id": phase_id,
        "label": _clean_text(label, _MAX_STEP_CHARS),
        "started_at": start,
        "ended_at": end,
    })


_IDLE_LIKE_STATUSES = {
    LaneStatus.WAITING.value,
    LaneStatus.BLOCKED.value,
    LaneStatus.IDLE.value,
}


def _transition_lane(
    state: dict[str, Any],
    lane: dict[str, Any],
    status: str,
    now: float,
) -> None:
    previous = str(lane.get("status") or LaneStatus.PENDING.value)
    since = float(lane.get("status_since") or lane.get("updated_at") or now)
    if previous in _IDLE_LIKE_STATUSES and previous != status:
        _append_span(
            state,
            category=TimingCategory.BLOCKED_IDLE.value,
            lane_id=str(lane["lane_id"]),
            started_at=since,
            ended_at=now,
            label=previous,
        )
    if previous != status:
        lane["status_since"] = now
    lane["status"] = status
    lane["updated_at"] = now


def _recompute_job_status(state: dict[str, Any], now: float) -> None:
    lanes = [
        lane for lane in (state.get("lanes") or {}).values() if isinstance(lane, dict)
    ]
    statuses = {str(lane.get("status") or "") for lane in lanes}
    if LaneStatus.BLOCKED.value in statuses:
        status = LaneStatus.BLOCKED.value
    elif LaneStatus.WORKING.value in statuses:
        status = LaneStatus.WORKING.value
    elif LaneStatus.WAITING.value in statuses:
        status = LaneStatus.WAITING.value
    elif LaneStatus.IDLE.value in statuses:
        status = LaneStatus.IDLE.value
    elif LaneStatus.FAILED.value in statuses:
        status = LaneStatus.FAILED.value
    elif lanes and statuses <= {LaneStatus.COMPLETED.value}:
        status = LaneStatus.COMPLETED.value
    else:
        status = LaneStatus.PENDING.value
    state["status"] = status
    state["updated_at"] = now
    if status in {LaneStatus.COMPLETED.value, LaneStatus.FAILED.value}:
        state["completed_at"] = max(
            (float(lane.get("completed_at") or now) for lane in lanes),
            default=now,
        )
    else:
        state["completed_at"] = None


def _validate_state(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise MalformedJobStateError(f"{path.name}: root must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise MalformedJobStateError(
            f"{path.name}: unsupported schema_version {data.get('schema_version')!r}"
        )
    if not isinstance(data.get("job_id"), str) or not data["job_id"]:
        raise MalformedJobStateError(f"{path.name}: job_id is missing")
    if not isinstance(data.get("lanes"), dict):
        raise MalformedJobStateError(f"{path.name}: lanes must be an object")
    if not isinstance(data.get("spans"), list):
        raise MalformedJobStateError(f"{path.name}: spans must be an array")

    def valid_timestamp(value: Any) -> bool:
        return value is None or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        )

    for lane_id, lane in data["lanes"].items():
        if not isinstance(lane_id, str) or not isinstance(lane, dict):
            raise MalformedJobStateError(
                f"{path.name}: every lane must be a named object"
            )
        if lane.get("lane_id", lane_id) != lane_id:
            raise MalformedJobStateError(
                f"{path.name}: lane key and lane_id disagree for {lane_id!r}"
            )
        for field in (
            "started_at",
            "updated_at",
            "completed_at",
            "heartbeat_at",
            "last_meaningful_output_at",
            "status_since",
        ):
            if field in lane and not valid_timestamp(lane[field]):
                raise MalformedJobStateError(
                    f"{path.name}: lane {lane_id!r} has invalid {field}"
                )
        for field in ("process", "repository", "checkpoint"):
            if field in lane and not isinstance(lane[field], dict):
                raise MalformedJobStateError(
                    f"{path.name}: lane {lane_id!r} {field} must be an object"
                )
        if lane.get("blocker") is not None and not isinstance(
            lane.get("blocker"), dict
        ):
            raise MalformedJobStateError(
                f"{path.name}: lane {lane_id!r} blocker must be an object or null"
            )
        phases = lane.get("phases", [])
        if not isinstance(phases, list) or any(
            not isinstance(phase, dict)
            or not isinstance(phase.get("phase_id"), str)
            or not phase["phase_id"]
            for phase in phases
        ):
            raise MalformedJobStateError(
                f"{path.name}: lane {lane_id!r} phases are malformed"
            )

    for span in data["spans"]:
        if (
            not isinstance(span, dict)
            or span.get("category") not in TIMING_CATEGORY_ORDER
            or not isinstance(span.get("lane_id"), str)
            or not valid_timestamp(span.get("started_at"))
            or not valid_timestamp(span.get("ended_at"))
        ):
            raise MalformedJobStateError(f"{path.name}: timing span is malformed")
    return data


class JobStateStore:
    """Atomic JSON store for job and lane state."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        repository_probe: Callable[
            [str | Path | None], dict[str, Any]
        ] = capture_repository_identity,
        process_probe: Callable[
            [int | None], dict[str, Any]
        ] = capture_process_identity,
    ):
        self.root = (
            Path(root)
            if root is not None
            else (get_hermes_home() / "jobs" / "diagnostics")
        )
        self.clock = clock
        self.repository_probe = repository_probe
        self.process_probe = process_probe

    def state_path(self, job_id: str) -> Path:
        return self.root / _state_filename(job_id)

    def load(self, job_id: str) -> dict[str, Any]:
        path = self.state_path(job_id)
        if not path.exists():
            raise JobNotFoundError(f"job not found: {job_id}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MalformedJobStateError(f"{path.name}: {exc}") from exc
        return _validate_state(data, path)

    def list_states(self) -> tuple[list[dict[str, Any]], list[StateIssue]]:
        """Read every state file without creating directories or lock files."""

        if not self.root.exists():
            return [], []
        states: list[dict[str, Any]] = []
        issues: list[StateIssue] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                states.append(_validate_state(data, path))
            except json.JSONDecodeError as exc:
                issues.append(StateIssue(str(path), "malformed_json", str(exc)))
            except (OSError, MalformedJobStateError) as exc:
                issues.append(StateIssue(str(path), "invalid_state", str(exc)))
        return states, issues

    def _write(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, path)
            temp_name = None
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass

    def _mutate(
        self,
        job_id: str,
        mutation: Callable[[dict[str, Any]], _T],
        *,
        create_title: str | None = None,
    ) -> _T:
        path = self.state_path(job_id)
        with _exclusive_file_lock(path):
            if path.exists():
                try:
                    state = _validate_state(
                        json.loads(path.read_text(encoding="utf-8")),
                        path,
                    )
                except (OSError, json.JSONDecodeError, MalformedJobStateError) as exc:
                    raise MalformedJobStateError(f"{path.name}: {exc}") from exc
            elif create_title is not None:
                state = _new_job_state(job_id, create_title, self.clock())
            else:
                raise JobNotFoundError(f"job not found: {job_id}")
            result = mutation(state)
            self._write(path, state)
            return result

    def start_lane(
        self,
        job_id: str,
        lane_id: str,
        *,
        job_title: str = "",
        lane_title: str = "",
        provider: str | None = None,
        model: str | None = None,
        worktree: str | Path | None = None,
        process: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        platform: str | None = None,
        command: str | None = None,
        read_only: bool = False,
        depends_on: Sequence[str] = (),
        resources: Sequence[str] = (),
        activate: bool = True,
    ) -> dict[str, Any]:
        now = self.clock()
        proc = dict(process or self.process_probe(None))

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            lanes = state.setdefault("lanes", {})
            lane = lanes.get(lane_id)
            if not isinstance(lane, dict):
                lane = _new_lane(
                    lane_id,
                    now,
                    title=lane_title,
                    provider=provider,
                    model=model,
                    worktree=worktree,
                    process=proc,
                    session_id=session_id,
                    task_id=task_id,
                    platform=platform,
                    command=command,
                    read_only=read_only,
                    depends_on=depends_on,
                    resources=resources,
                    repository_probe=self.repository_probe,
                )
                lanes[lane_id] = lane
            else:
                lane.update({
                    "title": lane_title or lane.get("title") or lane_id,
                    "provider": provider or lane.get("provider") or "",
                    "model": model or lane.get("model") or "",
                    "process": proc,
                    "session_id": session_id or lane.get("session_id") or "",
                    "task_id": task_id or lane.get("task_id") or "",
                    "platform": platform or lane.get("platform") or "",
                    "read_only": bool(read_only),
                })
                if command:
                    lane["command"] = _clean_text(command)
                if depends_on:
                    lane["depends_on"] = list(
                        dict.fromkeys(str(item) for item in depends_on)
                    )
                if resources:
                    lane["resources"] = list(
                        dict.fromkeys(str(item) for item in resources)
                    )
                if worktree:
                    lane["repository"] = self.repository_probe(worktree)
            if activate:
                _transition_lane(state, lane, LaneStatus.WORKING.value, now)
                lane["current_step"] = "starting"
            lane["heartbeat_at"] = now
            if activate:
                lane["blocker"] = None
                lane["next_expected_action"] = ""
            state["title"] = job_title or state.get("title") or job_id
            _recompute_job_status(state, now)
            return json.loads(json.dumps(lane))

        return self._mutate(job_id, mutate, create_title=job_title or job_id)

    def define_phases(
        self,
        job_id: str,
        lane_id: str,
        phases: Sequence[Mapping[str, Any]],
    ) -> None:
        now = self.clock()

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            existing = {
                phase.get("phase_id")
                for phase in lane.get("phases") or []
                if isinstance(phase, dict)
            }
            for spec in phases:
                phase_id = str(spec.get("phase_id") or "").strip()
                if not phase_id or phase_id in existing:
                    continue
                category = spec.get("category")
                if isinstance(category, TimingCategory):
                    category = category.value
                if category is not None and category not in TIMING_CATEGORY_ORDER:
                    raise ValueError(f"unknown timing category: {category}")
                lane.setdefault("phases", []).append({
                    "phase_id": phase_id,
                    "title": _clean_text(spec.get("title") or phase_id),
                    "category": category,
                    "status": LaneStatus.PENDING.value,
                    "attempts": 0,
                    "started_at": None,
                    "completed_at": None,
                    "command": (
                        _clean_text(spec.get("command"))
                        if spec.get("command")
                        else None
                    ),
                    "evidence": [],
                    "result_summary": "",
                })
                existing.add(phase_id)
            lane["updated_at"] = now
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def touch_lane(
        self,
        job_id: str,
        lane_id: str,
        *,
        current_step: str | None = None,
        meaningful_output: str | None = None,
        status: str | LaneStatus | None = None,
        next_expected_action: str | None = None,
    ) -> None:
        now = self.clock()
        normalized_status = status.value if isinstance(status, LaneStatus) else status

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            if normalized_status:
                _transition_lane(state, lane, str(normalized_status), now)
            if current_step:
                lane["current_step"] = _clean_text(current_step, _MAX_STEP_CHARS)
            if meaningful_output:
                lane["last_meaningful_output"] = _clean_text(meaningful_output)
                lane["last_meaningful_output_at"] = now
            if next_expected_action is not None:
                lane["next_expected_action"] = _clean_text(next_expected_action)
            lane["heartbeat_at"] = now
            lane["updated_at"] = now
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def record_span(
        self,
        job_id: str,
        lane_id: str,
        category: str | TimingCategory,
        started_at: float,
        ended_at: float,
        *,
        phase_id: str | None = None,
        label: str = "",
        meaningful_output: str | None = None,
    ) -> None:
        category_value = (
            category.value if isinstance(category, TimingCategory) else str(category)
        )
        now = self.clock()

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            _append_span(
                state,
                category=category_value,
                lane_id=lane_id,
                started_at=started_at,
                ended_at=ended_at,
                phase_id=phase_id,
                label=label,
            )
            lane["heartbeat_at"] = now
            lane["updated_at"] = now
            if meaningful_output:
                lane["last_meaningful_output"] = _clean_text(meaningful_output)
                lane["last_meaningful_output_at"] = now
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def mark_blocked(
        self,
        job_id: str,
        lane_id: str,
        *,
        blocker: str | BlockerKind,
        detail: str,
        next_action: str,
    ) -> None:
        now = self.clock()
        blocker_value = (
            blocker.value if isinstance(blocker, BlockerKind) else str(blocker)
        )

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            _transition_lane(state, lane, LaneStatus.BLOCKED.value, now)
            lane["blocker"] = {
                "kind": blocker_value,
                "detail": _clean_text(detail),
                "detected_at": now,
            }
            lane["next_expected_action"] = _clean_text(next_action)
            lane["heartbeat_at"] = now
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def finish_lane(
        self,
        job_id: str,
        lane_id: str,
        *,
        status: str | LaneStatus = LaneStatus.COMPLETED,
        summary: str = "",
        next_action: str = "",
    ) -> None:
        now = self.clock()
        normalized = status.value if isinstance(status, LaneStatus) else str(status)

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            _transition_lane(state, lane, normalized, now)
            lane["completed_at"] = (
                now
                if normalized
                in {
                    LaneStatus.COMPLETED.value,
                    LaneStatus.FAILED.value,
                }
                else None
            )
            lane["current_step"] = normalized
            lane["heartbeat_at"] = now
            if summary:
                lane["last_meaningful_output"] = _clean_text(summary)
                lane["last_meaningful_output_at"] = now
            lane["next_expected_action"] = _clean_text(next_action)
            lane["repository"] = self.repository_probe(
                (lane.get("repository") or {}).get("worktree")
            )
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def begin_phase(
        self,
        job_id: str,
        lane_id: str,
        phase_id: str,
        *,
        process: Mapping[str, Any] | None = None,
    ) -> bool:
        """Claim an incomplete phase after validating the safe checkpoint."""

        now = self.clock()
        current_process = dict(process or self.process_probe(None))

        def mutate(state: dict[str, Any]) -> bool:
            lane = _required_lane(state, lane_id)
            phase = _phase_by_id(lane, phase_id)
            if phase is None:
                raise JobDiagnosticsError(f"phase not defined: {phase_id}")
            if phase.get("status") == LaneStatus.COMPLETED.value:
                return False

            checkpoint = lane.get("checkpoint") or {}
            expected_repository = checkpoint.get("repository")
            worktree = (expected_repository or {}).get("worktree")
            current_repository = self.repository_probe(worktree)
            repo_reasons = repository_drift_reasons(
                expected_repository,
                current_repository,
            )
            if repo_reasons:
                raise RepositoryDriftError(
                    repo_reasons,
                    BlockerKind.WRONG_WORKTREE_OR_BRANCH,
                )
            evidence_reasons = evidence_drift_reasons(checkpoint.get("evidence") or [])
            if evidence_reasons:
                raise RepositoryDriftError(
                    evidence_reasons,
                    BlockerKind.HASH_MISMATCH,
                )

            if phase.get("status") == LaneStatus.WORKING.value:
                owner_status = process_identity_status(phase.get("process"))
                if owner_status == "alive":
                    raise DuplicatePhaseError(
                        f"phase {phase_id} is already running in pid "
                        f"{(phase.get('process') or {}).get('pid')}"
                    )

            phase["status"] = LaneStatus.WORKING.value
            phase["started_at"] = now
            phase["completed_at"] = None
            phase["attempts"] = int(phase.get("attempts") or 0) + 1
            phase["process"] = current_process
            phase["result_summary"] = ""
            lane["retry_count"] = sum(
                max(0, int(item.get("attempts") or 0) - 1)
                for item in lane.get("phases") or []
                if isinstance(item, dict)
            )
            lane["process"] = current_process
            lane["command"] = phase.get("command") or lane.get("command")
            lane["current_step"] = phase.get("title") or phase_id
            lane["blocker"] = None
            lane["next_expected_action"] = ""
            lane["repository"] = current_repository
            _transition_lane(state, lane, LaneStatus.WORKING.value, now)
            _recompute_job_status(state, now)
            return True

        return self._mutate(job_id, mutate)

    def complete_phase(
        self,
        job_id: str,
        lane_id: str,
        phase_id: str,
        *,
        summary: str = "",
        evidence_paths: Sequence[str | Path] = (),
    ) -> None:
        now = self.clock()
        evidence = [_evidence_identity(path) for path in evidence_paths]

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            phase = _required_phase(lane, phase_id)
            started = float(phase.get("started_at") or now)
            phase["status"] = LaneStatus.COMPLETED.value
            phase["completed_at"] = now
            phase["result_summary"] = _clean_text(summary)
            phase["evidence"] = evidence
            category = phase.get("category")
            if category:
                _append_span(
                    state,
                    category=str(category),
                    lane_id=lane_id,
                    phase_id=phase_id,
                    started_at=started,
                    ended_at=now,
                    label=str(phase.get("title") or phase_id),
                )
            repository = self.repository_probe(
                (lane.get("repository") or {}).get("worktree")
            )
            lane["repository"] = repository
            lane["checkpoint"] = {
                "repository": repository,
                "evidence": evidence,
                "phase_id": phase_id,
                "created_at": now,
            }
            lane["last_meaningful_output"] = _clean_text(
                summary or f"completed phase {phase_id}"
            )
            lane["last_meaningful_output_at"] = now
            lane["heartbeat_at"] = now
            remaining = [
                item
                for item in lane.get("phases") or []
                if isinstance(item, dict)
                and item.get("status") != LaneStatus.COMPLETED.value
            ]
            if remaining:
                _transition_lane(state, lane, LaneStatus.PENDING.value, now)
                lane["current_step"] = str(
                    remaining[0].get("title") or remaining[0].get("phase_id")
                )
                lane["next_expected_action"] = (
                    f"run phase {remaining[0].get('phase_id')}"
                )
            else:
                _transition_lane(state, lane, LaneStatus.COMPLETED.value, now)
                lane["current_step"] = "completed"
                lane["completed_at"] = now
                lane["next_expected_action"] = ""
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def fail_phase(
        self,
        job_id: str,
        lane_id: str,
        phase_id: str,
        *,
        detail: str,
        blocker: str | BlockerKind | None = None,
        next_action: str = "Inspect the failure and retry from this checkpoint.",
    ) -> None:
        now = self.clock()

        def mutate(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            phase = _required_phase(lane, phase_id)
            started = float(phase.get("started_at") or now)
            category = phase.get("category")
            if category:
                _append_span(
                    state,
                    category=str(category),
                    lane_id=lane_id,
                    phase_id=phase_id,
                    started_at=started,
                    ended_at=now,
                    label=str(phase.get("title") or phase_id),
                )
            blocker_value = (
                blocker.value
                if isinstance(blocker, BlockerKind)
                else str(
                    blocker
                    or classify_blocker(
                        detail,
                        test_command=category == TimingCategory.TEST.value,
                    ).value
                )
            )
            phase["status"] = LaneStatus.FAILED.value
            phase["completed_at"] = now
            phase["result_summary"] = _clean_text(detail)
            _transition_lane(state, lane, LaneStatus.BLOCKED.value, now)
            lane["blocker"] = {
                "kind": blocker_value,
                "detail": _clean_text(detail),
                "detected_at": now,
            }
            lane["next_expected_action"] = _clean_text(next_action)
            lane["heartbeat_at"] = now
            lane["retry_count"] = sum(
                max(0, int(item.get("attempts") or 0) - 1)
                for item in lane.get("phases") or []
                if isinstance(item, dict)
            )
            _recompute_job_status(state, now)

        self._mutate(job_id, mutate)

    def resume_plan(self, job_id: str, lane_id: str | None = None) -> ResumePlan:
        """Return a read-only resume decision; never claim or execute work."""

        state = self.load(job_id)
        lanes = state.get("lanes") or {}
        if lane_id is None:
            candidates = [
                key
                for key, lane in lanes.items()
                if isinstance(lane, dict)
                and lane.get("status") != LaneStatus.COMPLETED.value
            ]
            if not candidates:
                candidates = list(lanes)
            lane_id = candidates[0] if candidates else ""
        lane = lanes.get(lane_id)
        if not isinstance(lane, dict):
            raise JobDiagnosticsError(f"lane not found: {lane_id}")

        checkpoint = lane.get("checkpoint") or {}
        expected_repository = checkpoint.get("repository")
        current_repository = self.repository_probe(
            (expected_repository or {}).get("worktree")
        )
        repo_reasons = repository_drift_reasons(
            expected_repository,
            current_repository,
        )
        evidence_reasons = evidence_drift_reasons(checkpoint.get("evidence") or [])
        phases = [
            phase
            for phase in lane.get("phases") or []
            if isinstance(phase, dict)
            and phase.get("status") != LaneStatus.COMPLETED.value
        ]
        phase = phases[0] if phases else None
        phase_id = str(phase.get("phase_id")) if phase else None
        command = str(phase.get("command")) if phase and phase.get("command") else None
        evidence_paths = tuple(
            str(record.get("path"))
            for record in checkpoint.get("evidence") or []
            if isinstance(record, dict) and record.get("path")
        )

        if evidence_reasons:
            return ResumePlan(
                False,
                job_id,
                lane_id,
                phase_id,
                command,
                (expected_repository or {}).get("worktree"),
                evidence_paths,
                BlockerKind.HASH_MISMATCH.value,
                tuple(evidence_reasons),
                "Restore or independently verify the checkpoint evidence.",
            )
        if repo_reasons:
            return ResumePlan(
                False,
                job_id,
                lane_id,
                phase_id,
                command,
                (expected_repository or {}).get("worktree"),
                evidence_paths,
                BlockerKind.WRONG_WORKTREE_OR_BRANCH.value,
                tuple(repo_reasons),
                "Return to the exact checkpoint repository state before resuming.",
            )
        if phase is None:
            return ResumePlan(
                True,
                job_id,
                lane_id,
                None,
                None,
                (expected_repository or {}).get("worktree"),
                evidence_paths,
                None,
                ("all recorded phases are complete",),
                "No action is required.",
            )
        if phase.get("status") == LaneStatus.WORKING.value:
            owner_status = process_identity_status(phase.get("process"))
            if owner_status == "alive":
                return ResumePlan(
                    False,
                    job_id,
                    lane_id,
                    phase_id,
                    command,
                    (expected_repository or {}).get("worktree"),
                    evidence_paths,
                    BlockerKind.EXTERNAL_PROCESS_CONFLICT.value,
                    ("the phase owner process is still alive",),
                    "Wait for the existing process or inspect it; do not duplicate the phase.",
                )
        if not command:
            return ResumePlan(
                False,
                job_id,
                lane_id,
                phase_id,
                None,
                (expected_repository or {}).get("worktree"),
                evidence_paths,
                BlockerKind.OPERATOR_PRESENCE_REQUIREMENT.value,
                ("the next phase has no recorded resume command",),
                "Review the checkpoint and supply an explicit phase command.",
            )
        return ResumePlan(
            True,
            job_id,
            lane_id,
            phase_id,
            command,
            (expected_repository or {}).get("worktree"),
            evidence_paths,
            None,
            ("repository and evidence match the last safe checkpoint",),
            "Run the recorded command through the job runner; this report does not launch it.",
        )


def _required_lane(state: Mapping[str, Any], lane_id: str) -> dict[str, Any]:
    lane = (state.get("lanes") or {}).get(lane_id)
    if not isinstance(lane, dict):
        raise JobDiagnosticsError(f"lane not found: {lane_id}")
    return lane


def _required_phase(lane: Mapping[str, Any], phase_id: str) -> dict[str, Any]:
    phase = _phase_by_id(lane, phase_id)
    if phase is None:
        raise JobDiagnosticsError(f"phase not found: {phase_id}")
    return phase


class JobRun:
    """Idempotent phase runner backed by :class:`JobStateStore`."""

    def __init__(self, store: JobStateStore, job_id: str, lane_id: str):
        self.store = store
        self.job_id = job_id
        self.lane_id = lane_id

    @classmethod
    def start(
        cls,
        store: JobStateStore,
        *,
        job_id: str,
        lane_id: str,
        title: str,
        worktree: str | Path,
        provider: str = "",
        model: str = "",
        command: str | None = None,
        read_only: bool = False,
        depends_on: Sequence[str] = (),
        resources: Sequence[str] = (),
    ) -> "JobRun":
        store.start_lane(
            job_id,
            lane_id,
            job_title=title,
            lane_title=lane_id,
            provider=provider,
            model=model,
            worktree=worktree,
            command=command,
            read_only=read_only,
            depends_on=depends_on,
            resources=resources,
            activate=False,
        )
        return cls(store, job_id, lane_id)

    def define_phases(self, phases: Sequence[Mapping[str, Any]]) -> "JobRun":
        self.store.define_phases(self.job_id, self.lane_id, phases)
        return self

    def run_phase(
        self,
        phase_id: str,
        action: Callable[[], _T],
        *,
        evidence_paths: Sequence[str | Path] = (),
        summary: str | Callable[[_T], str] = "",
    ) -> PhaseExecution:
        should_run = self.store.begin_phase(self.job_id, self.lane_id, phase_id)
        if not should_run:
            return PhaseExecution(False, reason="phase already completed")
        try:
            value = action()
        except Exception as exc:
            self.store.fail_phase(
                self.job_id,
                self.lane_id,
                phase_id,
                detail=f"{type(exc).__name__}: {exc}",
            )
            raise
        rendered_summary = summary if isinstance(summary, str) else summary(value)
        self.store.complete_phase(
            self.job_id,
            self.lane_id,
            phase_id,
            summary=rendered_summary or f"completed {phase_id}",
            evidence_paths=evidence_paths,
        )
        return PhaseExecution(True, value=value)


def _merge_intervals(
    intervals: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    normalized = sorted(
        (float(start), max(float(start), float(end)))
        for start, end in intervals
        if end is not None
    )
    merged: list[tuple[float, float]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _interval_duration(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge_intervals(intervals))


def timing_breakdown(
    state: Mapping[str, Any],
    *,
    now: float | None = None,
    lane_id: str | None = None,
) -> dict[str, float]:
    """Return wall-clock-deduplicated category timing.

    Overlapping spans in concurrent lanes are unioned, so one second of two
    simultaneous test lanes counts as one job-wall second, not two.  The
    ``parallel_overlap`` field reports the saved lane-seconds separately.
    """

    current = float(now if now is not None else time.time())
    spans = [
        span
        for span in state.get("spans") or []
        if isinstance(span, dict)
        and (lane_id is None or span.get("lane_id") == lane_id)
    ]
    open_idle_spans: list[dict[str, Any]] = []
    for candidate_id, lane in (state.get("lanes") or {}).items():
        if not isinstance(lane, dict) or (
            lane_id is not None and candidate_id != lane_id
        ):
            continue
        if lane.get("status") in _IDLE_LIKE_STATUSES:
            open_idle_spans.append({
                "category": TimingCategory.BLOCKED_IDLE.value,
                "lane_id": candidate_id,
                "started_at": float(lane.get("status_since") or current),
                "ended_at": current,
            })
    spans.extend(open_idle_spans)

    result: dict[str, float] = {}
    for category in TIMING_CATEGORY_ORDER:
        result[category] = _interval_duration(
            (
                float(span.get("started_at") or 0),
                float(span.get("ended_at") or current),
            )
            for span in spans
            if span.get("category") == category
        )

    starts: list[float] = []
    ends: list[float] = []
    lanes = state.get("lanes") or {}
    for candidate_id, lane in lanes.items():
        if not isinstance(lane, dict) or (
            lane_id is not None and candidate_id != lane_id
        ):
            continue
        starts.append(
            float(lane.get("started_at") or state.get("started_at") or current)
        )
        terminal = lane.get("status") in {
            LaneStatus.COMPLETED.value,
            LaneStatus.FAILED.value,
        }
        ends.append(
            float(lane.get("completed_at") or lane.get("updated_at") or current)
            if terminal
            else current
        )
    result["total_elapsed"] = max(
        0.0, max(ends, default=current) - min(starts, default=current)
    )

    all_intervals = [
        (
            float(span.get("started_at") or 0),
            float(span.get("ended_at") or current),
        )
        for span in spans
    ]
    result["busy_wall"] = _interval_duration(all_intervals)
    by_lane: dict[str, list[tuple[float, float]]] = {}
    for span in spans:
        by_lane.setdefault(str(span.get("lane_id") or ""), []).append((
            float(span.get("started_at") or 0),
            float(span.get("ended_at") or current),
        ))
    lane_seconds = sum(_interval_duration(items) for items in by_lane.values())
    result["parallel_overlap"] = max(0.0, lane_seconds - result["busy_wall"])
    return {key: round(value, 6) for key, value in result.items()}


def effective_lane_status(
    lane: Mapping[str, Any],
    *,
    now: float | None = None,
    idle_after: float = DEFAULT_IDLE_AFTER_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
    process_status: Callable[[Mapping[str, Any] | None], str] = process_identity_status,
) -> str:
    current = float(now if now is not None else time.time())
    persisted = str(lane.get("status") or LaneStatus.PENDING.value)
    if persisted in {
        LaneStatus.COMPLETED.value,
        LaneStatus.FAILED.value,
        LaneStatus.BLOCKED.value,
        LaneStatus.PENDING.value,
    }:
        return persisted
    proc_state = process_status(lane.get("process"))
    if proc_state in {"dead", "reused"}:
        return LaneStatus.DEAD.value
    heartbeat_age = current - float(
        lane.get("heartbeat_at") or lane.get("updated_at") or current
    )
    if heartbeat_age >= stale_after:
        return LaneStatus.STALE.value
    if persisted == LaneStatus.WAITING.value:
        return LaneStatus.WAITING.value
    output_age = current - float(
        lane.get("last_meaningful_output_at") or lane.get("started_at") or current
    )
    if output_age >= idle_after:
        return LaneStatus.IDLE.value
    return LaneStatus.WORKING.value


class HeartbeatReporter:
    """Render state-aware heartbeats and suppress unchanged noise."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        meaningful_output_warning_seconds: float = DEFAULT_MEANINGFUL_OUTPUT_WARNING_SECONDS,
        idle_after_seconds: float = DEFAULT_IDLE_AFTER_SECONDS,
        repeat_after_seconds: float | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.meaningful_output_warning_seconds = max(
            self.interval_seconds,
            float(meaningful_output_warning_seconds),
        )
        self.idle_after_seconds = max(self.interval_seconds, float(idle_after_seconds))
        self.repeat_after_seconds = max(
            self.interval_seconds,
            float(
                repeat_after_seconds
                if repeat_after_seconds is not None
                else max(DEFAULT_HEARTBEAT_REPEAT_SECONDS, self.interval_seconds * 3)
            ),
        )
        self.clock = clock
        self._last_signature: tuple[Any, ...] | None = None
        self._last_emit_at = 0.0

    def evaluate(
        self,
        *,
        started_at: float,
        current_step: str = "",
        last_activity_at: float | None = None,
        last_meaningful_output_at: float | None = None,
        persisted_status: str = LaneStatus.WORKING.value,
        blocker: Mapping[str, Any] | None = None,
        process_alive: bool | None = True,
        now: float | None = None,
    ) -> HeartbeatUpdate | None:
        current = float(now if now is not None else self.clock())
        activity_at = float(last_activity_at or started_at)
        meaningful_at = float(last_meaningful_output_at or started_at)
        idle_for = max(0.0, current - activity_at)
        output_idle_for = max(0.0, current - meaningful_at)
        step = _clean_text(current_step, 100)

        if process_alive is False:
            status = LaneStatus.DEAD.value
        elif blocker or persisted_status == LaneStatus.BLOCKED.value:
            status = LaneStatus.BLOCKED.value
        elif persisted_status == LaneStatus.WAITING.value or re.search(
            r"\b(?:waiting|backoff|rate limit|provider response|user input)\b",
            step,
            re.IGNORECASE,
        ):
            status = LaneStatus.WAITING.value
        elif idle_for >= self.idle_after_seconds:
            status = LaneStatus.IDLE.value
        else:
            status = LaneStatus.WORKING.value

        output_stale = output_idle_for >= self.meaningful_output_warning_seconds
        blocker_kind = str((blocker or {}).get("kind") or "")
        signature = (status, step, blocker_kind, output_stale)
        if (
            signature == self._last_signature
            and current - self._last_emit_at < self.repeat_after_seconds
        ):
            return None

        elapsed = _format_duration(current - started_at)
        label = {
            LaneStatus.WORKING.value: "Working",
            LaneStatus.WAITING.value: "Waiting",
            LaneStatus.BLOCKED.value: "Blocked",
            LaneStatus.IDLE.value: "Idle",
            LaneStatus.DEAD.value: "Dead",
        }[status]
        glyph = {
            LaneStatus.WORKING.value: "⏳",
            LaneStatus.WAITING.value: "⌛",
            LaneStatus.BLOCKED.value: "⛔",
            LaneStatus.IDLE.value: "⚠️",
            LaneStatus.DEAD.value: "💀",
        }[status]
        parts = [f"{glyph} {label} — {elapsed}"]
        if step:
            parts.append(step)
        if blocker_kind:
            parts.append(blocker_kind.replace("_", " "))
        if output_stale:
            parts.append(
                f"no meaningful output for {_format_duration(output_idle_for)}"
            )
        update = HeartbeatUpdate(status, " — ".join(parts), signature)
        self._last_signature = signature
        self._last_emit_at = current
        return update


class AgentJobTracker:
    """Fail-open adapter used by live ``AIAgent`` turns."""

    def __init__(
        self,
        store: JobStateStore,
        *,
        job_id: str,
        lane_id: str,
        phase_id: str,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.job_id = job_id
        self.lane_id = lane_id
        self.phase_id = phase_id
        self.clock = clock
        self._last_touch_at = 0.0
        self._last_step = ""
        self._last_failure = ""
        self._last_test_command = False
        self._lock = threading.Lock()

    def activity(self, description: str, *, meaningful: bool | None = None) -> None:
        now = self.clock()
        step = _clean_text(description, _MAX_STEP_CHARS)
        if meaningful is None:
            meaningful = bool(
                re.search(r"\b(?:completed|receiving stream|response received)\b", step)
            )
        with self._lock:
            if step == self._last_step and now - self._last_touch_at < 5.0:
                return
            self._last_step = step
            self._last_touch_at = now
        self.store.touch_lane(
            self.job_id,
            self.lane_id,
            current_step=step,
            meaningful_output=step if meaningful else None,
        )

    def record_span(
        self,
        category: str | TimingCategory,
        started_at: float,
        ended_at: float,
        *,
        label: str = "",
        meaningful_output: str | None = None,
    ) -> None:
        self.store.record_span(
            self.job_id,
            self.lane_id,
            category,
            started_at,
            ended_at,
            phase_id=self.phase_id,
            label=label,
            meaningful_output=meaningful_output,
        )

    def record_tool(
        self,
        tool_name: str,
        args: Mapping[str, Any] | None,
        started_at: float,
        ended_at: float,
        *,
        failed: bool = False,
        detail: str = "",
    ) -> None:
        category = classify_tool_timing(tool_name, args)
        command = str((args or {}).get("command") or "")
        self.record_span(
            category,
            started_at,
            ended_at,
            label=tool_name,
            meaningful_output=f"tool completed: {tool_name}",
        )
        if command:
            self.store.touch_lane(
                self.job_id,
                self.lane_id,
                current_step=f"{tool_name}: {_clean_text(command, 160)}",
            )
        if failed:
            self._last_failure = detail or f"{tool_name} failed"
            self._last_test_command = category == TimingCategory.TEST.value

    def finish(
        self,
        *,
        failed: bool,
        interrupted: bool,
        summary: str,
        exit_reason: str,
    ) -> None:
        if failed:
            detail = self._last_failure or exit_reason or summary or "agent turn failed"
            blocker = classify_blocker(detail, test_command=self._last_test_command)
            self.store.fail_phase(
                self.job_id,
                self.lane_id,
                self.phase_id,
                detail=detail,
                blocker=blocker,
                next_action="Inspect the slow-job report and resume only from the recorded checkpoint.",
            )
            return
        if interrupted:
            self.store.finish_lane(
                self.job_id,
                self.lane_id,
                status=LaneStatus.IDLE,
                summary=summary or "agent turn interrupted",
                next_action="Resume the session when the operator is ready.",
            )
            return
        self.store.complete_phase(
            self.job_id,
            self.lane_id,
            self.phase_id,
            summary=summary or "agent turn completed",
        )

    def lane_snapshot(self) -> dict[str, Any]:
        state = self.store.load(self.job_id)
        return dict(_required_lane(state, self.lane_id))


def _job_diagnostics_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        raw = load_config().get("job_diagnostics") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _configured_thresholds() -> dict[str, float]:
    cfg = _job_diagnostics_config()

    def positive_number(key: str, default: float) -> float:
        try:
            return max(1.0, float(cfg.get(key, default)))
        except (TypeError, ValueError):
            return default

    return {
        "idle_after": positive_number("idle_after_seconds", DEFAULT_IDLE_AFTER_SECONDS),
        "stale_after": positive_number(
            "stale_after_seconds", DEFAULT_STALE_AFTER_SECONDS
        ),
        "meaningful_output_warning": positive_number(
            "meaningful_output_warning_seconds",
            DEFAULT_MEANINGFUL_OUTPUT_WARNING_SECONDS,
        ),
    }


def start_agent_job_tracker(
    agent: Any,
    *,
    effective_task_id: str,
    turn_id: str,
) -> AgentJobTracker | None:
    """Attach local diagnostics to an agent turn without risking the turn."""

    cfg = _job_diagnostics_config()
    if cfg.get("enabled", True) is False:
        return None
    try:
        session_id = str(getattr(agent, "session_id", "") or turn_id)
        job_id = f"session:{session_id}"
        lane_id = f"task:{effective_task_id}"
        phase_id = f"turn:{turn_id}"
        worktree = os.getenv("TERMINAL_CWD") or os.getcwd()
        store = JobStateStore()
        tracker = AgentJobTracker(
            store,
            job_id=job_id,
            lane_id=lane_id,
            phase_id=phase_id,
        )
        origin = str(getattr(agent, "_memory_write_origin", "") or "")
        phase_category = (
            TimingCategory.REVIEW.value if origin == "background_review" else None
        )
        store.start_lane(
            job_id,
            lane_id,
            job_title=f"Hermes {getattr(agent, 'platform', None) or 'cli'} session",
            lane_title="background review" if phase_category else "agent turn",
            provider=str(getattr(agent, "provider", "") or ""),
            model=str(getattr(agent, "model", "") or ""),
            worktree=worktree,
            session_id=session_id,
            task_id=effective_task_id,
            platform=str(getattr(agent, "platform", "") or ""),
            command=None,
            read_only=bool(phase_category),
            resources=(f"session:{session_id}",),
        )
        store.define_phases(
            job_id,
            lane_id,
            (
                {
                    "phase_id": phase_id,
                    "title": "background review" if phase_category else "agent turn",
                    "category": phase_category,
                    "command": None,
                },
            ),
        )
        # Automatic observation must never block normal Hermes work merely
        # because a previous turn changed the repository.  The explicit
        # JobRun resume path is the fail-closed execution boundary.
        now = store.clock()

        def claim_without_resume_check(state: dict[str, Any]) -> None:
            lane = _required_lane(state, lane_id)
            phase = _required_phase(lane, phase_id)
            phase["status"] = LaneStatus.WORKING.value
            phase["started_at"] = now
            phase["attempts"] = int(phase.get("attempts") or 0) + 1
            phase["process"] = capture_process_identity()
            lane["process"] = phase["process"]
            lane["current_step"] = phase["title"]
            lane["blocker"] = None
            _transition_lane(state, lane, LaneStatus.WORKING.value, now)
            _recompute_job_status(state, now)

        store._mutate(job_id, claim_without_resume_check)
        return tracker
    except Exception:
        return None


def _state_with_metrics(
    state: Mapping[str, Any],
    *,
    now: float,
    idle_after: float,
    stale_after: float,
) -> dict[str, Any]:
    copy = json.loads(json.dumps(state))
    copy["timing"] = timing_breakdown(copy, now=now)
    for lane_id, lane in copy.get("lanes", {}).items():
        lane["effective_status"] = effective_lane_status(
            lane,
            now=now,
            idle_after=idle_after,
            stale_after=stale_after,
        )
        lane["timing"] = timing_breakdown(copy, now=now, lane_id=lane_id)
        lane["elapsed_seconds"] = lane["timing"]["total_elapsed"]
        lane["last_output_age_seconds"] = max(
            0.0,
            now
            - float(
                lane.get("last_meaningful_output_at") or lane.get("started_at") or now
            ),
        )
    return copy


def diagnostics_snapshot(
    store: JobStateStore | None = None,
    *,
    now: float | None = None,
    idle_after: float = DEFAULT_IDLE_AFTER_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    selected_store = store or JobStateStore()
    current = float(now if now is not None else selected_store.clock())
    states, issues = selected_store.list_states()
    jobs = [
        _state_with_metrics(
            state,
            now=current,
            idle_after=idle_after,
            stale_after=stale_after,
        )
        for state in states
    ]
    return {
        "generated_at": current,
        "generated_at_iso": _now_iso(current),
        "jobs": jobs,
        "issues": [issue.__dict__ for issue in issues],
    }


def _all_lanes(
    snapshot: Mapping[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for job in snapshot.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        for lane in (job.get("lanes") or {}).values():
            if isinstance(lane, dict):
                rows.append((job, lane))
    return rows


def render_dashboard(
    store: JobStateStore | None = None,
    *,
    now: float | None = None,
    idle_after: float = DEFAULT_IDLE_AFTER_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    snapshot = diagnostics_snapshot(
        store,
        now=now,
        idle_after=idle_after,
        stale_after=stale_after,
    )
    rows = _all_lanes(snapshot)
    active_statuses = {
        LaneStatus.WORKING.value,
        LaneStatus.WAITING.value,
        LaneStatus.IDLE.value,
        LaneStatus.STALE.value,
        LaneStatus.DEAD.value,
    }
    active = [
        (job, lane) for job, lane in rows if lane["effective_status"] in active_statuses
    ]
    blocked = [
        (job, lane)
        for job, lane in rows
        if lane["effective_status"] == LaneStatus.BLOCKED.value
    ]
    idle = [
        (job, lane)
        for job, lane in rows
        if lane["effective_status"]
        in {
            LaneStatus.IDLE.value,
            LaneStatus.STALE.value,
            LaneStatus.DEAD.value,
        }
    ]
    longest = sorted(
        [
            (job, lane)
            for job, lane in rows
            if lane["effective_status"] != LaneStatus.COMPLETED.value
        ],
        key=lambda item: float(item[1].get("elapsed_seconds") or 0),
        reverse=True,
    )[:5]

    lines = [
        "Hermes job diagnostics (read-only)",
        (
            f"Active: {len(active)}  Blocked: {len(blocked)}  "
            f"Idle/stale/dead: {len(idle)}  State warnings: {len(snapshot['issues'])}"
        ),
    ]

    def append_rows(
        heading: str,
        selected: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    ) -> None:
        lines.append(f"\n{heading}:")
        if not selected:
            lines.append("  none")
            return
        for job, lane in selected:
            step = lane.get("current_step") or "unknown step"
            blocker = lane.get("blocker") or {}
            suffix = f" · {blocker.get('kind')}" if blocker.get("kind") else ""
            lines.append(
                f"  {job['job_id']} / {lane['lane_id']} · "
                f"{lane['effective_status']} · "
                f"{_format_duration(lane.get('elapsed_seconds') or 0)} · "
                f"{_clean_text(step, 90)}{suffix}"
            )

    append_rows("Active jobs", active)
    append_rows("Blocked jobs", blocked)
    append_rows("Longest-running jobs", longest)
    append_rows("Idle jobs", idle)

    providers: dict[str, dict[str, float]] = {}
    for _job, lane in active:
        provider = str(lane.get("provider") or "unknown")
        entry = providers.setdefault(provider, {"lanes": 0.0, "model_wait": 0.0})
        entry["lanes"] += 1
        entry["model_wait"] += float((lane.get("timing") or {}).get("model_wait") or 0)
    lines.append("\nProvider utilization:")
    if not providers:
        lines.append("  none")
    else:
        for provider, data in sorted(providers.items()):
            lines.append(
                f"  {provider}: {int(data['lanes'])} active lane(s), "
                f"{_format_duration(data['model_wait'])} model wait"
            )

    worktrees: dict[str, list[str]] = {}
    for job, lane in active + blocked:
        worktree = str((lane.get("repository") or {}).get("worktree") or "unknown")
        worktrees.setdefault(worktree, []).append(f"{job['job_id']}/{lane['lane_id']}")
    lines.append("\nCurrent worktrees:")
    if not worktrees:
        lines.append("  none")
    else:
        for worktree, owners in sorted(worktrees.items()):
            lines.append(f"  {worktree}: {', '.join(owners)}")

    decisions: list[str] = []
    for job, lane in blocked + idle:
        next_action = lane.get("next_expected_action") or (
            "inspect the lane before retrying"
        )
        decisions.append(
            f"{job['job_id']}/{lane['lane_id']}: {_clean_text(next_action, 140)}"
        )
    lines.append("\nNext operator decisions:")
    if decisions:
        lines.extend(f"  {decision}" for decision in decisions)
    else:
        lines.append("  none")

    if snapshot["issues"]:
        lines.append("\nUnreadable state files:")
        for issue in snapshot["issues"]:
            lines.append(
                f"  {Path(issue['path']).name}: {issue['kind']} ({_clean_text(issue['detail'], 120)})"
            )
    return "\n".join(lines)


def _select_lane(
    state: Mapping[str, Any],
    lane_id: str | None,
    *,
    now: float,
) -> tuple[str, dict[str, Any]]:
    lanes = state.get("lanes") or {}
    if lane_id:
        lane = lanes.get(lane_id)
        if not isinstance(lane, dict):
            raise JobDiagnosticsError(f"lane not found: {lane_id}")
        return lane_id, lane
    candidates = [
        (key, lane)
        for key, lane in lanes.items()
        if isinstance(lane, dict)
        and effective_lane_status(lane, now=now) != LaneStatus.COMPLETED.value
    ]
    if not candidates:
        candidates = [
            (key, lane) for key, lane in lanes.items() if isinstance(lane, dict)
        ]
    if not candidates:
        raise JobDiagnosticsError("job has no lanes")
    return max(
        candidates,
        key=lambda item: float(item[1].get("updated_at") or 0),
    )


def why_slow_report(
    job_id: str,
    *,
    lane_id: str | None = None,
    store: JobStateStore | None = None,
    now: float | None = None,
    idle_after: float = DEFAULT_IDLE_AFTER_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
    meaningful_output_warning: float = DEFAULT_MEANINGFUL_OUTPUT_WARNING_SECONDS,
) -> str:
    selected_store = store or JobStateStore()
    current = float(now if now is not None else selected_store.clock())
    state = selected_store.load(job_id)
    selected_lane_id, lane = _select_lane(state, lane_id, now=current)
    status = effective_lane_status(
        lane,
        now=current,
        idle_after=idle_after,
        stale_after=stale_after,
    )
    timings = timing_breakdown(state, now=current, lane_id=selected_lane_id)
    elapsed = max(timings["total_elapsed"], 0.000001)
    ranked = sorted(
        ((key, timings[key]) for key in TIMING_CATEGORY_ORDER),
        key=lambda item: item[1],
        reverse=True,
    )
    output_age = max(
        0.0,
        current
        - float(
            lane.get("last_meaningful_output_at") or lane.get("started_at") or current
        ),
    )
    blocker = lane.get("blocker") or {}
    causes: list[str] = []
    if status in {LaneStatus.DEAD.value, LaneStatus.STALE.value}:
        causes.append(
            f"the lane is {status}; its process/heartbeat is not currently trustworthy"
        )
    if blocker.get("kind"):
        causes.append(
            f"it is blocked by {blocker['kind']}: {blocker.get('detail') or 'no detail'}"
        )
    for category, seconds in ranked:
        if seconds <= 0:
            continue
        pct = seconds / elapsed * 100
        if pct >= 20:
            causes.append(
                f"{category.replace('_', ' ')} accounts for "
                f"{_format_duration(seconds)} ({pct:.0f}% of elapsed)"
            )
        if len(causes) >= 3:
            break
    if int(lane.get("retry_count") or 0):
        causes.append(f"{lane['retry_count']} retry attempt(s) added repeated work")
    if output_age >= meaningful_output_warning:
        causes.append(
            f"no meaningful output has been recorded for {_format_duration(output_age)}"
        )
    if not causes:
        causes.append("no single dominant delay is visible in the recorded spans")

    lines = [
        f"Why is {job_id} slow?",
        f"Lane: {selected_lane_id} · Status: {status} · Elapsed: {_format_duration(elapsed)}",
        f"Current step: {lane.get('current_step') or 'unknown'}",
        f"Last meaningful output: {lane.get('last_meaningful_output') or 'none recorded'} "
        f"({_format_duration(output_age)} ago)",
        f"Retries: {int(lane.get('retry_count') or 0)}",
        "\nTiming:",
    ]
    for category in TIMING_CATEGORY_ORDER:
        seconds = timings[category]
        lines.append(
            f"  {category}: {_format_duration(seconds)} ({seconds / elapsed * 100:.0f}%)"
        )
    lines.append(f"  parallel_overlap: {_format_duration(timings['parallel_overlap'])}")
    lines.append("\nLikely causes:")
    lines.extend(f"  - {cause}" for cause in causes)
    lines.append(
        "\nNext action: "
        + (
            str(lane.get("next_expected_action"))
            if lane.get("next_expected_action")
            else "inspect the dominant timing bucket and the lane's latest output"
        )
    )
    return "\n".join(lines)


def _dependency_satisfied(
    dependency: str,
    states: Mapping[str, tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    local_job_id: str,
) -> bool:
    key = dependency if "/" in dependency else f"{local_job_id}/{dependency}"
    item = states.get(key)
    return bool(item and item[1].get("status") == LaneStatus.COMPLETED.value)


def parallel_recommendation(
    *,
    job_id: str | None = None,
    store: JobStateStore | None = None,
    now: float | None = None,
    idle_after: float = DEFAULT_IDLE_AFTER_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    """Recommend compatible lanes without claiming or launching any work."""

    selected_store = store or JobStateStore()
    current = float(now if now is not None else selected_store.clock())
    states, issues = selected_store.list_states()
    index: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for state in states:
        for lane_id, lane in (state.get("lanes") or {}).items():
            if isinstance(lane, dict):
                index[f"{state['job_id']}/{lane_id}"] = (state, lane)

    active: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    ready: list[tuple[str, Mapping[str, Any], Mapping[str, Any]]] = []
    refused: list[str] = []
    for key, (state, lane) in index.items():
        if job_id and state["job_id"] != job_id:
            continue
        status = effective_lane_status(
            lane,
            now=current,
            idle_after=idle_after,
            stale_after=stale_after,
        )
        if status in {LaneStatus.WORKING.value, LaneStatus.WAITING.value}:
            active.append((key, state, lane))
            continue
        if status not in {
            LaneStatus.PENDING.value,
            LaneStatus.IDLE.value,
            LaneStatus.DEAD.value,
        }:
            continue
        dependencies = [str(item) for item in lane.get("depends_on") or []]
        missing = [
            dep
            for dep in dependencies
            if not _dependency_satisfied(dep, index, local_job_id=str(state["job_id"]))
        ]
        if missing:
            refused.append(f"{key}: waiting on {', '.join(missing)}")
            continue
        try:
            plan = selected_store.resume_plan(
                str(state["job_id"]), str(lane["lane_id"])
            )
        except JobDiagnosticsError as exc:
            refused.append(f"{key}: {exc}")
            continue
        if not plan.safe or not plan.command:
            refused.append(f"{key}: {', '.join(plan.reasons)}")
            continue
        ready.append((key, state, lane))

    safe: list[str] = []
    for key, _state, lane in ready:
        conflicts: list[str] = []
        lane_resources = set(map(str, lane.get("resources") or []))
        lane_worktree = (lane.get("repository") or {}).get("worktree")
        for active_key, _active_state, active_lane in active:
            shared = lane_resources & set(map(str, active_lane.get("resources") or []))
            active_worktree = (active_lane.get("repository") or {}).get("worktree")
            same_writable_worktree = (
                lane_worktree
                and lane_worktree == active_worktree
                and not (lane.get("read_only") and active_lane.get("read_only"))
            )
            if shared:
                conflicts.append(
                    f"shares resource {sorted(shared)[0]} with {active_key}"
                )
            if same_writable_worktree:
                conflicts.append(f"shares writable worktree with {active_key}")
        for safe_key in safe:
            _safe_state, safe_lane = index[safe_key]
            shared = lane_resources & set(map(str, safe_lane.get("resources") or []))
            same_writable_worktree = (
                lane_worktree
                and lane_worktree == (safe_lane.get("repository") or {}).get("worktree")
                and not (lane.get("read_only") and safe_lane.get("read_only"))
            )
            if shared:
                conflicts.append(f"shares resource {sorted(shared)[0]} with {safe_key}")
            if same_writable_worktree:
                conflicts.append(f"shares writable worktree with {safe_key}")
        if conflicts:
            refused.append(f"{key}: {'; '.join(conflicts)}")
        else:
            safe.append(key)

    lines = [
        "Safe parallel-work recommendation (read-only; nothing was launched)",
        f"Active lanes considered: {len(active)}",
        "Safe to start in parallel:",
    ]
    if safe:
        for key in safe:
            state, lane = index[key]
            phase = next(
                (
                    item
                    for item in lane.get("phases") or []
                    if isinstance(item, dict)
                    and item.get("status") != LaneStatus.COMPLETED.value
                ),
                {},
            )
            lines.append(
                f"  {key} · {phase.get('phase_id') or 'next phase'} · "
                f"{'read-only' if lane.get('read_only') else 'isolated writable worktree'}"
            )
    else:
        lines.append("  none")
    lines.append("Held back:")
    if refused:
        lines.extend(f"  {reason}" for reason in refused)
    else:
        lines.append("  none")
    if issues:
        lines.append(f"State warnings: {len(issues)} unreadable file(s) were ignored.")
    return "\n".join(lines)


def render_job_detail(
    job_id: str,
    *,
    store: JobStateStore | None = None,
    now: float | None = None,
    idle_after: float = DEFAULT_IDLE_AFTER_SECONDS,
    stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
) -> str:
    selected_store = store or JobStateStore()
    current = float(now if now is not None else selected_store.clock())
    state = _state_with_metrics(
        selected_store.load(job_id),
        now=current,
        idle_after=idle_after,
        stale_after=stale_after,
    )
    lines = [
        f"Job: {state['job_id']}",
        f"Title: {state.get('title') or state['job_id']}",
        f"Status: {state.get('status')}",
        f"Elapsed: {_format_duration((state.get('timing') or {}).get('total_elapsed') or 0)}",
        "Lanes:",
    ]
    for lane in (state.get("lanes") or {}).values():
        repository = lane.get("repository") or {}
        blocker = lane.get("blocker") or {}
        lines.extend([
            f"  {lane['lane_id']} · {lane.get('effective_status')} · "
            f"{_format_duration(lane.get('elapsed_seconds') or 0)}",
            f"    step: {lane.get('current_step') or 'unknown'}",
            f"    output: {lane.get('last_meaningful_output') or 'none'}",
            f"    retries: {int(lane.get('retry_count') or 0)}",
            f"    process: pid={((lane.get('process') or {}).get('pid'))} "
            f"session={lane.get('session_id') or '-'} task={lane.get('task_id') or '-'}",
            f"    repository: {repository.get('worktree') or '-'} · "
            f"{repository.get('branch') or '-'} · {repository.get('head') or '-'}",
            f"    blocker: {blocker.get('kind') or 'none'}",
            f"    next: {lane.get('next_expected_action') or 'none'}",
        ])
    return "\n".join(lines)


def render_resume_plan(plan: ResumePlan) -> str:
    verdict = "SAFE" if plan.safe else "REFUSED"
    lines = [
        f"Resume plan: {verdict}",
        f"Job/lane: {plan.job_id} / {plan.lane_id}",
        f"Phase: {plan.phase_id or 'none'}",
        f"Worktree: {plan.worktree or 'unknown'}",
        f"Command: {plan.command or 'not recorded'}",
        f"Blocker: {plan.blocker or 'none'}",
        "Reasons:",
    ]
    lines.extend(f"  - {reason}" for reason in plan.reasons)
    if plan.evidence_paths:
        lines.append("Evidence:")
        lines.extend(f"  - {path}" for path in plan.evidence_paths)
    lines.append(f"Next action: {plan.next_action}")
    lines.append("No command was launched and no job state was changed.")
    return "\n".join(lines)


def run_jobs_slash(text: str, *, store: JobStateStore | None = None) -> str:
    """Shared classic-CLI/gateway parser for read-only ``/jobs``."""

    raw = (text or "").strip()
    if raw.startswith("/"):
        raw = raw[1:]
    if raw.startswith("jobs"):
        raw = raw[4:].lstrip()
    try:
        tokens = shlex.split(raw) if raw else []
    except ValueError as exc:
        return f"Invalid /jobs arguments: {exc}"
    action = tokens[0].lower() if tokens else "status"
    args = tokens[1:]
    selected_store = store or JobStateStore()
    thresholds = _configured_thresholds()
    try:
        if action in {"status", "dashboard", "blocked", "active"}:
            return render_dashboard(
                selected_store,
                idle_after=thresholds["idle_after"],
                stale_after=thresholds["stale_after"],
            )
        if action == "show":
            if not args:
                return "Usage: /jobs show <job-id>"
            return render_job_detail(
                args[0],
                store=selected_store,
                idle_after=thresholds["idle_after"],
                stale_after=thresholds["stale_after"],
            )
        if action in {"why", "why-slow", "slow"}:
            if not args:
                return "Usage: /jobs why-slow <job-id> [lane-id]"
            return why_slow_report(
                args[0],
                lane_id=args[1] if len(args) > 1 else None,
                store=selected_store,
                **thresholds,
            )
        if action in {"parallel", "parallel-plan"}:
            return parallel_recommendation(
                job_id=args[0] if args else None,
                store=selected_store,
                idle_after=thresholds["idle_after"],
                stale_after=thresholds["stale_after"],
            )
        if action in {"resume", "resume-plan"}:
            if not args:
                return "Usage: /jobs resume-plan <job-id> [lane-id]"
            plan = selected_store.resume_plan(
                args[0],
                args[1] if len(args) > 1 else None,
            )
            return render_resume_plan(plan)
    except JobDiagnosticsError as exc:
        return f"Job diagnostics error: {exc}"
    return (
        "Usage: /jobs [status|show <job>|why-slow <job> [lane]|"
        "parallel [job]|resume-plan <job> [lane]]"
    )


def jobs_command(args: Any, *, store: JobStateStore | None = None) -> int:
    """Argparse command handler for ``hermes jobs``."""

    selected_store = store or JobStateStore()
    action = getattr(args, "jobs_action", None) or "status"
    action = {
        "dashboard": "status",
        "why": "why-slow",
        "resume": "resume-plan",
    }.get(action, action)
    as_json = bool(getattr(args, "json", False))
    thresholds = _configured_thresholds()
    try:
        if action == "status":
            if as_json:
                print(
                    json.dumps(
                        diagnostics_snapshot(
                            selected_store,
                            idle_after=thresholds["idle_after"],
                            stale_after=thresholds["stale_after"],
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    render_dashboard(
                        selected_store,
                        idle_after=thresholds["idle_after"],
                        stale_after=thresholds["stale_after"],
                    )
                )
        elif action == "show":
            state = selected_store.load(args.job_id)
            if as_json:
                print(
                    json.dumps(
                        _state_with_metrics(
                            state,
                            now=selected_store.clock(),
                            idle_after=thresholds["idle_after"],
                            stale_after=thresholds["stale_after"],
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    render_job_detail(
                        args.job_id,
                        store=selected_store,
                        idle_after=thresholds["idle_after"],
                        stale_after=thresholds["stale_after"],
                    )
                )
        elif action == "why-slow":
            print(
                why_slow_report(
                    args.job_id,
                    lane_id=getattr(args, "lane", None),
                    store=selected_store,
                    **thresholds,
                )
            )
        elif action == "parallel":
            print(
                parallel_recommendation(
                    job_id=getattr(args, "job_id", None),
                    store=selected_store,
                    idle_after=thresholds["idle_after"],
                    stale_after=thresholds["stale_after"],
                )
            )
        elif action == "resume-plan":
            plan = selected_store.resume_plan(
                args.job_id,
                getattr(args, "lane", None),
            )
            print(
                json.dumps(plan.to_dict(), indent=2, sort_keys=True)
                if as_json
                else render_resume_plan(plan)
            )
            return 0 if plan.safe else 2
        else:
            raise JobDiagnosticsError(f"unknown jobs action: {action}")
    except JobDiagnosticsError as exc:
        print(f"Job diagnostics error: {exc}")
        return 2
    return 0
