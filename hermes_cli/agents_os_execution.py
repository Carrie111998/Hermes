"""Fail-closed process adapters for Agents OS runtimes.

This module deliberately separates command construction from process execution so
callers can inspect a redacted command before authorising a run.  Probing only
checks the local executable search path; it never starts an agent.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence


class RuntimeExecutionError(RuntimeError):
    """Base error for adapter validation and execution failures."""


class RuntimeUnavailableError(RuntimeExecutionError):
    """Raised when a runtime is intentionally unavailable or not installed."""


class UnsafeWorkingDirectoryError(RuntimeExecutionError):
    """Raised when a requested cwd is not an exact allowlisted directory."""


@dataclass(frozen=True)
class ProbeResult:
    runtime: str
    available: bool
    executable: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RuntimeInvocation:
    runtime: str
    argv: tuple[str, ...]
    cwd: Path
    stdin: bytes
    evidence_argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionResult:
    runtime: str
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_seconds: float
    timed_out: bool
    cancelled: bool
    stdout_truncated: bool
    stderr_truncated: bool
    command_evidence: tuple[str, ...]

    @property
    def succeeded(self) -> bool:
        return not self.timed_out and not self.cancelled and self.returncode == 0


class CancelToken:
    """Thread-safe cooperative cancellation token."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class RuntimeAdapter(Protocol):
    name: str

    def probe(self) -> ProbeResult: ...

    def build_invocation(self, *, prompt: str, cwd: Path, **options: object) -> RuntimeInvocation: ...


def _resolve_allowed_cwd(cwd: Path | str, allowed_cwds: Iterable[Path | str]) -> Path:
    requested = Path(cwd)
    if not requested.is_absolute():
        raise UnsafeWorkingDirectoryError("runtime cwd must be absolute")
    try:
        resolved = requested.resolve(strict=True)
    except OSError as exc:
        raise UnsafeWorkingDirectoryError(f"runtime cwd is not accessible: {requested}") from exc
    if not resolved.is_dir():
        raise UnsafeWorkingDirectoryError(f"runtime cwd is not a directory: {resolved}")

    allowed: set[Path] = set()
    for item in allowed_cwds:
        candidate = Path(item)
        if not candidate.is_absolute():
            raise UnsafeWorkingDirectoryError("allowlisted cwd entries must be absolute")
        try:
            allowed.add(candidate.resolve(strict=True))
        except OSError as exc:
            raise UnsafeWorkingDirectoryError(f"allowlisted cwd is not accessible: {candidate}") from exc
    if resolved not in allowed:
        raise UnsafeWorkingDirectoryError(f"runtime cwd is not exactly allowlisted: {resolved}")
    return resolved


class _BaseAdapter:
    name = ""

    def __init__(self, executable: str, allowed_cwds: Iterable[Path | str]) -> None:
        self.executable = executable
        self.allowed_cwds = tuple(allowed_cwds)

    def probe(self) -> ProbeResult:
        found = shutil.which(self.executable)
        if found is None:
            return ProbeResult(self.name, False, reason="executable not found on PATH")
        return ProbeResult(self.name, True, executable=str(Path(found).resolve()))

    def _cwd(self, cwd: Path | str) -> Path:
        return _resolve_allowed_cwd(cwd, self.allowed_cwds)


class HermesAdapter(_BaseAdapter):
    name = "hermes"

    def build_invocation(self, *, prompt: str, cwd: Path, **options: object) -> RuntimeInvocation:
        resolved = self._cwd(cwd)
        max_turns = options.get("max_turns", 8)
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not (1 <= max_turns <= 90):
            raise RuntimeExecutionError("Hermes max_turns must be between 1 and 90")
        toolsets = options.get("toolsets", ("clarify",))
        if not isinstance(toolsets, (list, tuple)) or not toolsets or any(
            not isinstance(item, str) or item not in {"clarify", "memory", "session_search"}
            for item in toolsets
        ):
            raise RuntimeExecutionError("Hermes toolsets must use the safe conversational allowlist")
        toolset_arg = ",".join(toolsets)
        # Verified 0.18.2 programmatic contract. Never use root -z/--oneshot,
        # --yolo or any approval-bypass spelling. An explicit conversational
        # toolset prevents a headless run from waiting on terminal/file approval.
        argv = (self.executable, "chat", "-q", prompt, "-Q", "--source", "agents-os",
                "--toolsets", toolset_arg, "--max-turns", str(max_turns))
        evidence = (self.executable, "chat", "-q", "<redacted>", "-Q", "--source", "agents-os",
                    "--toolsets", toolset_arg, "--max-turns", str(max_turns))
        return RuntimeInvocation(self.name, argv, resolved, b"", evidence)


