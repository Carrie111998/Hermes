"""Event-driven ownership and bounded recovery for dispatcher worker processes.

The dispatcher keeps the :class:`subprocess.Popen` handle instead of abandoning
it after launch.  A dedicated monitor thread blocks in ``Popen.wait()`` (an OS
child-terminal event), parses the worker's JSONL evidence file, cleans only the
processes that worker registered as owned, and permits one continuation after
an explicit recovery signal.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import inspect
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import threading
import time
from typing import Callable, Optional
import uuid

from hermes_cli.worker_lifecycle import (
    SCHEMA_VERSION,
    ExitKind,
    FailureReason,
    LifecycleEventType,
    TerminalClassification,
    exit_kind_and_value,
    process_birth_token,
)

TRANSIENT_PROVIDER = "transient_provider"


@dataclass(frozen=True)
class WorkerIdentity:
    """Identity that must not change across a supervised continuation."""

    task_id: str
    session_id: str
    worktree: Path
    run_id: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "worktree", Path(self.worktree).resolve())

    @property
    def key(self) -> tuple[str, object]:
        return self.task_id, self.run_id


@dataclass(frozen=True)
class SessionCompressionLineageResolver:
    """Verify terminal-session lineage in one exact profile state database."""

    state_db_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_db_path", Path(self.state_db_path).resolve())

    def verifies(self, expected_session_id: str, observed_session_id: str) -> bool:
        try:
            if not self.state_db_path.is_file():
                return False
            from hermes_state import SessionDB

            session_db = SessionDB(self.state_db_path, read_only=True)
            try:
                return session_db.is_verified_compression_descendant(
                    expected_session_id,
                    observed_session_id,
                )
            finally:
                session_db.close()
        except Exception:
            # State identity is a security boundary: unavailable, unreadable,
            # malformed, or otherwise unprovable lineage always fails closed.
            return False


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    role: str
    port: Optional[int] = None


@dataclass(frozen=True)
class AttemptExit:
    task_id: str
    run_id: object
    session_id: str
    worktree: Path
    attempt: int
    pid: int
    exit_code: int
    classification: str
    events: tuple[dict, ...]
    owned_processes: tuple[OwnedProcess, ...]

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "worktree": str(self.worktree),
            "attempt": self.attempt,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "classification": self.classification,
            "owned_processes": [
                {"pid": item.pid, "role": item.role, "port": item.port}
                for item in self.owned_processes
            ],
        }


@dataclass(frozen=True)
class LifecycleFailure:
    task_id: str
    run_id: object
    session_id: str
    attempts: int
    classification: str
    exit_code: int
    reason: str

    def as_dict(self) -> dict:
        classifications = {
            *(item.value for item in TerminalClassification),
            "callback_failure",
            "invalid_evidence",
            "process_exit",
            "retry_setup_failure",
        }
        reasons = classifications | {
            "cleanup_not_verified",
            "credential_recovered",
            "provider_recovered",
            "recovery_timeout",
        }
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "attempts": self.attempts,
            "classification": (
                self.classification
                if self.classification in classifications
                else TerminalClassification.SUPERVISOR_FAILURE.value
            ),
            "exit_code": self.exit_code,
            "reason": self.reason if self.reason in reasons else "unspecified",
        }


LaunchWorker = Callable[..., subprocess.Popen]
ExitCallback = Callable[[AttemptExit], None]
SuccessCallback = Callable[[AttemptExit], None]
Notifier = Callable[[LifecycleFailure], None]


def pid_is_alive(pid: int) -> bool:
    """Return whether one exact host PID is alive without signaling it."""

    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            process_query_limited_information | synchronize,
            False,
            int(pid),
        )
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # type: ignore[attr-defined]
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_events(path: Path) -> tuple[tuple[dict, ...], bool]:
    if not path.exists():
        return (), True
    events: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except (TypeError, ValueError):
            return tuple(events), False
        if not isinstance(event, dict):
            return tuple(events), False
        try:
            LifecycleEventType(event["kind"])
        except (KeyError, TypeError, ValueError):
            return tuple(events), False
        events.append(event)
    return tuple(events), True


_TERMINAL_MATRIX = {
    TerminalClassification.SUCCESS: FailureReason.NONE,
    TerminalClassification.TRANSIENT_PROVIDER: FailureReason.TRANSIENT_PROVIDER,
    TerminalClassification.RATE_LIMITED: FailureReason.RATE_LIMIT,
    TerminalClassification.BILLING: FailureReason.BILLING,
    TerminalClassification.FAILED: FailureReason.WORKER_FAILURE,
    TerminalClassification.SUPERVISOR_FAILURE: FailureReason.SUPERVISOR_FAILURE,
    TerminalClassification.OWNERSHIP_LOSS: FailureReason.OWNERSHIP_LOSS,
}


def _bind_process_birth(proc: subprocess.Popen) -> str:
    """Capture birth identity while the exact process handle is still live."""
    token = getattr(proc, "_hermes_process_birth_token", None)
    if not isinstance(token, str) or not token:
        token = process_birth_token(int(proc.pid))
    if token is None:
        raise RuntimeError(f"could not obtain process birth identity for PID {proc.pid}")
    proc._hermes_process_birth_token = token  # type: ignore[attr-defined]
    return token


@dataclass(frozen=True)
class _AttemptOwnership:
    nonce: str
    launcher_pid: int
    launcher_birth_token: str
    root_pid: int
    root_birth_token: str


def _is_live_launcher_or_descendant(
    proc: subprocess.Popen,
    launcher_birth_token: str,
    observed_pid: int,
) -> bool:
    """Prove the observed interpreter belongs to the still-live launcher."""

    if proc.poll() is not None:
        return False
    launcher_pid = int(proc.pid)
    if observed_pid == launcher_pid:
        return process_birth_token(launcher_pid) == launcher_birth_token
    try:
        import psutil

        current = psutil.Process(observed_pid)
        while True:
            current = current.parent()
            if current is None:
                return False
            if int(current.pid) == launcher_pid:
                return (
                    proc.poll() is None
                    and process_birth_token(launcher_pid) == launcher_birth_token
                )
    except Exception:
        return False


def _start_identity_matches(
    event: dict,
    identity: WorkerIdentity,
    attempt: int,
    nonce: str,
) -> bool:
    try:
        event_worktree = event["worktree"]
        return (
            type(event.get("schema_version")) is int
            and event["schema_version"] == SCHEMA_VERSION
            and LifecycleEventType(event["kind"]) is LifecycleEventType.IDENTITY
            and isinstance(event.get("nonce"), str)
            and secrets.compare_digest(event["nonce"], nonce)
            and isinstance(event.get("task_id"), str)
            and event["task_id"] == identity.task_id
            and type(event.get("run_id")) is int
            and event["run_id"] > 0
            and event["run_id"] == identity.run_id
            and type(event.get("attempt")) is int
            and event["attempt"] == attempt
            and isinstance(event_worktree, str)
            and event_worktree == str(identity.worktree)
            and str(Path(event_worktree).resolve()) == event_worktree
            and isinstance(event.get("observed_session_id"), str)
            and event["observed_session_id"] == identity.session_id
            and type(event.get("root_pid")) is int
            and event["root_pid"] > 0
            and isinstance(event.get("process_birth_token"), str)
            and bool(event["process_birth_token"])
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _identity_matches_ownership(
    event: dict,
    identity: WorkerIdentity,
    attempt: int,
    ownership: _AttemptOwnership,
) -> bool:
    return (
        _start_identity_matches(event, identity, attempt, ownership.nonce)
        and event.get("root_pid") == ownership.root_pid
        and event.get("process_birth_token") == ownership.root_birth_token
    )


def _terminal_event_matches(
    event: dict,
    identity: WorkerIdentity,
    attempt: int,
    proc: subprocess.Popen,
    ownership: _AttemptOwnership,
    lineage_resolver: Optional[SessionCompressionLineageResolver] = None,
) -> bool:
    """Validate typed terminal evidence against the exact owned attempt."""

    try:
        classification = TerminalClassification(event["classification"])
        failure_reason = FailureReason(event["failure_reason"])
        exit_kind = ExitKind(event["exit_kind"])
        exit_value = event["exit_value"]
        event_worktree = event["worktree"]
        expected_kind, expected_value = exit_kind_and_value(int(proc.returncode))
        successful_exit = exit_kind is ExitKind.CODE and exit_value == 0
        matrix_valid = _TERMINAL_MATRIX[classification] is failure_reason
        observed_session_id = event.get("observed_session_id")
        session_valid = (
            isinstance(observed_session_id, str)
            and (
                observed_session_id == identity.session_id
                or (
                    lineage_resolver is not None
                    and lineage_resolver.verifies(
                        identity.session_id,
                        observed_session_id,
                    )
                )
            )
        )
        if classification is TerminalClassification.SUCCESS:
            matrix_valid = matrix_valid and successful_exit
        else:
            matrix_valid = matrix_valid and not successful_exit
        return (
            type(event.get("schema_version")) is int
            and event["schema_version"] == SCHEMA_VERSION
            and LifecycleEventType(event["kind"]) is LifecycleEventType.TERMINAL
            and isinstance(event.get("nonce"), str)
            and secrets.compare_digest(event["nonce"], ownership.nonce)
            and isinstance(event.get("task_id"), str)
            and event["task_id"] == identity.task_id
            and type(event.get("run_id")) is int
            and event["run_id"] > 0
            and event["run_id"] == identity.run_id
            and type(event.get("attempt")) is int
            and event["attempt"] == attempt
            and isinstance(event.get("expected_session_id"), str)
            and event["expected_session_id"] == identity.session_id
            and session_valid
            and isinstance(event_worktree, str)
            and event_worktree == str(identity.worktree)
            and str(Path(event_worktree).resolve()) == event_worktree
            and type(event.get("root_pid")) is int
            and event["root_pid"] == ownership.root_pid
            and isinstance(event.get("process_birth_token"), str)
            and event["process_birth_token"] == ownership.root_birth_token
            and type(exit_value) is int
            and exit_value >= 0
            and exit_kind is expected_kind
            and exit_value == expected_value
            and matrix_valid
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _attempt_exit(
    identity: WorkerIdentity,
    attempt: int,
    proc: subprocess.Popen,
    path: Path,
    ownership: _AttemptOwnership,
    owned_pids: Optional[set[int]] = None,
    lineage_resolver: Optional[SessionCompressionLineageResolver] = None,
) -> AttemptExit:
    events, events_valid = _read_events(path)
    identity_events = [
        event for event in events if event.get("kind") == LifecycleEventType.IDENTITY.value
    ]
    terminal_events = [
        event for event in events if event.get("kind") == LifecycleEventType.TERMINAL.value
    ]
    terminal = (
        terminal_events[0]
        if events_valid
        and len(identity_events) == 1
        and _identity_matches_ownership(
            identity_events[0], identity, attempt, ownership
        )
        and len(terminal_events) == 1
        and _terminal_event_matches(
            terminal_events[0],
            identity,
            attempt,
            proc,
            ownership,
            lineage_resolver,
        )
        else None
    )
    classification = (
        str(terminal["classification"])
        if terminal is not None
        else (
            "invalid_evidence"
            if not events_valid or terminal_events or len(identity_events) != 1
            else "process_exit"
        )
    )
    owned: list[OwnedProcess] = []
    seen: set[int] = set()
    for event in events if terminal is not None else ():
        if event.get("kind") != "owned_process":
            continue
        try:
            pid = int(event["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid <= 0 or pid in seen:
            continue
        seen.add(pid)
        try:
            port = int(event["port"]) if event.get("port") is not None else None
        except (TypeError, ValueError):
            port = None
        owned.append(OwnedProcess(pid=pid, role=str(event.get("role") or "background"), port=port))
    return AttemptExit(
        task_id=identity.task_id,
        run_id=identity.run_id,
        session_id=identity.session_id,
        worktree=identity.worktree,
        attempt=attempt,
        pid=ownership.root_pid,
        exit_code=int(proc.returncode),
        classification=classification,
        events=events,
        owned_processes=tuple(owned),
    )


def _descendant_pids(pid: int) -> set[int]:
    """Snapshot descendants using the project's cross-platform process API."""

    try:
        import psutil

        return {int(child.pid) for child in psutil.Process(int(pid)).children(recursive=True)}
    except Exception:
        return set()


