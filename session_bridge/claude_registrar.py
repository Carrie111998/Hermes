"""Single-claim interactive ConPTY registrar for native Claude visibility."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib.metadata
import inspect
import json
import os
import queue
import re
import select
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .claude_adapter import (
    ClaudeParseResult,
    ClaudeReadableSource,
    claude_project_directory_name,
)
from .claude_visibility import (
    ClaudeVisibilityCandidate,
    ClaudeVisibilityClaim,
    ClaudeVisibilityIdentity,
    build_claude_registration_prompt,
    derive_claude_visibility_identity,
    validate_claude_visibility_identity_binding,
)
from .models import OriginKind, Provider, SessionProjection


_MAX_RESPONSE_CHARS = 65_536
_RESPONSE_SETTLE_SECONDS = 0.5
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")


class InteractivePty(Protocol):
    def read_until(self, timeout: float, *, prompt: str | None = None) -> str: ...
    def write(self, data: str) -> None: ...
    def wait(self, timeout: float) -> int | None: ...
    def terminate(self, timeout: float = 1.0) -> bool: ...
    def close(self, timeout: float = 1.0) -> PtyCleanupResult: ...


class InteractivePtyFactory(Protocol):
    def spawn(self, argv: list[str], *, cwd: str) -> InteractivePty: ...


class ClaudeVisibilityStore(Protocol):
    def commit_claude_visibility_job(
        self, job_id: str, lease_digest: str, transcript_digest: str,
        visible_at: float,
    ) -> dict[str, object]: ...
    def retry_claude_visibility_job(
        self, job_id: str, lease_digest: str, error_code: str,
        next_attempt_at: float, detail: str,
    ) -> dict[str, object]: ...
    def fail_claude_visibility_job(
        self, job_id: str, lease_digest: str, error_code: str, detail: str,
    ) -> dict[str, object]: ...
    def record_claude_visibility_exact_id_absent(
        self, job_id: str, lease_digest: str, reserved_claude_uuid: str,
        attempt_ordinal: int, evidence_digest: str,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class ClaudeRegistrarOutcome:
    status: str
    job_id: str | None
    reserved_claude_uuid: str | None
    error_code: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class PtyCleanupResult:
    process_dead: bool
    reader_stopped: bool
    descriptors_closed: bool
    exit_code: int | None
    registrar_reader_stopped: bool | None = field(default=None, compare=False)
    transport_reader_stopped: bool | None = field(default=None, compare=False)

    @property
    def succeeded(self) -> bool:
        registrar_stopped = (
            self.reader_stopped
            if self.registrar_reader_stopped is None
            else self.registrar_reader_stopped
        )
        transport_stopped = (
            self.reader_stopped
            if self.transport_reader_stopped is None
            else self.transport_reader_stopped
        )
        return (
            self.process_dead
            and self.reader_stopped
            and registrar_stopped
            and transport_stopped
            and self.descriptors_closed
        )


class _TranscriptConflict(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class _ExactTranscript:
    path: Path
    parsed: ClaudeParseResult

    @property
    def projection(self) -> SessionProjection:
        return self.parsed.projection


class WindowsConPtyFactory:
    """Production pywinpty factory; imports remain safe off Windows."""

    def spawn(self, argv: list[str], *, cwd: str) -> InteractivePty:
        if not sys.platform.startswith("win"):
            raise RuntimeError("pty unavailable")
        try:
            process = self._spawn_process(list(argv), cwd=cwd)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise RuntimeError("pty unavailable") from exc
        try:
            return self._adapt_process(process)
        except Exception as exc:
            if not _reclaim_unadapted_process(process, timeout=2.0):
                raise RuntimeError("pty cleanup unconfirmed") from exc
            raise RuntimeError("pty unavailable") from exc

    def _spawn_process(self, argv: list[str], *, cwd: str) -> object:
        process_type = _registrar_pywinpty_process_type()
        return process_type.spawn(
            argv, cwd=cwd, env=os.environ.copy(), dimensions=(24, 120)
        )

    def _adapt_process(self, spawned: object) -> _WinPtyProcess:
        return _WinPtyProcess(
            spawned, require_supported_layout=True, direct_native_pty=True
        )


def _registrar_pywinpty_process_type() -> Any:
    try:
        from winpty import PTY, PtyProcess
    except ImportError as exc:
        raise RuntimeError("pywinpty import unavailable") from exc
    try:
        version = importlib.metadata.version("pywinpty")
        major = int(version.split(".", 1)[0])
    except (importlib.metadata.PackageNotFoundError, ValueError) as exc:
        raise RuntimeError("pywinpty version unavailable") from exc
    if major != 2:
        raise RuntimeError(f"unsupported pywinpty major version: {version}")
    spawn_parameters = tuple(inspect.signature(PtyProcess.spawn).parameters)
    if spawn_parameters != ("argv", "cwd", "env", "dimensions", "backend"):
        raise RuntimeError("unsupported pywinpty spawn signature")
    if not all(
        callable(getattr(PtyProcess, name, None))
        for name in ("spawn", "isalive", "terminate")
    ):
        raise RuntimeError("unsupported pywinpty process API")
    if not all(
        callable(getattr(PTY, name, None))
        for name in ("read", "write", "iseof", "isalive", "get_exitstatus")
    ):
        raise RuntimeError("unsupported pywinpty PTY API")

    class _RegistrarPtyProcess(PtyProcess):
        """Registrar-only transport; avoids pywinpty's process-global reader hook."""

        def __init__(self, pty: object) -> None:
            self.pty: Any = pty
            self.pid = pty.pid  # type: ignore[attr-defined]
            self.read_blocking = False
            self.closed = False
            self.flag_eof = False
            self.delayafterterminate = 0.1
            self.delayafterclose = 0.1
            self.fileobj, self._server = socket.socketpair()
            self.fd = self.fileobj.fileno()
            self._transport_stop = threading.Event()
            self._thread = threading.Thread(
                target=lambda: None,
                daemon=False,
                name="session-bridge-winpty-transport",
            )
            self._thread.start()

        def read_with_timeout(self, size: int, timeout: float) -> str | None:
            if self._transport_stop.is_set():
                raise EOFError("Pty is closed")
            data = self.pty.read(size, blocking=False)
            if data:
                return data if isinstance(data, str) else bytes(data).decode("utf-8", "replace")
            ready, _, _ = select.select(
                [self.fileobj], [], [], min(max(0.0, timeout), 0.01)
            )
            if ready:
                try:
                    self.fileobj.recv(1)
                except OSError:
                    pass
                if self._transport_stop.is_set():
                    raise EOFError("Pty is closed")
            return None

        def stop_transport(self) -> None:
            self._transport_stop.set()
            try:
                self._server.send(b"\0")
            except OSError:
                pass

        def release_native_pty(self) -> None:
            """Drop the last owned pseudoconsole handle after synchronous reads stop."""
            self.pty = None

    return _RegistrarPtyProcess


