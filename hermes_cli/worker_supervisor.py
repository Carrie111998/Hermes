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
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Callable, Optional
import uuid


TRANSIENT_PROVIDER = "transient_provider"


@dataclass(frozen=True)
class WorkerIdentity:
    """Identity that must not change across a supervised continuation."""

    task_id: str
    session_id: str
    worktree: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "worktree", Path(self.worktree).resolve())


@dataclass(frozen=True)
class OwnedProcess:
    pid: int
    role: str
    port: Optional[int] = None


@dataclass(frozen=True)
class AttemptExit:
    task_id: str
    attempt: int
    pid: int
    exit_code: int
    classification: str
    events: tuple[dict, ...]
    owned_processes: tuple[OwnedProcess, ...]

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
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
    attempts: int
    classification: str
    exit_code: int
    reason: str


LaunchWorker = Callable[[WorkerIdentity, int, Path], subprocess.Popen]
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


def _read_events(path: Path) -> tuple[dict, ...]:
    if not path.exists():
        return ()
    events: list[dict] = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return tuple(events)


def _attempt_exit(identity: WorkerIdentity, attempt: int, proc: subprocess.Popen, path: Path) -> AttemptExit:
    events = _read_events(path)
    failure = next(
        (event for event in reversed(events) if event.get("kind") == "failure"),
        None,
    )
    classification = (
        str(failure.get("classification"))
        if failure and failure.get("classification")
        else ("success" if proc.returncode == 0 else "process_exit")
    )
    owned: list[OwnedProcess] = []
    seen: set[int] = set()
    for event in events:
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
        attempt=attempt,
        pid=int(proc.pid),
        exit_code=int(proc.returncode),
        classification=classification,
        events=events,
        owned_processes=tuple(owned),
    )


def _terminate_exact_pid(pid: int, *, force: bool) -> None:
    if not pid_is_alive(pid):
        return
    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
        return
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


def _cleanup_owned_tree(attempt_exit: AttemptExit, timeout: float) -> None:
    """Terminate only this launch's process group and registered owned PIDs."""

    pids = {item.pid for item in attempt_exit.owned_processes if item.pid != attempt_exit.pid}
    if os.name != "nt":
        try:
            os.killpg(attempt_exit.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    for pid in pids:
        _terminate_exact_pid(pid, force=False)

    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline and any(pid_is_alive(pid) for pid in pids):
        time.sleep(0.02)
    for pid in pids:
        _terminate_exact_pid(pid, force=True)

    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline and any(pid_is_alive(pid) for pid in pids):
        time.sleep(0.02)


class WorkerLifecycleHandle:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self._recovery = threading.Event()
        self._finished = threading.Event()
        self._recovery_reason = ""
        self._thread: Optional[threading.Thread] = None
        self._pid: Optional[int] = None

    def signal_recovery(self, reason: str) -> None:
        if reason:
            self._recovery_reason = str(reason)
            self._recovery.set()

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
    ) -> None:
        self._event_root = Path(event_root)
        self._recovery_timeout = max(0.1, float(recovery_timeout))
        self._cleanup_timeout = max(0.1, float(cleanup_timeout))
        self._active: dict[str, WorkerLifecycleHandle] = {}
        self._lock = threading.Lock()

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def active_pid(self, task_id: str) -> Optional[int]:
        """Return the current PID while this supervisor owns task_id."""
        with self._lock:
            handle = self._active.get(task_id)
            if handle is None or handle._pid is None:
                return None
            return int(handle._pid)

    def start(
        self,
        identity: WorkerIdentity,
        launch: LaunchWorker,
        *,
        on_exit: ExitCallback,
        on_success: Optional[SuccessCallback] = None,
        notifier: Optional[Notifier] = None,
        initial_proc: Optional[subprocess.Popen] = None,
        initial_event_path: Optional[Path] = None,
    ) -> WorkerLifecycleHandle:
        if (initial_proc is None) != (initial_event_path is None):
            raise ValueError("initial_proc and initial_event_path must be provided together")
        with self._lock:
            if identity.task_id in self._active:
                raise RuntimeError(f"worker {identity.task_id} is already supervised")
            handle = WorkerLifecycleHandle(identity.task_id)
            self._active[identity.task_id] = handle

        self._event_root.mkdir(parents=True, exist_ok=True)
        try:
            if initial_proc is None:
                first_proc, first_path = self._launch_attempt(identity, launch, 1)
            else:
                assert initial_event_path is not None
                first_proc = initial_proc
                first_path = Path(initial_event_path)
            with self._lock:
                handle._pid = int(first_proc.pid)
        except Exception:
            with self._lock:
                self._active.pop(identity.task_id, None)
            raise
        thread = threading.Thread(
            target=self._monitor,
            name=f"worker-supervisor-{identity.task_id}",
            args=(identity, launch, handle, first_proc, first_path, on_exit, on_success, notifier),
            daemon=True,
        )
        handle._thread = thread
        thread.start()
        return handle

    def signal_recovery(self, task_id: str, reason: str) -> bool:
        with self._lock:
            handle = self._active.get(task_id)
        if handle is None:
            return False
        handle.signal_recovery(reason)
        return True

    def _launch_attempt(
        self,
        identity: WorkerIdentity,
        launch: LaunchWorker,
        attempt: int,
    ) -> tuple[subprocess.Popen, Path]:
        event_path = self.allocate_event_path(identity, attempt)
        return launch(identity, attempt, event_path), event_path

    def allocate_event_path(self, identity: WorkerIdentity, attempt: int) -> Path:
        self._event_root.mkdir(parents=True, exist_ok=True)
        event_path = self._event_root / (
            f"{identity.task_id}.attempt-{attempt}.{uuid.uuid4().hex}.jsonl"
        )
        return event_path

    def _monitor(
        self,
        identity: WorkerIdentity,
        launch: LaunchWorker,
        handle: WorkerLifecycleHandle,
        proc: subprocess.Popen,
        event_path: Path,
        on_exit: ExitCallback,
        on_success: Optional[SuccessCallback],
        notifier: Optional[Notifier],
    ) -> None:
        attempts = 0
        last_exit: Optional[AttemptExit] = None
        try:
            for attempt in (1, 2):
                attempts = attempt
                proc.wait()
                last_exit = _attempt_exit(identity, attempt, proc, event_path)
                try:
                    on_exit(last_exit)
                finally:
                    _cleanup_owned_tree(last_exit, self._cleanup_timeout)

                if last_exit.classification == "success":
                    if on_success is not None:
                        on_success(last_exit)
                    return
                if attempt == 1 and last_exit.classification == TRANSIENT_PROVIDER:
                    if not handle._recovery.wait(self._recovery_timeout):
                        break
                    proc, event_path = self._launch_attempt(identity, launch, 2)
                    with self._lock:
                        handle._pid = int(proc.pid)
                    continue
                break

            if notifier is not None and last_exit is not None:
                notifier(
                    LifecycleFailure(
                        task_id=identity.task_id,
                        attempts=attempts,
                        classification=last_exit.classification,
                        exit_code=last_exit.exit_code,
                        reason=handle.recovery_reason or "recovery_timeout",
                    )
                )
        finally:
            with self._lock:
                self._active.pop(identity.task_id, None)
            handle._finished.set()