def _wait_with_descendant_tracking(proc: subprocess.Popen, interval: float) -> set[int]:
    """Wait on the exact handle while retaining descendants seen before exit."""

    stopped = threading.Event()
    observed: set[int] = set()

    def track() -> None:
        while not stopped.is_set():
            observed.update(_descendant_pids(int(proc.pid)))
            stopped.wait(interval)

    tracker = threading.Thread(target=track, name=f"worker-tree-{proc.pid}", daemon=True)
    tracker.start()
    try:
        returncode = proc.wait()
        if getattr(proc, "returncode", None) is None:
            proc.returncode = returncode
    finally:
        observed.update(_descendant_pids(int(proc.pid)))
        stopped.set()
        tracker.join(timeout=max(0.1, interval * 4))
    return observed


def _terminate_process_tree(proc: subprocess.Popen) -> bool:
    """Terminate the owned root and only descendants observed from that root."""

    tracked = set(getattr(proc, "_hermes_owned_descendants", ()))
    tracked.update(_descendant_pids(int(proc.pid)))
    try:
        import psutil

        processes = []
        for pid in sorted(tracked, reverse=True):
            try:
                processes.append(psutil.Process(pid))
            except psutil.Error:
                pass
        try:
            root = psutil.Process(int(proc.pid))
        except psutil.Error:
            root = None
        for process in processes:
            try:
                process.terminate()
            except psutil.Error:
                pass
        if root is not None:
            try:
                root.terminate()
            except psutil.Error:
                pass
        _, survivors = psutil.wait_procs(
            processes + ([root] if root is not None else []), timeout=1.0
        )
        for process in survivors:
            try:
                process.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(survivors, timeout=1.0)
    except Exception:
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass
    return not any(pid_is_alive(pid) for pid in tracked) and not pid_is_alive(int(proc.pid))