class CodexAdapter(_BaseAdapter):
    name = "codex"

    def build_invocation(self, *, prompt: str, cwd: Path, **options: object) -> RuntimeInvocation:
        resolved = self._cwd(cwd)
        output_value = options.get("output_last_message")
        if output_value is None:
            raise RuntimeExecutionError("codex requires output_last_message")
        output = Path(str(output_value))
        if not output.is_absolute():
            raise RuntimeExecutionError("output_last_message must be absolute")
        output = output.resolve(strict=False)
        if output.parent != resolved:
            raise RuntimeExecutionError("output_last_message must be directly inside cwd")
        argv = (
            self.executable, "exec", "--json", "--ephemeral", "--color", "never",
            "-C", str(resolved), "--output-last-message", str(output), "-",
        )
        return RuntimeInvocation(self.name, argv, resolved, prompt.encode("utf-8"), argv)


class ClaudeAdapter(_BaseAdapter):
    name = "claude"

    def build_invocation(self, *, prompt: str, cwd: Path, **options: object) -> RuntimeInvocation:
        resolved = self._cwd(cwd)
        budget = options.get("max_budget_usd", 1.0)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not (0 < float(budget) <= 100):
            raise RuntimeExecutionError("max_budget_usd must be greater than 0 and at most 100")
        tools_value = options.get("allowed_tools", ())
        if isinstance(tools_value, (str, bytes)):
            raise RuntimeExecutionError("allowed_tools must be a sequence, not a string")
        tools = tuple(str(tool) for tool in tools_value)  # type: ignore[arg-type]
        if any(not tool or any(char in tool for char in "\r\n\x00") for tool in tools):
            raise RuntimeExecutionError("allowed_tools contains an invalid tool name")
        permission_mode = str(options.get("permission_mode", "dontAsk"))
        if permission_mode not in {"dontAsk", "plan"}:
            raise RuntimeExecutionError("only dontAsk or plan permission modes are allowed")
        argv = (
            self.executable, "-p", "--input-format", "text", "--output-format", "stream-json",
            "--permission-mode", permission_mode, "--tools", ",".join(tools),
            "--max-budget-usd", str(float(budget)), "--no-session-persistence",
        )
        return RuntimeInvocation(self.name, argv, resolved, prompt.encode("utf-8"), argv)


class OpenClawAdapter(_BaseAdapter):
    name = "openclaw"

    def build_invocation(self, *, prompt: str, cwd: Path, **options: object) -> RuntimeInvocation:
        resolved = self._cwd(cwd)
        agent_id = options.get("agent_id", "main")
        timeout_seconds = options.get("timeout_seconds", 600)
        if not isinstance(agent_id, str) or not agent_id or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for char in agent_id
        ):
            raise RuntimeExecutionError("OpenClaw agent_id contains unsupported characters")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not (1 <= timeout_seconds <= 900)
        ):
            raise RuntimeExecutionError("OpenClaw timeout_seconds must be between 1 and 900")

        # Verified against the locally installed 2026.5.22 CLI reference.  Omitting
        # --deliver is intentional: an Agents OS run must never emit a channel
        # callback. JSON reserves stdout for a machine-readable result.
        argv = (
            self.executable,
            "agent",
            "--agent",
            agent_id,
            "--message",
            prompt,
            "--timeout",
            str(timeout_seconds),
            "--json",
        )
        evidence = (
            self.executable,
            "agent",
            "--agent",
            agent_id,
            "--message",
            "<redacted>",
            "--timeout",
            str(timeout_seconds),
            "--json",
        )
        return RuntimeInvocation(self.name, argv, resolved, b"", evidence)


class RuntimeAdapterRegistry:
    def __init__(self, adapters: Iterable[RuntimeAdapter] = ()) -> None:
        self._adapters: dict[str, RuntimeAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: RuntimeAdapter) -> None:
        if not adapter.name or adapter.name in self._adapters:
            raise RuntimeExecutionError(f"invalid or duplicate runtime adapter: {adapter.name!r}")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> RuntimeAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise RuntimeUnavailableError(f"unknown runtime: {name}") from exc

    def probe_all(self) -> dict[str, ProbeResult]:
        return {name: adapter.probe() for name, adapter in self._adapters.items()}


