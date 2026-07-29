"""Codex app-server JSON-RPC client.

Speaks the protocol documented in codex-rs/app-server/README.md (codex 0.125+).
Transport is newline-delimited JSON-RPC 2.0 over stdio: spawn `codex app-server`,
do an `initialize` handshake, then drive `thread/start` + `turn/start` and
consume streaming `item/*` notifications until `turn/completed`.

This module is the wire-level speaker only. Higher-level concerns (event
projection into Hermes' display, approval bridging, transcript projection into
AIAgent.messages, plugin migration) live in sibling modules.

Status: optional opt-in runtime gated behind `model.openai_runtime ==
"codex_app_server"`. Hermes' default tool dispatch is unchanged when this
runtime is not selected.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hermes_cli._subprocess_compat import windows_hide_flags
from tools.environments.local import hermes_subprocess_env

# Default minimum codex version we test against. The PR sets this from the
# `codex --version` parsed at install time; bumping is a one-line change here.
MIN_CODEX_VERSION = (0, 125, 0)
_READ_ONLY_WORKSPACE_CLASSES = frozenset(
    {"isolated-read-only-worktree", "shared-read-only"}
)


def _kanban_workspace_class(spawn_env: dict[str, str]) -> Optional[str]:
    """Read the admitted workspace class from the claimed task's board DB.

    The class is governance data, so it must come from the durable task
    contract rather than a profile-controlled environment variable.  A
    governed Kwilo task fails closed if its semantic row cannot be read.
    Non-governed boards retain the legacy workspace-write behaviour.
    """
    task_id = spawn_env.get("HERMES_KANBAN_TASK")
    db_value = spawn_env.get("HERMES_KANBAN_DB")
    if not task_id or not db_value:
        return None

    db_path = Path(db_value).expanduser()
    if not db_path.is_file():
        if spawn_env.get("HERMES_KANBAN_BOARD") == "kwilo":
            raise RuntimeError(f"governed Kanban database is unavailable: {db_path}")
        return None

    try:
        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT workspace_class FROM task_semantics WHERE task_id = ?",
                (task_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        if spawn_env.get("HERMES_KANBAN_BOARD") == "kwilo":
            raise RuntimeError(
                f"cannot resolve governed workspace class for {task_id}: {exc}"
            ) from exc
        return None

    if row is None:
        if spawn_env.get("HERMES_KANBAN_BOARD") == "kwilo":
            raise RuntimeError(
                f"governed task {task_id} has no admitted workspace class"
            )
        return None
    value = str(row[0] or "").strip()
    if not value:
        raise RuntimeError(f"task {task_id} has an empty admitted workspace class")
    return value


@dataclass
class CodexAppServerError(RuntimeError):
    """Raised on JSON-RPC errors from the app-server."""

    code: int
    message: str
    data: Optional[Any] = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"codex app-server error {self.code}: {self.message}"


@dataclass
class _Pending:
    queue: queue.Queue
    method: str
    sent_at: float = field(default_factory=time.time)


def _hermes_runtime_python(spawn_env: dict[str, str]) -> str:
    """Return the interpreter that owns the installed Hermes environment.

    Windows launchers can run the gateway with a base ``pythonw.exe`` while
    exposing the Hermes venv through ``VIRTUAL_ENV`` and ``PYTHONPATH``.
    ``sys.executable`` is therefore not always the interpreter that owns the
    dependencies used by task-scoped child processes.
    """
    configured = spawn_env.get("HERMES_PYTHON", "").strip()
    if configured and Path(configured).is_file():
        return configured
    venv = spawn_env.get("VIRTUAL_ENV", "").strip()
    if venv:
        candidate = (
            Path(venv) / "Scripts" / "python.exe"
            if os.name == "nt"
            else Path(venv) / "bin" / "python"
        )
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _ensure_codex_mcp_runtime(
    python_executable: str,
    *,
    source_root: Path,
    spawn_env: dict[str, str],
) -> None:
    """Install/check lifecycle dependencies in the exact MCP interpreter."""
    try:
        same_interpreter = Path(python_executable).resolve() == Path(
            sys.executable
        ).resolve()
    except OSError:
        same_interpreter = python_executable == sys.executable
    if same_interpreter:
        from tools.lazy_deps import ensure

        ensure("tool.codex_app_server", prompt=False)
        return

    env = spawn_env.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(source_root), existing_pythonpath) if value
    )
    result = subprocess.run(
        [
            python_executable,
            "-c",
            (
                "from tools.lazy_deps import ensure; "
                "ensure('tool.codex_app_server', prompt=False)"
            ),
        ],
        cwd=str(source_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
        creationflags=windows_hide_flags(),
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise RuntimeError(
            "failed to prepare the Hermes lifecycle MCP interpreter "
            f"{python_executable}: {detail[-2000:]}"
        )


class CodexAppServerClient:
    """Minimal JSON-RPC 2.0 client for `codex app-server` over stdio.

    Threading model:
      - Spawning thread (caller) drives request/response pairs synchronously.
      - One reader thread parses stdout, dispatches replies to the right
        pending future, and routes notifications + server-initiated requests
        to bounded queues that the caller drains on their own cadence.
      - One reader thread captures stderr for diagnostics; codex emits
        tracing logs there at RUST_LOG-controlled levels.

    Intentionally NOT async. AIAgent.run_conversation() is synchronous and
    runs on the main thread; layering asyncio just to drive a stdio child
    creates surprising interrupt semantics. We use blocking queues with
    timeouts and rely on `turn/interrupt` for cancellation.
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        codex_home: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self._codex_bin = codex_bin
        self._cwd = cwd
        # codex app-server is a model-driving CLI executor: it runs a
        # model-chosen agentic loop that executes shell commands, so it
        # legitimately needs LLM provider credentials (inherit_credentials=True)
        # to authenticate against the model endpoint. But the previous
        # `os.environ.copy()` also handed it every Tier-1 Hermes secret — gateway
        # bot tokens, GitHub auth, Modal/Daytona infra tokens, the dashboard
        # session token, AUXILIARY_* side-LLM keys, GATEWAY_RELAY_* auth — none
        # of which a coding subprocess has any use for. Route through the
        # centralized helper so Tier-1 + dynamic-internal secrets are always
        # stripped while provider creds still flow, matching copilot_acp_client
        # (#29157 sibling spawn-site gap).
        spawn_env = hermes_subprocess_env(inherit_credentials=True)
        if env:
            spawn_env.update(env)
        if codex_home:
            spawn_env["CODEX_HOME"] = codex_home

        app_server_args = list(extra_args or [])
        # Kanban workers must be able to write their handoff/status back to
        # the board DB, which lives outside the per-task workspace. Keep the
        # Codex sandbox on. Implementation workspaces get only the board root
        # as an extra writable root; governed review workspaces are OS-level
        # read-only and update the board only through the isolated MCP bridge.
        if spawn_env.get("HERMES_KANBAN_TASK"):
            source_root = Path(__file__).resolve().parents[2]
            runtime_env = os.environ.copy()
            runtime_env.update(spawn_env)
            mcp_python = _hermes_runtime_python(runtime_env)
            # The lifecycle MCP child is launched with this exact interpreter.
            # Ensure its optional dependencies there before spawning Codex; a
            # base launcher or second sibling venv must never mask a broken
            # worker environment.
            _ensure_codex_mcp_runtime(
                mcp_python,
                source_root=source_root,
                spawn_env=spawn_env,
            )
            workspace_class = _kanban_workspace_class(spawn_env)
            read_only_workspace = workspace_class in _READ_ONLY_WORKSPACE_CLASSES
            kanban_db = spawn_env.get("HERMES_KANBAN_DB")
            kanban_root = (
                os.path.dirname(kanban_db)
                if kanban_db
                else spawn_env.get(
                    "HERMES_KANBAN_ROOT",
                    os.path.join(
                        spawn_env.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
                        "kanban",
                    ),
                )
            )
            # TOML basic strings treat backslashes as escapes. Passing a
            # native Windows path therefore makes Codex reject the list
            # override and fall back to treating it as one raw string. Use
            # forward slashes, which Windows accepts and TOML parses
            # consistently.
            writable_roots = json.dumps([kanban_root.replace("\\", "/")])
            mcp_python = mcp_python.replace("\\", "/")
            # Codex intentionally starts stdio MCP servers with a constrained
            # environment. Forward only the non-secret task identity needed by
            # the lifecycle handlers; relying on ambient inheritance made the
            # MCP server believe it was an unrestricted orchestrator.
            mcp_env_args: list[str] = []
            for env_name in (
                "HERMES_HOME",
                "HERMES_KANBAN_TASK",
                "HERMES_KANBAN_RUN_ID",
                "HERMES_KANBAN_DB",
                "HERMES_KANBAN_WORKSPACE",
                "HERMES_KANBAN_BOARD",
                "HERMES_PROFILE",
                "HERMES_TENANT",
            ):
                env_value = spawn_env.get(env_name)
                if env_value:
                    mcp_env_args.extend(
                        [
                            "-c",
                            (
                                f"mcp_servers.hermes-tools.env.{env_name}="
                                f"{json.dumps(env_value)}"
                            ),
                        ]
                    )
            # The app-server itself runs in the assigned task workspace so
            # Codex's integrated apply_patch resolves against the same root as
            # thread/start. Give only the Hermes MCP child an import path back
            # to this source checkout.
            mcp_env_args.extend(
                [
                    "-c",
                    (
                        "mcp_servers.hermes-tools.env.PYTHONPATH="
                        f"{json.dumps(str(source_root))}"
                    ),
                ]
            )
            sandbox_args = ["-c", 'sandbox_mode="read-only"']
            if not read_only_workspace:
                sandbox_args = [
                    "-c",
                    'sandbox_mode="workspace-write"',
                    "-c",
                    f"sandbox_workspace_write.writable_roots={writable_roots}",
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                ]
            app_server_args.extend(
                [
                    *sandbox_args,
                    "-c",
                    'approval_policy="never"',
                    # A Kanban worker must not inherit arbitrary user MCP
                    # servers (networked Proxmox, mail, etc.). Replace that
                    # table with the one task-scoped Hermes lifecycle bridge.
                    "-c",
                    "mcp_servers={}",
                    "-c",
                    f'mcp_servers.hermes-tools.command="{mcp_python}"',
                    "-c",
                    (
                        "mcp_servers.hermes-tools.args="
                        '["-m","agent.transports.hermes_tools_mcp_server"]'
                    ),
                    "-c",
                    "mcp_servers.hermes-tools.startup_timeout_sec=30",
                    "-c",
                    "mcp_servers.hermes-tools.tool_timeout_sec=180",
                    *mcp_env_args,
                ]
            )

        cmd = [codex_bin, "app-server"] + app_server_args
        # Codex emits tracing to stderr; default WARN keeps it quiet for users.
        spawn_env.setdefault("RUST_LOG", "warn")

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=spawn_env,
            cwd=self._cwd,
        )
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        # Requests may be issued by the turn-driving thread and a Kanban
        # comment-forwarding thread at the same time. Keep each JSON-RPC
        # frame (write + flush) atomic so their bytes cannot interleave.
        self._send_lock = threading.Lock()
        self._notifications: queue.Queue = queue.Queue()
        self._server_requests: queue.Queue = queue.Queue()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()
        self._closed = False
        self._initialized = False

        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    # ---------- lifecycle ----------

    def initialize(
        self,
        client_name: str = "hermes",
        client_title: str = "Hermes Agent",
        client_version: str = "0.1",
        capabilities: Optional[dict] = None,
        timeout: float = 10.0,
    ) -> dict:
        """Send `initialize` + `initialized` handshake. Returns the server's
        InitializeResponse (userAgent, codexHome, platformFamily, platformOs)."""
        if self._initialized:
            raise RuntimeError("already initialized")
        params = {
            "clientInfo": {
                "name": client_name,
                "title": client_title,
                "version": client_version,
            },
            "capabilities": capabilities or {},
        }
        result = self.request("initialize", params, timeout=timeout)
        self.notify("initialized")
        self._initialized = True
        return result

    def close(self, timeout: float = 3.0) -> None:
        """Close stdin and wait for the subprocess to exit, escalating to kill."""
        if self._closed:
            return
        self._closed = True
        self._fail_pending("codex app-server client closed")
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1.0)
            except Exception:
                pass

    def __enter__(self) -> "CodexAppServerClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- send/receive ----------

    def request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict:
        """Send a JSON-RPC request and block on the response. Returns `result`,
        raises CodexAppServerError on `error`."""
        rid = self._take_id()
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[rid] = _Pending(queue=q, method=method)
        self._send({"id": rid, "method": method, "params": params or {}})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise TimeoutError(
                f"codex app-server method {method!r} timed out after {timeout}s"
            )
        if "error" in msg:
            err = msg["error"]
            raise CodexAppServerError(
                code=err.get("code", -1),
                message=err.get("message", ""),
                data=err.get("data"),
            )
        return msg.get("result", {})

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        self._send({"method": method, "params": params or {}})

    def respond(self, request_id: Any, result: dict) -> None:
        """Reply to a server-initiated request (e.g. approval prompts)."""
        self._send({"id": request_id, "result": result})

    def respond_error(
        self, request_id: Any, code: int, message: str, data: Optional[Any] = None
    ) -> None:
        """Reply to a server-initiated request with an error."""
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"id": request_id, "error": err})

    def take_notification(self, timeout: float = 0.0) -> Optional[dict]:
        """Pop the next streaming notification, or return None on timeout.

        timeout=0.0 means non-blocking. Use small positive timeouts inside the
        AIAgent turn loop to interleave reads with interrupt checks."""
        try:
            if timeout <= 0:
                return self._notifications.get_nowait()
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def take_server_request(self, timeout: float = 0.0) -> Optional[dict]:
        """Pop the next server-initiated request (e.g. exec/applyPatch approval)."""
        try:
            if timeout <= 0:
                return self._server_requests.get_nowait()
            return self._server_requests.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---------- diagnostics ----------

    def stderr_tail(self, n: int = 20) -> list[str]:
        """Return last n lines of codex's stderr (for error reports)."""
        with self._stderr_lock:
            return list(self._stderr_lines[-n:])

    def is_alive(self) -> bool:
        return self._proc.poll() is None

    # ---------- internals ----------

    def _take_id(self) -> int:
        # JSON-RPC ids only need to be unique per-connection. A simple
        # monotonically increasing int is the common choice and matches what
        # codex's own clients use.
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
            return rid

    def _send(self, obj: dict) -> None:
        if self._closed:
            raise RuntimeError("codex app-server client is closed")
        if self._proc.stdin is None:
            raise RuntimeError("codex app-server stdin not available")
        try:
            with self._send_lock:
                self._proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise RuntimeError(
                f"codex app-server stdin closed unexpectedly: {exc}"
            ) from exc

    def _read_stdout(self) -> None:
        if self._proc.stdout is None:
            return
        try:
            for line in iter(self._proc.stdout.readline, b""):
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON output is unexpected on stdout; tracing belongs
                    # on stderr. Surface it via stderr buffer for diagnostics.
                    with self._stderr_lock:
                        self._stderr_lines.append(
                            f"<non-json on stdout> {line[:200]!r}"
                        )
                    continue
                self._dispatch(msg)
        except Exception as exc:
            with self._stderr_lock:
                self._stderr_lines.append(f"<stdout reader error> {exc}")
        finally:
            self._fail_pending("codex app-server stdout closed")

    def _fail_pending(self, message: str) -> None:
        """Wake every request waiter when the subprocess can no longer reply."""
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        error = {"error": {"code": -32000, "message": message}}
        for request in pending:
            try:
                request.queue.put_nowait(error)
            except queue.Full:  # pragma: no cover - defensive
                pass

    def _dispatch(self, msg: dict) -> None:
        # Reply (has id + result/error, no method)
        if "id" in msg and ("result" in msg or "error" in msg):
            with self._pending_lock:
                pending = self._pending.pop(msg["id"], None)
            if pending is not None:
                try:
                    pending.queue.put_nowait(msg)
                except queue.Full:  # pragma: no cover - defensive
                    pass
            return
        # Server-initiated request (has id + method)
        if "id" in msg and "method" in msg:
            self._server_requests.put(msg)
            return
        # Notification (no id)
        if "method" in msg:
            self._notifications.put(msg)

    def _read_stderr(self) -> None:
        if self._proc.stderr is None:
            return
        try:
            for line in iter(self._proc.stderr.readline, b""):
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_lines.append(
                        line.decode("utf-8", "replace").rstrip()
                    )
                    # Bound memory: keep last 500 lines.
                    if len(self._stderr_lines) > 500:
                        self._stderr_lines = self._stderr_lines[-500:]
        except Exception:  # pragma: no cover
            pass


def parse_codex_version(output: str) -> Optional[tuple[int, int, int]]:
    """Parse `codex --version` output. Returns (major, minor, patch) or None."""
    # Output format: "codex-cli 0.130.0" possibly followed by metadata.
    import re

    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_codex_binary(
    codex_bin: str = "codex", min_version: tuple[int, int, int] = MIN_CODEX_VERSION
) -> tuple[bool, str]:
    """Verify codex CLI is installed and meets minimum version.

    Returns (ok, message). Used by setup wizard and runtime startup."""
    try:
        proc = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, (
            f"codex CLI not found at {codex_bin!r}. Install with: "
            f"npm i -g @openai/codex"
        )
    except subprocess.TimeoutExpired:
        return False, "codex --version timed out"
    if proc.returncode != 0:
        return False, f"codex --version exited {proc.returncode}: {proc.stderr.strip()}"
    version = parse_codex_version(proc.stdout)
    if version is None:
        return False, f"could not parse codex version from: {proc.stdout!r}"
    if version < min_version:
        return False, (
            f"codex {'.'.join(map(str, version))} is older than required "
            f"{'.'.join(map(str, min_version))}. Run: npm i -g @openai/codex"
        )
    return True, ".".join(map(str, version))