def _cleanup_failed_start(proc: subprocess.Popen, timeout: float) -> None:
    """Clean only through the retained handle after handshake rejection."""

    expected_birth = getattr(proc, "_hermes_process_birth_token", None)
    try:
        if proc.poll() is not None:
            return
        if (
            not isinstance(expected_birth, str)
            or process_birth_token(int(proc.pid)) != expected_birth
        ):
            return
        proc.terminate()
        try:
            proc.wait(timeout=max(0.1, timeout))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=max(0.1, timeout))
    except Exception:
        return


def _cleanup_owned_tree(
    attempt_exit: AttemptExit,
    proc: subprocess.Popen,
    tracked: set[int],
    timeout: float,
    cleanup_fn: Callable[[subprocess.Popen], bool],
) -> bool:
    """Clean a verified tree; never signal an unverified registered PID."""

    proc._hermes_owned_descendants = tuple(tracked)  # type: ignore[attr-defined]
    try:
        reported_clean = bool(cleanup_fn(proc))
    except Exception:
        reported_clean = False
    registered = {
        item.pid for item in attempt_exit.owned_processes if item.pid != attempt_exit.pid
    }
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        if not any(pid_is_alive(pid) for pid in tracked | registered):
            break
        time.sleep(0.02)
    # A live PID reported by the worker but never observed as a descendant is
    # evidence mismatch.  It blocks retry but is deliberately never signaled.
    unverified_survivor = any(
        pid not in tracked and pid_is_alive(pid) for pid in registered
    )
    survivor = any(pid_is_alive(pid) for pid in tracked)
    return reported_clean and not survivor and not unverified_survivor


