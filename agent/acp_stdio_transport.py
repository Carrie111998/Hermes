"""Shared ACP stdio transport used by Hermes ACP model-provider clients.

An ACP CLI speaks JSON-RPC over stdin/stdout. Hermes rebuilds the full
conversation transcript on every chat completion, so this transport:

* keeps one durable child process per client (initialize once);
* opens a fresh session/new per completion so the rebuilt transcript is
  not replayed on top of session history the agent already holds;
* serializes the stdio wire (one in-flight JSON-RPC exchange at a time);
* on close(), kills the POSIX process group. ACP launchers fork workers
  (kiro-cli acp execs kiro-cli-chat) that otherwise survive as orphans.

Permission policy is injected by the vendor profile. Kiro's handler
selects allow_once only — never session-wide allow/approve. Hermes does
not own Kiro's exec tools. Copilot ACP stays on its own client and
continues to deny session/request_permission.

On Windows, spawn uses windows_detach_flags and close() tree-kills via
kill_process_tree (taskkill /T /F). POSIX still uses start_new_session
+ killpg.

This module does not spawn Copilot and does not change CopilotACPClient.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Callable

from agent.file_safety import (
    get_read_block_error,
    get_write_denied_error,
    is_write_approval_required,
)
from agent.redact import redact_sensitive_text
from tools.environments.local import hermes_subprocess_env

_DEFAULT_TIMEOUT_SECONDS = 900.0
_EXIT_DRAIN_SECONDS = 0.5
_READER_JOIN_SECONDS = 1.0

logger = logging.getLogger(__name__)

ACP_DEFAULT_TIMEOUT_SECONDS = _DEFAULT_TIMEOUT_SECONDS

PermissionHandler = Callable[[Any, Any], dict[str, Any]]


def is_acp_base_url(url: Any) -> bool:
    text = str(url or "").strip().lower()
    return text.startswith("acp://") or text.startswith("acp+tcp://")


def acp_scheme_host(url: Any) -> str:
    text = str(url or "").strip()
    if "://" not in text:
        return ""
    rest = text.split("://", 1)[1]
    return rest.split("/")[0].split("?")[0].strip().lower()


def resolve_home_dir() -> str:
    home = os.environ.get("HOME", "").strip()
    if home:
        return home
    expanded = os.path.expanduser("~")
    if expanded and expanded != "~":
        return expanded
    try:
        import pwd

        resolved = pwd.getpwuid(os.getuid()).pw_dir.strip()
        if resolved:
            return resolved
    except Exception:
        pass
    return "/tmp"


def build_subprocess_env(*, inherit_credentials: bool = False) -> dict[str, str]:
    env = hermes_subprocess_env(inherit_credentials=inherit_credentials)
    home = resolve_home_dir()
    env["HOME"] = home
    from hermes_constants import apply_subprocess_home_env

    apply_subprocess_home_env(env)
    return env


def resolve_acp_cwd(explicit: str | None) -> str:
    """Anchor the ACP session on Hermes workspace, not os.getcwd()."""
    if explicit is not None and str(explicit).strip():
        return str(Path(str(explicit)).expanduser().resolve())
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd().expanduser().resolve())
    except Exception:
        return str(Path(os.getcwd()).resolve())


def terminate_process_group(proc: Any, *, timeout: float = 2.0) -> None:
    """Stop an ACP child and every process it forked."""
    if proc is None:
        return
    if os.name == "nt":
        try:
            from hermes_cli._subprocess_compat import kill_process_tree

            kill_process_tree(proc)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=timeout)
        except Exception:
            pass
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=timeout)
    except Exception:
        pass
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            pass


def jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": code, "message": message},
    }


def permission_denied(message_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {"outcome": {"outcome": "cancelled"}},
    }


def permission_allowed(message_id: Any, options: Any) -> dict[str, Any]:
    """Kiro-only: pick allow_once. Never session-wide allow/approve.

    Cancelling every request makes Kiro end the turn with ToolUseRejected after
    its first progress message. A one-shot option lets later tools still ask.
    Hermes does not take over Kiro's exec tools and does not grant a session
    blanket. If Kiro only offers session-wide allow, deny.
    """
    if not isinstance(options, list):
        return permission_denied(message_id)
    option_ids = {
        str(option.get("optionId") or "").strip()
        for option in options
        if isinstance(option, dict)
    }
    if "allow_once" in option_ids:
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "outcome": {
                    "outcome": "selected",
                    "optionId": "allow_once",
                }
            },
        }
    return permission_denied(message_id)


def ensure_path_within_cwd(path_text: str, cwd: str) -> Path:
    candidate = Path(path_text)
    if not candidate.is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved = candidate.resolve()
    root = Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(
            f"Path '{resolved}' is outside the session cwd '{root}'."
        ) from exc
    return resolved


def effective_timeout_seconds(timeout: Any) -> float:
    if timeout is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if isinstance(timeout, (int, float)):
        return float(timeout)
    candidates = [
        getattr(timeout, attr, None)
        for attr in ("read", "write", "connect", "pool", "timeout")
    ]
    numeric = [float(v) for v in candidates if isinstance(v, (int, float))]
    return max(numeric) if numeric else _DEFAULT_TIMEOUT_SECONDS


class AcpConnection:
    """One live ACP child process plus its stdio pumps and JSON-RPC state."""

    def __init__(self, process: Any) -> None:
        self.process = process
        self.inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.initialized = False
        self._next_id = 0
        self._threads: list[threading.Thread] = []

    def start_readers(self) -> None:
        def _stdout_reader() -> None:
            stream = self.process.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    try:
                        self.inbox.put(json.loads(line))
                    except Exception:
                        self.inbox.put({"raw": line.rstrip("\n")})
            except Exception:
                return

        def _stderr_reader() -> None:
            stream = self.process.stderr
            if stream is None:
                return
            try:
                for line in stream:
                    self.stderr_tail.append(line.rstrip("\n"))
            except Exception:
                return

        for target in (_stdout_reader, _stderr_reader):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def shutdown(self) -> None:
        terminate_process_group(self.process)
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(self.process, stream_name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass
        for thread in self._threads:
            try:
                thread.join(timeout=_READER_JOIN_SECONDS)
            except Exception:
                pass
        self._threads = []

    def is_alive(self) -> bool:
        try:
            return self.process.poll() is None
        except Exception:
            return False

    def next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def drain_inbox(self) -> None:
        while True:
            try:
                self.inbox.get_nowait()
            except queue.Empty:
                return

    def stderr_text(self) -> str:
        return "\n".join(self.stderr_tail).strip()


_LIVE_TRANSPORTS: "weakref.WeakSet[AcpStdioTransport]" = weakref.WeakSet()
_ATEXIT_REGISTERED = False
_ATEXIT_LOCK = threading.Lock()


def _close_live_transports() -> None:
    for transport in list(_LIVE_TRANSPORTS):
        try:
            transport.close()
        except Exception:
            pass


def _register_atexit_cleanup(transport: "AcpStdioTransport") -> None:
    global _ATEXIT_REGISTERED
    with _ATEXIT_LOCK:
        _LIVE_TRANSPORTS.add(transport)
        if not _ATEXIT_REGISTERED:
            atexit.register(_close_live_transports)
            _ATEXIT_REGISTERED = True


class AcpStdioTransport:
    """Durable one-process ACP JSON-RPC client."""

    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str | None = None,
        vendor_label: str = "ACP",
        missing_hint: str = "",
        permission_handler: PermissionHandler | None = None,
        inherit_credentials: bool = False,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.cwd = resolve_acp_cwd(cwd)
        self.vendor_label = vendor_label
        self.missing_hint = missing_hint
        self.permission_handler = permission_handler or (
            lambda message_id, _options: permission_denied(message_id)
        )
        self.inherit_credentials = inherit_credentials
        self.is_closed = False
        self._connection: AcpConnection | None = None
        self._active_process_lock = threading.Lock()
        self._prompt_lock = threading.RLock()

    @property
    def active_process(self) -> Any:
        conn = self._connection
        return conn.process if conn is not None else None

    def close(self) -> None:
        with self._active_process_lock:
            conn = self._connection
            self._connection = None
        self.is_closed = True
        if conn is None:
            return
        conn.shutdown()

    def _discard_connection(self, conn: AcpConnection) -> None:
        with self._active_process_lock:
            if self._connection is conn:
                self._connection = None
        try:
            conn.shutdown()
        except Exception:
            pass

    def run_prompt(self, prompt_text: str, *, timeout_seconds: float) -> tuple[str, str]:
        with self._prompt_lock:
            conn = self._ensure_connection(timeout_seconds=timeout_seconds)
            conn.drain_inbox()
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            try:
                session = self._request(
                    conn,
                    "session/new",
                    {"cwd": self.cwd, "mcpServers": []},
                    timeout_seconds=timeout_seconds,
                ) or {}
                session_id = str(session.get("sessionId") or "").strip()
                if not session_id:
                    raise RuntimeError(f"{self.vendor_label} did not return a sessionId.")
                self._request(
                    conn,
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": prompt_text}],
                    },
                    timeout_seconds=timeout_seconds,
                    text_parts=text_parts,
                    reasoning_parts=reasoning_parts,
                )
            except BaseException:
                self._discard_connection(conn)
                raise
            return "".join(text_parts), "".join(reasoning_parts)

    def _ensure_connection(self, *, timeout_seconds: float) -> AcpConnection:
        with self._active_process_lock:
            conn = self._connection
        if conn is not None:
            if conn.is_alive() and conn.initialized:
                return conn
            logger.info(
                "ACP process %s is no longer usable; respawning",
                getattr(conn.process, "pid", "?"),
            )
            self._discard_connection(conn)

        conn = self._spawn_connection()
        with self._active_process_lock:
            self._connection = conn
        self.is_closed = False
        _register_atexit_cleanup(self)
        try:
            self._request(
                conn,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True}
                    },
                    "clientInfo": {
                        "name": "hermes-agent",
                        "title": "Hermes Agent",
                        "version": "0.0.0",
                    },
                },
                timeout_seconds=timeout_seconds,
            )
        except BaseException:
            self._discard_connection(conn)
            raise
        conn.initialized = True
        return conn

    def _spawn_connection(self) -> AcpConnection:
        try:
            from hermes_cli._subprocess_compat import (
                windows_detach_flags,
                windows_detach_flags_without_breakaway,
            )

            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "cwd": self.cwd,
                "env": build_subprocess_env(inherit_credentials=self.inherit_credentials),
                "start_new_session": os.name != "nt",
            }
            if os.name == "nt":
                try:
                    proc = subprocess.Popen(
                        [self.command] + self.args,
                        creationflags=windows_detach_flags(),
                        **popen_kwargs,
                    )
                except OSError:
                    proc = subprocess.Popen(
                        [self.command] + self.args,
                        creationflags=windows_detach_flags_without_breakaway(),
                        **popen_kwargs,
                    )
            else:
                proc = subprocess.Popen([self.command] + self.args, **popen_kwargs)
        except FileNotFoundError as exc:
            hint = self.missing_hint or (
                f"Install {self.vendor_label} or set its command override."
            )
            raise RuntimeError(
                f"Could not start {self.vendor_label} command '{self.command}'. {hint}"
            ) from exc

        if proc.stdin is None or proc.stdout is None:
            terminate_process_group(proc)
            raise RuntimeError(
                f"{self.vendor_label} process did not expose stdin/stdout pipes."
            )
        conn = AcpConnection(proc)
        conn.start_readers()
        return conn

    def _request(
        self,
        conn: AcpConnection,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        text_parts: list[str] | None = None,
        reasoning_parts: list[str] | None = None,
    ) -> Any:
        proc = conn.process
        request_id = conn.next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RuntimeError(
                f"{self.vendor_label} process is not accepting input for {method}: {exc}"
            ) from exc

        deadline = time.monotonic() + timeout_seconds
        drain_deadline: float | None = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for {self.vendor_label} response to {method}."
                )
            if drain_deadline is None:
                if proc.poll() is not None:
                    drain_deadline = now + _EXIT_DRAIN_SECONDS
            elif now >= drain_deadline:
                break
            try:
                msg = conn.inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if self.handle_server_message(
                msg,
                process=proc,
                cwd=self.cwd,
                text_parts=text_parts,
                reasoning_parts=reasoning_parts,
            ):
                continue
            if msg.get("id") != request_id:
                continue
            if "error" in msg:
                err = msg.get("error") or {}
                raise RuntimeError(
                    f"{self.vendor_label} {method} failed: {err.get('message') or err}"
                )
            return msg.get("result")
        raise self._process_exited_error(conn, method)

    def _process_exited_error(self, conn: AcpConnection, method: str) -> RuntimeError:
        stderr_text = conn.stderr_text()
        detail = f": {stderr_text}" if stderr_text else "."
        return RuntimeError(
            f"{self.vendor_label} process exited during {method} "
            f"(exit code {conn.process.poll()}){detail}"
        )

    def handle_server_message(
        self,
        msg: dict[str, Any],
        *,
        process: subprocess.Popen[str],
        cwd: str,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
    ) -> bool:
        method = msg.get("method")
        if not isinstance(method, str):
            return False
        if method == "session/update":
            params = msg.get("params") or {}
            update = params.get("update") or {}
            kind = str(update.get("sessionUpdate") or "").strip()
            content = update.get("content") or {}
            chunk_text = ""
            if isinstance(content, dict):
                chunk_text = str(content.get("text") or "")
            if kind == "agent_message_chunk" and chunk_text and text_parts is not None:
                text_parts.append(chunk_text)
            elif kind == "agent_thought_chunk" and chunk_text and reasoning_parts is not None:
                reasoning_parts.append(chunk_text)
            return True
        if process.stdin is None:
            return True
        message_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            response = self.permission_handler(message_id, params.get("options"))
        elif method == "fs/read_text_file":
            try:
                path = ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                block_error = get_read_block_error(str(path))
                if block_error:
                    raise PermissionError(block_error)
                try:
                    content = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    content = ""
                line = params.get("line")
                limit = params.get("limit")
                if isinstance(line, int) and line > 1:
                    lines = content.splitlines(keepends=True)
                    start = line - 1
                    end = start + limit if isinstance(limit, int) and limit > 0 else None
                    content = "".join(lines[start:end])
                if content:
                    content = redact_sensitive_text(content, force=True)
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {"content": content},
                }
            except Exception as exc:
                response = jsonrpc_error(message_id, -32602, str(exc))
        elif method == "fs/write_text_file":
            try:
                path = ensure_path_within_cwd(str(params.get("path") or ""), cwd)
                denied = get_write_denied_error(str(path))
                if denied:
                    raise PermissionError(denied)
                if is_write_approval_required(str(path)):
                    raise PermissionError(
                        f"Write denied: '{path}' requires interactive approval "
                        "and cannot be written through the ACP file bridge."
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(params.get("content") or ""), encoding="utf-8")
                response = {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": None,
                }
            except Exception as exc:
                response = jsonrpc_error(message_id, -32602, str(exc))
        else:
            if message_id is None:
                # JSON-RPC notification: never reply, especially not -32601.
                return True
            response = jsonrpc_error(
                message_id,
                -32601,
                f"ACP client method '{method}' is not supported by Hermes yet.",
            )
        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True
