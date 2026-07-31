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
    run_id: object = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "worktree", Path(self.worktree).resolve())

    @property
    def key(self) -> tuple[str, object]:
        return self.task_id, self.run_id


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


def _terminal_event_matches(
    event: dict,
    identity: WorkerIdentity,
    attempt: int,
    proc: subprocess.Popen,
    owned_pids: set[int],
) -> bool:
    """Validate typed terminal evidence against the exact owned attempt."""

    try:
        return (
            event.get("schema_version") == 1
            and event.get("kind") == "terminal"
            and event.get("task_id") == identity.task_id
            and event.get("run_id") == identity.run_id
            and int(event.get("attempt")) == attempt
            and event.get("session_id") == identity.session_id
            and Path(str(event.get("worktree"))).resolve() == identity.worktree
            and int(event.get("owner_pid")) in ({int(proc.pid)} | owned_pids)
            and int(event.get("exit_code")) == int(proc.returncode)
            and isinstance(event.get("classification"), str)
        )
    except (TypeError, ValueError, OSError):
        return False


def _attempt_exit(
    identity: WorkerIdentity,
    attempt: int,
    proc: subprocess.Popen,
    path: Path,
    owned_pids: Optional[set[int]] = None,
) -> AttemptExit:
    events = _read_events(path)
    terminal = next(
        (
            event
            for event in reversed(events)
            if _terminal_event_matches(
                event, identity, attempt, proc, owned_pids or set()
            )
        ),
        None,
    )
    classification = (
        str(terminal["classification"])
        if terminal is not None
        else ("success" if proc.returncode == 0 else "process_exit")
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
        pid=int(proc.pid),
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
        poll_interval: float = 0.01,
        cleanup_fn: Optional[Callable[[subprocess.Popen], bool]] = None,
    ) -> None:
        self._event_root = Path(event_root)
        self._recovery_timeout = max(0.1, float(recovery_timeout))
        self._cleanup_timeout = max(0.1, float(cleanup_timeout))
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
                first_proc, first_path = self._launch_attempt(identity, launch, 1)
            else:
                assert initial_event_path is not None
                first_proc = initial_proc
                first_path = Path(initial_event_path)
            with self._lock:
                handle._pid = int(first_proc.pid)
            if on_pid is not None:
                on_pid(identity, 1, int(first_proc.pid))
        except Exception as exc:
            if "first_proc" in locals():
                _terminate_process_tree(first_proc)
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
                on_success, notifier, on_pid, gate_advance,
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
    ) -> tuple[subprocess.Popen, Path]:
        event_path = self.allocate_event_path(identity, attempt)
        return launch(identity, attempt, event_path), event_path

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
        on_success: Optional[SuccessCallback],
        notifier: Optional[Notifier],
        on_pid: Optional[Callable[[WorkerIdentity, int, int], None]],
        gate_advance: Optional[Callable[[WorkerIdentity], None]],
    ) -> None:
        attempts = 0
        last_exit: Optional[AttemptExit] = None
        try:
            for attempt in (1, 2):
                attempts = attempt
                tracked = _wait_with_descendant_tracking(proc, self._poll_interval)
                last_exit = _attempt_exit(
                    identity, attempt, proc, event_path, owned_pids=tracked
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
                        proc, event_path = self._launch_attempt(identity, launch, 2)
                        with self._lock:
                            handle._pid = int(proc.pid)
                        if on_pid is not None:
                            on_pid(identity, 2, int(proc.pid))
                    except Exception as exc:
                        if "proc" in locals() and proc.poll() is None:
                            _terminate_process_tree(proc)
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