class WorkerLifecycleHandle:
    def __init__(self, supervisor: "DispatcherWorkerSupervisor", identity: WorkerIdentity) -> None:
        self.task_id = identity.task_id
        self.identity = identity
        self._supervisor = supervisor
        self._recovery = threading.Event()
        self._finished = threading.Event()
        self._recovery_reason = ""
        self._waiting_since: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._pid: Optional[int] = None

    def signal_recovery(self, reason: str) -> bool:
        # Compatibility for callers holding a handle: wait briefly for the
        # on-exit callback/cleanup boundary, then use the same exact gate.
        deadline = time.monotonic() + self._supervisor._cleanup_timeout
        while self._waiting_since is None and not self._finished.is_set():
            if time.monotonic() >= deadline:
                return False
            time.sleep(self._supervisor._poll_interval)
        return self._supervisor.signal_recovery(self.identity, reason)

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._finished.wait(timeout)

    @property
    def recovery_reason(self) -> str:
        return self._recovery_reason

    @property
    def pid(self) -> Optional[int]:
        return self._pid


class DispatcherWorkerSupervisor:
    """Own worker handles and perform at most one signaled continuation."""

    def __init__(
        self,
        *,
        event_root: Path,
        recovery_timeout: float = 300.0,
        cleanup_timeout: float = 5.0,
        start_timeout: float = 5.0,
        poll_interval: float = 0.01,
        cleanup_fn: Optional[Callable[[subprocess.Popen], bool]] = None,
    ) -> None:
        self._event_root = Path(event_root)
        self._recovery_timeout = max(0.1, float(recovery_timeout))
        self._cleanup_timeout = max(0.1, float(cleanup_timeout))
        self._start_timeout = max(0.05, float(start_timeout))
        self._poll_interval = max(0.001, float(poll_interval))
        self._cleanup_fn = cleanup_fn or _terminate_process_tree
        self._active: dict[tuple[str, object], WorkerLifecycleHandle] = {}
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_pids(self) -> set[int]:
        """Return exact live-handle PIDs owned by this supervisor."""
        with self._lock:
            return {
                int(handle._pid)
                for handle in self._active.values()
                if handle._pid is not None
            }

    def active_pid(self, task_id: str, run_id: object = None) -> Optional[int]:
        """Return the PID for one exact run (or one unambiguous legacy task)."""
        with self._lock:
            if run_id is not None:
                handle = self._active.get((task_id, run_id))
            else:
                matches = [
                    item for key, item in self._active.items() if key[0] == task_id
                ]
                handle = matches[0] if len(matches) == 1 else None
            if handle is None or handle._pid is None:
                return None
            return int(handle._pid)

    def is_waiting_for_recovery(self, identity: WorkerIdentity) -> bool:
        with self._lock:
            handle = self._active.get(identity.key)
            return bool(
                handle is not None
                and handle.identity == identity
                and handle._waiting_since is not None
            )

    def start(
        self,
        identity: WorkerIdentity,
        launch: LaunchWorker,
        *,
        on_exit: Optional[ExitCallback] = None,
        on_success: Optional[SuccessCallback] = None,
        notifier: Optional[Notifier] = None,
        on_pid: Optional[Callable[[WorkerIdentity, int, int], None]] = None,
        gate_advance: Optional[Callable[[WorkerIdentity], None]] = None,
        initial_proc: Optional[subprocess.Popen] = None,
        initial_event_path: Optional[Path] = None,
        lineage_resolver: Optional[SessionCompressionLineageResolver] = None,
    ) -> WorkerLifecycleHandle:
        if (initial_proc is None) != (initial_event_path is None):
            raise ValueError("initial_proc and initial_event_path must be provided together")
        with self._lock:
            if identity.key in self._active:
                raise RuntimeError(f"worker {identity.key!r} is already supervised")
            handle = WorkerLifecycleHandle(self, identity)
            self._active[identity.key] = handle

        self._event_root.mkdir(parents=True, exist_ok=True)
        try:
            if initial_proc is None:
                first_proc, first_path, first_ownership = self._launch_attempt(
                    identity, launch, 1
                )
            else:
                assert initial_event_path is not None
                first_proc = initial_proc
                first_path = Path(initial_event_path)
                launcher_birth = _bind_process_birth(first_proc)
                initial_nonce = getattr(first_proc, "_hermes_worker_start_nonce", None)
                if not isinstance(initial_nonce, str) or not initial_nonce:
                    raise RuntimeError("initial process has no pre-spawn start nonce")
                first_ownership = self._await_start_identity(
                    identity,
                    1,
                    first_proc,
                    first_path,
                    initial_nonce,
                    launcher_birth,
                )
            with self._lock:
                handle._pid = first_ownership.root_pid
            if on_pid is not None:
                on_pid(identity, 1, first_ownership.root_pid)
        except Exception as exc:
            if "first_proc" in locals():
                _cleanup_failed_start(first_proc, self._cleanup_timeout)
            if "first_path" in locals():
                try:
                    first_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._notify_once(
                handle, notifier, 1, "setup_failure", -1, str(exc)
            )
            with self._lock:
                self._active.pop(identity.key, None)
            handle._finished.set()
            raise
        thread = threading.Thread(
            target=self._monitor,
            name=f"worker-supervisor-{identity.task_id}",
            args=(
                identity, launch, handle, first_proc, first_path, on_exit,
                first_ownership, on_success, notifier, on_pid, gate_advance,
                lineage_resolver,
            ),
            daemon=True,
        )
        handle._thread = thread
        thread.start()
        return handle

    def signal_recovery(
        self,
        identity: WorkerIdentity | str,
        reason: str,
        *,
        signaled_at: Optional[float] = None,
    ) -> bool:
        with self._lock:
            if isinstance(identity, WorkerIdentity):
                handle = self._active.get(identity.key)
                if handle is not None and handle.identity != identity:
                    handle = None
            else:
                matches = [
                    item for key, item in self._active.items() if key[0] == identity
                ]
                handle = matches[0] if len(matches) == 1 else None
            now = time.monotonic() if signaled_at is None else float(signaled_at)
            if (
                handle is None
                or not reason
                or handle._waiting_since is None
                or now < handle._waiting_since
                or handle._recovery.is_set()
            ):
                return False
            handle._recovery_reason = str(reason)
            handle._recovery.set()
            return True

    def _launch_attempt(
        self,
        identity: WorkerIdentity,
        launch: LaunchWorker,
        attempt: int,
    ) -> tuple[subprocess.Popen, Path, _AttemptOwnership]:
        event_path = self.allocate_event_path(identity, attempt)
        nonce = secrets.token_urlsafe(32)
        parameters = inspect.signature(launch).parameters
        supports_nonce = "start_nonce" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        proc = (
            launch(identity, attempt, event_path, start_nonce=nonce)
            if supports_nonce
            else launch(identity, attempt, event_path)
        )
        proc._hermes_worker_start_nonce = nonce  # type: ignore[attr-defined]
        launcher_birth = _bind_process_birth(proc)
        try:
            ownership = self._await_start_identity(
                identity, attempt, proc, event_path, nonce, launcher_birth
            )
        except Exception:
            _cleanup_failed_start(proc, self._cleanup_timeout)
            try:
                event_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return proc, event_path, ownership

    def _await_start_identity(
        self,
        identity: WorkerIdentity,
        attempt: int,
        proc: subprocess.Popen,
        event_path: Path,
        nonce: str,
        launcher_birth: str,
    ) -> _AttemptOwnership:
        deadline = time.monotonic() + self._start_timeout
        while True:
            events, valid = _read_events(event_path)
            if not valid:
                raise RuntimeError("malformed child-start identity record")
            identity_events = [
                event
                for event in events
                if event.get("kind") == LifecycleEventType.IDENTITY.value
            ]
            if len(identity_events) > 1:
                raise RuntimeError("duplicate child-start identity records")
            if identity_events:
                start_event = identity_events[0]
                if events[0] is not start_event or not _start_identity_matches(
                    start_event, identity, attempt, nonce
                ):
                    raise RuntimeError("child-start identity contract mismatch")
                observed_pid = int(start_event["root_pid"])
                observed_birth = str(start_event["process_birth_token"])
                if process_birth_token(observed_pid) != observed_birth:
                    raise RuntimeError("child-start process birth mismatch")
                if not _is_live_launcher_or_descendant(
                    proc, launcher_birth, observed_pid
                ):
                    raise RuntimeError("child-start PID is not owned by launcher")
                if process_birth_token(observed_pid) != observed_birth:
                    raise RuntimeError("child-start process identity changed")
                return _AttemptOwnership(
                    nonce=nonce,
                    launcher_pid=int(proc.pid),
                    launcher_birth_token=launcher_birth,
                    root_pid=observed_pid,
                    root_birth_token=observed_birth,
                )
            if events:
                raise RuntimeError("child-start identity was not the first record")
            if proc.poll() is not None:
                raise RuntimeError("worker exited before child-start identity")
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out waiting for child-start identity")
            time.sleep(self._poll_interval)

    def allocate_event_path(self, identity: WorkerIdentity, attempt: int) -> Path:
        self._event_root.mkdir(parents=True, exist_ok=True)
        event_path = self._event_root / (
            f"attempt-{attempt}.{uuid.uuid4().hex}.jsonl"
        )
        return event_path

    def _monitor(
        self,
        identity: WorkerIdentity,
        launch: LaunchWorker,
        handle: WorkerLifecycleHandle,
        proc: subprocess.Popen,
        event_path: Path,
        on_exit: Optional[ExitCallback],
        ownership: _AttemptOwnership,
        on_success: Optional[SuccessCallback],
        notifier: Optional[Notifier],
        on_pid: Optional[Callable[[WorkerIdentity, int, int], None]],
        gate_advance: Optional[Callable[[WorkerIdentity], None]],
        lineage_resolver: Optional[SessionCompressionLineageResolver],
    ) -> None:
        attempts = 0
        last_exit: Optional[AttemptExit] = None
        try:
            for attempt in (1, 2):
                attempts = attempt
                tracked = _wait_with_descendant_tracking(proc, self._poll_interval)
                last_exit = _attempt_exit(
                    identity,
                    attempt,
                    proc,
                    event_path,
                    ownership,
                    owned_pids=tracked,
                    lineage_resolver=lineage_resolver,
                )
                callback_error: Optional[Exception] = None
                try:
                    if on_exit is not None:
                        on_exit(last_exit)
                except Exception as exc:
                    callback_error = exc
                finally:
                    clean = _cleanup_owned_tree(
                        last_exit, proc, tracked, self._cleanup_timeout, self._cleanup_fn
                    )
                    try:
                        event_path.unlink(missing_ok=True)
                    except OSError:
                        clean = False

                if callback_error is not None or not clean:
                    self._notify_once(
                        handle,
                        notifier,
                        attempts,
                        last_exit.classification,
                        last_exit.exit_code,
                        str(callback_error) if callback_error else "cleanup_not_verified",
                    )
                    return

                if last_exit.classification == "success":
                    try:
                        if on_success is not None:
                            on_success(last_exit)
                        if attempt == 2 and gate_advance is not None:
                            gate_advance(identity)
                    except Exception as exc:
                        self._notify_once(
                            handle, notifier, attempts, "callback_failure",
                            last_exit.exit_code, str(exc),
                        )
                    return
                if attempt == 1 and last_exit.classification == TRANSIENT_PROVIDER:
                    with self._lock:
                        handle._waiting_since = time.monotonic()
                    recovered = handle._recovery.wait(self._recovery_timeout)
                    with self._lock:
                        handle._waiting_since = None
                    if not recovered:
                        self._notify_once(
                            handle, notifier, attempts, last_exit.classification,
                            last_exit.exit_code, "recovery_timeout",
                        )
                        return
                    try:
                        proc, event_path, ownership = self._launch_attempt(
                            identity, launch, 2
                        )
                        with self._lock:
                            handle._pid = ownership.root_pid
                        if on_pid is not None:
                            on_pid(identity, 2, ownership.root_pid)
                    except Exception as exc:
                        if "proc" in locals() and proc.poll() is None:
                            _cleanup_failed_start(proc, self._cleanup_timeout)
                        self._notify_once(
                            handle, notifier, 2, "retry_setup_failure", -1, str(exc)
                        )
                        return
                    continue
                break

            if last_exit is not None:
                self._notify_once(
                    handle, notifier, attempts, last_exit.classification,
                    last_exit.exit_code,
                    handle.recovery_reason or last_exit.classification,
                )
        finally:
            with self._lock:
                if self._active.get(identity.key) is handle:
                    self._active.pop(identity.key, None)
            handle._finished.set()

    def _notify_once(
        self,
        handle: WorkerLifecycleHandle,
        notifier: Optional[Notifier],
        attempts: int,
        classification: str,
        exit_code: int,
        reason: str,
    ) -> None:
        if getattr(handle, "_notified", False):
            return
        handle._notified = True  # type: ignore[attr-defined]
        if notifier is None:
            return
        try:
            notifier(
                LifecycleFailure(
                    task_id=handle.identity.task_id,
                    run_id=handle.identity.run_id,
                    session_id=handle.identity.session_id,
                    attempts=attempts,
                    classification=classification,
                    exit_code=exit_code,
                    reason=reason,
                )
            )
        except Exception:
            # Notification is a terminal seam; a broken notifier must not keep
            # ownership alive or cause a duplicate notification attempt.
            pass