def _reclaim_unadapted_process(process: object, *, timeout: float) -> bool:
    try:
        alive = bool(process.isalive())  # type: ignore[attr-defined]
    except Exception:
        alive = True
    if alive:
        try:
            process.terminate(force=True)  # type: ignore[attr-defined]
        except Exception:
            pass
    deadline = time.monotonic() + timeout
    while alive and time.monotonic() < deadline:
        try:
            alive = bool(process.isalive())  # type: ignore[attr-defined]
        except Exception:
            break
        if alive:
            time.sleep(0.01)
    if alive:
        pid = getattr(process, "pid", None)
        if type(pid) is int:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=timeout,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        final_deadline = time.monotonic() + timeout
        while time.monotonic() < final_deadline:
            try:
                if not process.isalive():  # type: ignore[attr-defined]
                    alive = False
                    break
            except Exception:
                break
            time.sleep(0.01)
    stop_transport = getattr(process, "stop_transport", None)
    if callable(stop_transport):
        try:
            stop_transport()
        except Exception:
            pass
    for name in ("fileobj", "_server"):
        resource = getattr(process, name, None)
        try:
            shutdown = getattr(resource, "shutdown", None)
            if callable(shutdown):
                shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            close = getattr(resource, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
    try:
        setattr(process, "fd", -1)
        setattr(process, "closed", True)
    except Exception:
        pass
    reader = getattr(process, "_thread", None)
    if isinstance(reader, threading.Thread):
        reader.join(timeout)
    try:
        process_dead = not bool(process.isalive())  # type: ignore[attr-defined]
    except Exception:
        process_dead = False
    descriptors_closed = all(
        _fileno_closed(getattr(process, name, None))
        for name in ("fileobj", "_server")
    ) and getattr(process, "fd", None) == -1
    reader_stopped = not isinstance(reader, threading.Thread) or not reader.is_alive()
    if process_dead and reader_stopped:
        release_native_pty = getattr(process, "release_native_pty", None)
        if callable(release_native_pty):
            try:
                release_native_pty()
            except Exception:
                return False
    native_pty_released = not hasattr(process, "release_native_pty") or (
        getattr(process, "pty", object()) is None
    )
    return process_dead and descriptors_closed and reader_stopped and native_pty_released


class _WinPtyProcess:
    def __init__(
        self,
        process: object,
        *,
        require_supported_layout: bool = False,
        direct_native_pty: bool = False,
    ) -> None:
        self._process = process
        self._closed = False
        self._cleanup_result: PtyCleanupResult | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_result: queue.Queue[str | BaseException | None] | None = None
        self._reader_stop = threading.Event()
        self._close_lock = threading.Lock()
        self._direct_native_pty = direct_native_pty
        if require_supported_layout:
            self._resources()

    def _resources(self) -> tuple[object, object, threading.Thread]:
        fileobj = getattr(self._process, "fileobj", None)
        server = getattr(self._process, "_server", None)
        reader = getattr(self._process, "_thread", None)
        pty = getattr(self._process, "pty", None)
        direct_layout_supported = not self._direct_native_pty or all(
            callable(getattr(pty, name, None))
            for name in ("read", "write", "iseof", "get_exitstatus")
        )
        if not (
            callable(getattr(fileobj, "close", None))
            and callable(getattr(fileobj, "fileno", None))
            and callable(getattr(server, "close", None))
            and callable(getattr(server, "fileno", None))
            and isinstance(reader, threading.Thread)
            and hasattr(self._process, "fd")
            and hasattr(self._process, "closed")
            and callable(getattr(self._process, "isalive", None))
            and direct_layout_supported
        ):
            raise RuntimeError("unsupported pywinpty resource layout")
        return fileobj, server, reader

    def read_until(self, timeout: float, *, prompt: str | None = None) -> str:
        timed_read = getattr(self._process, "read_with_timeout", None)
        if callable(timed_read):
            return self._read_until_cancellable(timeout, prompt, timed_read)
        if self._reader_thread is not None:
            raise RuntimeError("PTY reader already started")
        result: queue.Queue[str | BaseException | None] = queue.Queue()
        self._reader_result = result

        def _read() -> None:
            try:
                while True:
                    if self._reader_stop.is_set():
                        return
                    if self._direct_native_pty:
                        pty = getattr(self._process, "pty")
                        chunk = pty.read(4096, blocking=False)
                        if not chunk and pty.iseof():
                            result.put(None)
                            return
                    else:
                        chunk = self._process.read(4096)  # type: ignore[attr-defined]
                    if not chunk:
                        if self._reader_stop.is_set():
                            return
                        time.sleep(0.01)
                        continue
                    text = chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else str(chunk)
                    result.put(text)
            except (EOFError, StopIteration):
                result.put(None)
            except BaseException as exc:
                result.put(exc)

        reader = threading.Thread(target=_read, daemon=True, name="session-bridge-winpty-reader")
        self._reader_thread = reader
        reader.start()
        deadline = time.monotonic() + timeout
        settle_deadline: float | None = None
        candidate_seen = False
        chunks: list[str] = []
        while True:
            now = time.monotonic()
            wake_at = deadline if settle_deadline is None else min(deadline, settle_deadline)
            remaining = wake_at - now
            if remaining <= 0:
                joined = "".join(chunks)
                if candidate_seen:
                    return self._finish_read(_normalized_terminal_output(joined, prompt))
                self._stop_reader()
                raise TimeoutError
            try:
                value = result.get(timeout=remaining)
            except queue.Empty:
                continue
            if value is None:
                joined = "".join(chunks)
                if candidate_seen:
                    return self._finish_read(_normalized_terminal_output(joined, prompt))
                return self._finish_read(_normalized_terminal_output(joined, prompt))
            if isinstance(value, BaseException):
                self._stop_reader()
                raise RuntimeError("PTY read unavailable") from value
            chunks.append(value)
            joined = "".join(chunks)
            if len(joined) > _MAX_RESPONSE_CHARS:
                return self._finish_read(joined)
            if not candidate_seen and _exact_registered_suffix(joined) is not None:
                candidate_seen = True
            if candidate_seen:
                settle_deadline = time.monotonic() + _RESPONSE_SETTLE_SECONDS

    def _read_until_cancellable(
        self,
        timeout: float,
        prompt: str | None,
        timed_read: Callable[[int, float], str | bytes | None],
    ) -> str:
        deadline = time.monotonic() + timeout
        settle_deadline: float | None = None
        candidate_seen = False
        chunks: list[str] = []
        while True:
            now = time.monotonic()
            wake_at = deadline if settle_deadline is None else min(deadline, settle_deadline)
            remaining = wake_at - now
            if remaining <= 0:
                joined = "".join(chunks)
                if candidate_seen:
                    return _normalized_terminal_output(joined, prompt)
                raise TimeoutError
            try:
                value = timed_read(4096, remaining)
            except EOFError:
                joined = "".join(chunks)
                return _normalized_terminal_output(joined, prompt)
            except Exception as exc:
                raise RuntimeError("PTY read unavailable") from exc
            if value is None:
                continue
            text = (
                value.decode("utf-8", "replace")
                if isinstance(value, bytes)
                else str(value)
            )
            chunks.append(text)
            joined = "".join(chunks)
            if len(joined) > _MAX_RESPONSE_CHARS:
                return joined
            if not candidate_seen and _exact_registered_suffix(joined) is not None:
                candidate_seen = True
            if candidate_seen:
                settle_deadline = time.monotonic() + _RESPONSE_SETTLE_SECONDS

    def _finish_read(self, value: str) -> str:
        self._stop_reader()
        return value

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        if self._direct_native_pty and self._reader_thread is not None:
            self._reader_thread.join(2.0)

    def write(self, data: str) -> None:
        if self._direct_native_pty:
            self._process.pty.write(data)  # type: ignore[attr-defined]
        else:
            self._process.write(data)  # type: ignore[attr-defined]

    def wait(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._process.isalive():  # type: ignore[attr-defined]
                value = getattr(self._process, "exitstatus", None)
                if value is None:
                    pty = getattr(self._process, "pty", None)
                    getter = getattr(pty, "get_exitstatus", None)
                    value = getter() if callable(getter) else None
                return value if type(value) is int else None
            time.sleep(0.01)
        raise TimeoutError

    def terminate(self, timeout: float = 1.0) -> bool:
        try:
            terminated = self._process.terminate(force=True)  # type: ignore[attr-defined]
        except Exception:
            terminated = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not self._process.isalive():  # type: ignore[attr-defined]
                    return terminated is not False
            except Exception:
                return False
            time.sleep(0.01)
        pid = getattr(self._process, "pid", None)
        if sys.platform.startswith("win") and type(pid) is int:
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    timeout=max(0.1, timeout),
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if completed.returncode == 0:
                final_deadline = time.monotonic() + timeout
                while time.monotonic() < final_deadline:
                    if self._is_dead():
                        return True
                    time.sleep(0.01)
        return False

    def close(self, timeout: float = 1.0) -> PtyCleanupResult:
        with self._close_lock:
            if self._cleanup_result is not None:
                return self._cleanup_result
            try:
                fileobj, server, native_reader = self._resources()
            except RuntimeError:
                result = PtyCleanupResult(False, False, False, self._exit_code())
                self._cleanup_result = result
                return result
            process_dead = self._is_dead()
            if not process_dead:
                process_dead = self.terminate(timeout)
            stop_transport = getattr(self._process, "stop_transport", None)
            if callable(stop_transport):
                try:
                    stop_transport()
                except Exception:
                    pass
            for resource in (fileobj, server):
                try:
                    shutdown = getattr(resource, "shutdown", None)
                    if callable(shutdown):
                        shutdown(2)
                except Exception:
                    pass
                try:
                    resource.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
            try:
                setattr(self._process, "fd", -1)
                setattr(self._process, "closed", True)
            except Exception:
                pass
            deadline = time.monotonic() + timeout
            for reader in (self._reader_thread, native_reader):
                if reader is not None and reader is not threading.current_thread():
                    reader.join(max(0.0, deadline - time.monotonic()))
            registrar_reader_stopped = (
                self._reader_thread is None or not self._reader_thread.is_alive()
            )
            transport_reader_stopped = not native_reader.is_alive()
            reader_stopped = registrar_reader_stopped and transport_reader_stopped
            exit_code = self._exit_code()
            release_native_pty = getattr(self._process, "release_native_pty", None)
            native_pty_released = not callable(release_native_pty)
            if process_dead and transport_reader_stopped and callable(release_native_pty):
                try:
                    release_native_pty()
                    native_pty_released = True
                except Exception:
                    native_pty_released = False
            descriptors_closed = (
                _fileno_closed(fileobj)
                and _fileno_closed(server)
                and getattr(self._process, "fd", None) == -1
                and native_pty_released
            )
            self._closed = True
            result = PtyCleanupResult(
                process_dead,
                reader_stopped,
                descriptors_closed,
                exit_code,
                registrar_reader_stopped=registrar_reader_stopped,
                transport_reader_stopped=transport_reader_stopped,
            )
            self._cleanup_result = result
            return result

    def _is_dead(self) -> bool:
        try:
            return not bool(self._process.isalive())  # type: ignore[attr-defined]
        except Exception:
            return False

    def _exit_code(self) -> int | None:
        value = getattr(self._process, "exitstatus", None)
        if value is None:
            getter = getattr(getattr(self._process, "pty", None), "get_exitstatus", None)
            try:
                value = getter() if callable(getter) else None
            except Exception:
                value = None
        return value if type(value) is int else None


class ClaudeNativeRegistrar:
    """Processes exactly one already-leased Claude visibility claim."""

    def __init__(
        self,
        store: ClaudeVisibilityStore,
        source_adapter: ClaudeReadableSource,
        *,
        marker_secret: bytes,
        pty_factory: InteractivePtyFactory | None = None,
        claude_command: Sequence[str] = ("claude",),
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        process_timeout: float = 120.0,
        exit_timeout: float = 5.0,
        discovery_timeout: float = 15.0,
        retry_delay: float = 30.0,
        poll_interval: float = 0.1,
    ) -> None:
        self._store = store
        self._source = source_adapter
        self._secret = marker_secret
        self._factory = pty_factory or WindowsConPtyFactory()
        self._command = list(claude_command)
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._process_timeout = process_timeout
        self._exit_timeout = exit_timeout
        self._discovery_timeout = discovery_timeout
        self._retry_delay = retry_delay
        self._poll_interval = poll_interval

    def process(self, claim: ClaudeVisibilityClaim) -> ClaudeRegistrarOutcome:
        if not claim.claimed:
            return ClaudeRegistrarOutcome(claim.status, claim.job_id, claim.reserved_claude_uuid)
        try:
            self._validate_claim_authority(claim)
        except ValueError:
            return ClaudeRegistrarOutcome(
                "failed", claim.job_id, claim.reserved_claude_uuid,
                "bridge_conflict", "claim authority conflict",
            )
        try:
            candidate, identity = self._materialize_claim(claim)
        except ValueError:
            return self._fail(claim, "bridge_conflict", "claim identity conflict")

        if claim.lease_kind == "reconciliation":
            return self._reconcile(claim, candidate, identity)
        return self._launch(claim, candidate, identity)

    def _materialize_claim(
        self, claim: ClaudeVisibilityClaim
    ) -> tuple[ClaudeVisibilityCandidate, ClaudeVisibilityIdentity]:
        self._validate_claim_authority(claim)
        required_text = (
            claim.job_id,
            claim.source_session_id,
            claim.reserved_claude_uuid,
            claim.native_name,
            claim.source_cwd,
            claim.signed_marker,
            claim.lease_digest,
        )
        if any(not isinstance(value, str) or not value for value in required_text):
            raise ValueError("incomplete claim")
        if (
            not isinstance(claim.attempt_ordinal, int)
            or isinstance(claim.attempt_ordinal, bool)
            or claim.attempt_ordinal < 0
        ):
            raise ValueError("invalid attempt ordinal")
        if claim.source_provider not in (Provider.CODEX, Provider.HERMES):
            raise ValueError("invalid provider")
        assert claim.job_id is not None
        assert claim.source_session_id is not None
        assert claim.reserved_claude_uuid is not None
        assert claim.native_name is not None
        assert claim.source_cwd is not None
        assert claim.signed_marker is not None
        candidate = ClaudeVisibilityCandidate(
            source_session_id=claim.source_session_id,
            source_provider=claim.source_provider,
            native_name=claim.native_name,
            source_cwd=claim.source_cwd,
            git_root=claim.git_root,
            git_branch=claim.git_branch,
            git_head=claim.git_head,
            worktree_id=claim.worktree_id,
            eligible_at=0.0,
        )
        derived = derive_claude_visibility_identity(candidate, self._secret)
        identity = ClaudeVisibilityIdentity(
            job_id=claim.job_id,
            bridge_id=derived.bridge_id,
            idempotency_key=derived.idempotency_key,
            claude_uuid=claim.reserved_claude_uuid,
            signed_marker=claim.signed_marker,
        )
        validate_claude_visibility_identity_binding(candidate, identity, self._secret)
        return candidate, identity

    @staticmethod
    def _validate_claim_authority(claim: ClaudeVisibilityClaim) -> None:
        authority = (
            claim.lease_kind,
            claim.launch_permitted,
            claim.registration_reserved,
            claim.requires_exact_id_reconciliation,
        )
        if any(type(flag) is not bool for flag in authority[1:]) or authority not in {
            ("launch", True, True, False),
            ("reconciliation", False, False, True),
        }:
            raise ValueError("inconsistent reconciliation authority")

    def _reconcile(
        self, claim: ClaudeVisibilityClaim, candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
    ) -> ClaudeRegistrarOutcome:
        try:
            found = self._read_exact(identity.claude_uuid)
        except _TranscriptConflict as exc:
            return self._fail(claim, exc.code, "exact transcript identity conflict")
        except ValueError:
            return self._fail(claim, "uuid_conflict", "exact transcript identity conflict")
        except (OSError, RuntimeError):
            return self._retry(claim, "native_transcript_not_indexed", "exact transcript lookup unavailable")
        if found is None:
            evidence = hashlib.sha256(
                f"absent:{identity.claude_uuid}:{claim.attempt_ordinal}".encode()
            ).hexdigest()
            try:
                self._store.record_claude_visibility_exact_id_absent(
                    claim.job_id or "", claim.lease_digest or "", identity.claude_uuid,
                    claim.attempt_ordinal or 0, evidence,
                )
            except Exception:
                return ClaudeRegistrarOutcome("retry", claim.job_id, identity.claude_uuid,
                                               "session_bridge_unavailable", "store transition unavailable")
            return ClaudeRegistrarOutcome("absent", claim.job_id, identity.claude_uuid)
        return self._validate_and_commit(claim, candidate, identity, found)

    def _launch(
        self, claim: ClaudeVisibilityClaim, candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity,
    ) -> ClaudeRegistrarOutcome:
        try:
            existing = self._read_exact(identity.claude_uuid)
        except _TranscriptConflict as exc:
            return self._fail(claim, exc.code, "exact transcript identity conflict")
        except ValueError:
            return self._fail(
                claim, "bridge_conflict", "exact transcript identity conflict"
            )
        except (OSError, RuntimeError):
            return self._retry(
                claim, "native_transcript_not_indexed", "exact transcript lookup unavailable"
            )
        if existing is not None:
            return self._validate_and_commit(claim, candidate, identity, existing)

        argv = [*self._command, "--session-id", identity.claude_uuid,
                "--name", candidate.native_name, "--model", "haiku", "--tools", "",
                "--permission-mode", "dontAsk"]
        process: InteractivePty | None = None
        launched = False
        clean_exit = False
        pending: tuple[str, str, str] | None = None
        try:
            process = self._factory.spawn(argv, cwd=candidate.source_cwd)
            launched = True
            prompt = build_claude_registration_prompt(candidate, identity, self._secret)
            process.write(f"\x1b[200~{prompt}\x1b[201~\r")
            output = process.read_until(self._process_timeout, prompt=prompt)
            if _is_authentication_failure(output):
                pending = ("retry", "claude_authentication_unavailable", "Claude authentication unavailable")
            elif not _has_exact_registered_response(output, prompt):
                pending = ("fail", "bridge_conflict", "registration response malformed")
            else:
                process.write("/exit\r")
                exit_code = process.wait(self._exit_timeout)
                if type(exit_code) is not int or exit_code != 0:
                    pending = ("retry", "clean_exit_not_observed", "Claude did not exit cleanly")
                else:
                    clean_exit = True
        except FileNotFoundError:
            pending = ("retry", "claude_executable_unavailable", "Claude executable unavailable")
        except TimeoutError:
            pending = ("retry", "creation_ambiguous", "registration result ambiguous")
        except RuntimeError:
            code = "creation_ambiguous" if launched else "pty_unavailable"
            pending = ("retry", code, "interactive PTY unavailable")
        except Exception:
            code = "creation_ambiguous" if launched else "pty_unavailable"
            pending = ("retry", code, "interactive registration unavailable")
        finally:
            if process is not None:
                if not clean_exit:
                    try:
                        terminated = process.terminate(self._exit_timeout)
                    except Exception:
                        terminated = False
                    if not terminated:
                        pending = (
                            "retry", "creation_ambiguous",
                            "PTY termination was not confirmed",
                        )
                try:
                    cleanup = process.close(self._exit_timeout)
                except Exception:
                    cleanup = PtyCleanupResult(False, False, False, None)
                if not cleanup.succeeded:
                    pending = (
                        "retry", "creation_ambiguous",
                        "PTY cleanup postconditions failed",
                    )

        if pending is not None:
            transition, code, detail = pending
            if transition == "fail":
                return self._fail(claim, code, detail)
            return self._retry(claim, code, detail)

        deadline = self._monotonic() + self._discovery_timeout
        while True:
            try:
                found = self._read_exact(identity.claude_uuid)
            except _TranscriptConflict as exc:
                return self._fail(claim, exc.code, "exact transcript identity conflict")
            except ValueError:
                return self._fail(
                    claim, "bridge_conflict", "exact transcript identity conflict"
                )
            except (OSError, RuntimeError):
                found = None
            if found is not None:
                return self._validate_and_commit(claim, candidate, identity, found)
            if self._monotonic() >= deadline:
                return self._retry(claim, "native_transcript_not_indexed", "native transcript not indexed")
            self._sleep(self._poll_interval)

    def _read_exact(self, native_id: str) -> _ExactTranscript | None:
        finder = getattr(self._source, "find_native_sessions", None)
        if callable(finder):
            paths = list(finder(native_id))
        else:
            found = self._source.find_native_session(native_id)
            paths = [] if found is None else [found]
        if len(paths) > 1:
            raise _TranscriptConflict("duplicate_uuid")
        if not paths:
            return None
        exact_path = Path(paths[0])
        parsed: ClaudeParseResult = self._source.parse(exact_path)
        return _ExactTranscript(path=exact_path, parsed=parsed)

    def _validate_and_commit(
        self, claim: ClaudeVisibilityClaim, candidate: ClaudeVisibilityCandidate,
        identity: ClaudeVisibilityIdentity, transcript: _ExactTranscript,
    ) -> ClaudeRegistrarOutcome:
        try:
            _validate_projection(transcript, candidate, identity, self._secret)
        except _TranscriptConflict as exc:
            return self._fail(claim, exc.code, "exact transcript conflict")
        projection = transcript.projection
        digest = projection.native_hash
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            digest = hashlib.sha256(json.dumps({
                "native_id": projection.native_id, "native_path": projection.native_path,
                "last_active": projection.last_active,
            }, sort_keys=True).encode()).hexdigest()
        try:
            self._store.commit_claude_visibility_job(
                claim.job_id or "", claim.lease_digest or "", digest, self._clock()
            )
        except Exception:
            return ClaudeRegistrarOutcome("retry", claim.job_id, identity.claude_uuid,
                                           "session_bridge_unavailable", "store transition unavailable")
        return ClaudeRegistrarOutcome("visible", claim.job_id, identity.claude_uuid)

    def _retry(self, claim: ClaudeVisibilityClaim, code: str, detail: str) -> ClaudeRegistrarOutcome:
        try:
            self._store.retry_claude_visibility_job(
                claim.job_id or "", claim.lease_digest or "", code,
                self._clock() + self._retry_delay, detail,
            )
        except Exception:
            code, detail = "session_bridge_unavailable", "store transition unavailable"
        return ClaudeRegistrarOutcome("retry", claim.job_id, claim.reserved_claude_uuid, code, detail)

    def _fail(self, claim: ClaudeVisibilityClaim, code: str, detail: str) -> ClaudeRegistrarOutcome:
        try:
            self._store.fail_claude_visibility_job(
                claim.job_id or "", claim.lease_digest or "", code, detail
            )
        except Exception:
            code, detail = "session_bridge_unavailable", "store transition unavailable"
        return ClaudeRegistrarOutcome("failed", claim.job_id, claim.reserved_claude_uuid, code, detail)


def _validate_projection(
    transcript: _ExactTranscript, candidate: ClaudeVisibilityCandidate,
    identity: ClaudeVisibilityIdentity, marker_secret: bytes,
) -> None:
    projection = transcript.projection
    if transcript.parsed.malformed_lines or transcript.parsed.unknown_records:
        raise _TranscriptConflict("bridge_conflict")
    if projection.provider is not Provider.CLAUDE or projection.native_id != identity.claude_uuid:
        raise _TranscriptConflict("uuid_conflict")
    if transcript.path.parent.name != claude_project_directory_name(candidate.source_cwd):
        raise _TranscriptConflict("cwd_conflict")
    if projection.cwd != candidate.source_cwd:
        raise _TranscriptConflict("cwd_conflict")
    if projection.title != candidate.native_name:
        raise _TranscriptConflict("name_conflict")
    if projection.origin_bridge_id != identity.bridge_id or projection.origin_kind is not OriginKind.BRIDGE_PLACEHOLDER:
        raise _TranscriptConflict("bridge_conflict")
    expected = build_claude_registration_prompt(candidate, identity, marker_secret)
    messages = list(projection.messages)
    prompt_indexes = [index for index, message in enumerate(messages) if message.role == "user" and message.content == expected]
    if len(prompt_indexes) != 1:
        raise _TranscriptConflict("marker_conflict")
    if prompt_indexes != [0]:
        raise _TranscriptConflict("bridge_conflict")
    prompt = messages[0]
    if (
        prompt.ordinal != 0
        or prompt.tool_calls
        or prompt.tool_name
        or prompt.tool_call_id
        or prompt.reasoning
    ):
        raise _TranscriptConflict("bridge_conflict")
    if len(messages) < 2:
        raise _TranscriptConflict("bridge_conflict")
    response = messages[1]
    if response.role != "assistant":
        raise _TranscriptConflict("bridge_conflict")
    turn_messages = messages[1:]
    response_event_id = response.native_event_id
    for message in turn_messages:
        if (
            message.role != "assistant"
            or message.native_event_id != response_event_id
            or message.tool_calls
            or message.tool_name
            or message.tool_call_id
            or message.reasoning
        ):
            raise _TranscriptConflict("bridge_conflict")
    if [message.ordinal for message in turn_messages] != list(range(len(turn_messages))):
        raise _TranscriptConflict("bridge_conflict")
    aggregate = "".join(
        message.content
        for message in turn_messages
        if isinstance(message.content, str)
    )
    if not _is_exact_registered_text(aggregate):
        raise _TranscriptConflict("bridge_conflict")


def _is_exact_registered_text(content: object) -> bool:
    if not isinstance(content, str):
        return False
    cleaned = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", content)).replace("\r", "")
    return cleaned.strip() == "REGISTERED"


def _is_authentication_failure(output: str) -> bool:
    folded = output.casefold()
    return "authentication required" in folded or "not authenticated" in folded or "please log in" in folded


def _fileno_closed(resource: object) -> bool:
    try:
        return int(resource.fileno()) < 0  # type: ignore[attr-defined]
    except Exception:
        return False


def _normalized_terminal_output(output: str, prompt: str | None) -> str:
    """Remove only recognized terminal echo/UI framing, preserving other output."""

    cleaned = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", output)).replace("\r", "")
    if prompt is not None:
        for exact_echo in (prompt, prompt.replace("\n", "")):
            if exact_echo in cleaned:
                cleaned = cleaned.replace(exact_echo, "", 1)
    prompt_lines = (
        {line.strip() for line in prompt.splitlines()} if prompt is not None else set()
    )
    meaningful: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip()
        for prefix in ("Claude>", ">"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if not line or line in prompt_lines:
            continue
        if (
            line == "Claude Code ready"
            or line == "status: connected"
            or line == "metadata continuation"
            or line.startswith("Signed marker: ")
        ):
            continue
        meaningful.append(line)
    return "\n".join(meaningful) + ("\n" if meaningful else "")


def _has_exact_registered_response(output: str, prompt: str) -> bool:
    if not isinstance(output, str) or len(output) > _MAX_RESPONSE_CHARS:
        return False
    cleaned = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", output)).replace("\r", "")
    prompt_lines = {line.strip() for line in prompt.splitlines()}
    meaningful: list[str] = []
    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line or line in prompt_lines:
            continue
        for prefix in ("Claude>", ">"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if line in prompt_lines:
            continue
        if line:
            meaningful.append(line)
    return meaningful == ["REGISTERED"]


def _exact_registered_suffix(output: str) -> str | None:
    return _registered_suffix(output, require_complete=True)


def _registered_suffix(output: str, *, require_complete: bool) -> str | None:
    if require_complete and not output.endswith(("\r", "\n")):
        return None
    cleaned = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", output)).replace("\r", "")
    lines = cleaned.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        for prefix in ("Claude>", ">"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if line == "REGISTERED":
            suffix = ["REGISTERED"]
            suffix.extend(
                remainder.strip()
                for remainder in lines[index + 1:]
                if remainder.strip()
            )
            return "\n".join(suffix) + "\n"
    return None


__all__ = [
    "ClaudeNativeRegistrar", "ClaudeRegistrarOutcome", "InteractivePty",
    "InteractivePtyFactory", "WindowsConPtyFactory",
    "PtyCleanupResult",
]