def default_registry(*, allowed_cwds: Iterable[Path | str]) -> RuntimeAdapterRegistry:
    allowed = tuple(allowed_cwds)

    def executable(name: str) -> str:
        override = os.environ.get(f"AGENTS_OS_{name.upper()}_EXECUTABLE")
        if override:
            return override
        for candidate in (Path.home() / ".local" / "bin" / name,
                          Path.home() / ".npm-global" / "bin" / name):
            if candidate.is_file():
                return str(candidate)
        return name

    return RuntimeAdapterRegistry((
        HermesAdapter(executable("hermes"), allowed),
        CodexAdapter(executable("codex"), allowed),
        ClaudeAdapter(executable("claude"), allowed),
        OpenClawAdapter(executable("openclaw"), allowed),
    ))


class _BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: object) -> None:
        read = getattr(stream, "read")
        while True:
            chunk = read(8192)
            if not chunk:
                return
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            remaining = self.limit - len(self.data)
            if remaining > 0:
                self.data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True


def _default_terminate_tree(proc: subprocess.Popen[bytes], *, force: bool) -> None:
    if os.name == "nt":
        args = ["taskkill", "/PID", str(proc.pid), "/T"]
        if force:
            args.append("/F")
        subprocess.run(args, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=False)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass


ProcessFactory = Callable[..., subprocess.Popen[bytes]]
TreeTerminator = Callable[[subprocess.Popen[bytes]], None]


def execute_invocation(
    invocation: RuntimeInvocation,
    *,
    timeout_seconds: float,
    cancel_token: CancelToken | None = None,
    max_stdout_bytes: int = 1_000_000,
    max_stderr_bytes: int = 250_000,
    process_factory: ProcessFactory = subprocess.Popen,
    terminate_tree: TreeTerminator | None = None,
    kill_tree: TreeTerminator | None = None,
) -> ExecutionResult:
    if timeout_seconds <= 0:
        raise RuntimeExecutionError("timeout_seconds must be positive")
    if max_stdout_bytes < 0 or max_stderr_bytes < 0:
        raise RuntimeExecutionError("output limits cannot be negative")
    started = time.monotonic()
    popen_options: dict[str, object] = {
        "cwd": str(invocation.cwd), "env": {**os.environ, **invocation.environment},
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    proc = process_factory(list(invocation.argv), **popen_options)
    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        proc.kill()
        raise RuntimeExecutionError("runtime process did not provide required pipes")

    stdout_capture = _BoundedCapture(max_stdout_bytes)
    stderr_capture = _BoundedCapture(max_stderr_bytes)
    readers = (
        threading.Thread(target=stdout_capture.drain, args=(proc.stdout,), daemon=True),
        threading.Thread(target=stderr_capture.drain, args=(proc.stderr,), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        if invocation.stdin:
            proc.stdin.write(invocation.stdin)
            proc.stdin.flush()
    finally:
        proc.stdin.close()

    deadline = started + timeout_seconds
    timed_out = False
    cancelled = False
    while proc.poll() is None:
        if cancel_token is not None and cancel_token.cancelled:
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)

    if timed_out or cancelled:
        graceful = terminate_tree or (lambda child: _default_terminate_tree(child, force=False))
        forceful = kill_tree or (lambda child: _default_terminate_tree(child, force=True))
        graceful(proc)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            forceful(proc)
            proc.wait(timeout=2.0)
    else:
        proc.wait()
    for reader in readers:
        reader.join(timeout=2.0)
    return ExecutionResult(
        invocation.runtime, int(proc.returncode), bytes(stdout_capture.data), bytes(stderr_capture.data),
        time.monotonic() - started, timed_out, cancelled, stdout_capture.truncated,
        stderr_capture.truncated, invocation.evidence_argv,
    )


__all__ = [
    "CancelToken", "ClaudeAdapter", "CodexAdapter", "ExecutionResult", "HermesAdapter",
    "OpenClawAdapter", "ProbeResult", "RuntimeAdapterRegistry", "RuntimeExecutionError",
    "RuntimeInvocation", "RuntimeUnavailableError", "UnsafeWorkingDirectoryError",
    "default_registry", "execute_invocation",
]
