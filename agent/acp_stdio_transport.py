"""Shared ACP stdio transport used by Hermes ACP model-provider clients.

An ACP CLI speaks JSON-RPC over stdin/stdout. Hermes rebuilds the full
conversation transcript on every chat completion, so this transport:

* keeps one durable child process per client (initialize once);
* opens a fresh session/new per completion so the rebuilt transcript is
  not replayed on top of session history the agent already holds;
* serializes the stdio wire (one in-flight JSON-RPC exchange at a time);
* on close(), kills the POSIX process group. ACP launchers fork workers
  (kiro-cli acp execs kiro-cli-chat) that otherwise survive as orphans.

Permission policy is injected by the vendor profile. Kiro denies native
tool permissions so Hermes executes the mapped tool_calls (Codex-parity).
Copilot ACP stays on its own client and continues to deny
session/request_permission.

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

from agent.acp_tool_bridge import extract_acp_usage, parse_acp_tool_update
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


def acp_uses_oneshot_completion(*, provider: Any = None, base_url: Any = None) -> bool:
    """True when the ACP client cannot yield live session/update chunks.

    KiroACPClient implements stream=True (tool/text cards mid-turn).
    CopilotACPClient and unknown ACP hosts still return a one-shot
    SimpleNamespace, so the conversation loop must stay non-streaming.
    """
    slug = str(provider or "").strip().lower()
    host = acp_scheme_host(base_url)
    if slug == "kiro-acp" or host == "kiro":
        return False
    if slug == "copilot-acp" or host == "copilot":
        return True
    url = str(base_url or "").strip().lower()
    return url.startswith("acp://") or url.startswith("acp+tcp://")


def coerce_session_update(params: Any) -> list[dict[str, Any]]:
    """Normalize Kiro/ACP session/update params into a list of update dicts.

    Vendors nest the payload as ``params.update``, send a list, or put
    ``sessionUpdate`` on ``params`` itself. We accept all three.
    """
    if not isinstance(params, dict):
        return []
    raw = params.get("update")
    if raw is None:
        raw = params.get("sessionUpdate")
        if isinstance(raw, str):
            return [params]
        if raw is None and (
            params.get("sessionUpdate")
            or params.get("toolCallId")
            or params.get("tool_call_id")
            or params.get("kind")
        ):
            return [params]
        raw = params
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, dict):
        nested = raw.get("toolCall") or raw.get("tool_call")
        if isinstance(nested, dict) and not raw.get("sessionUpdate"):
            merged = dict(nested)
            merged.setdefault("sessionUpdate", "tool_call")
            return [merged]
        return [raw]
    return []


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
        self._on_update: Callable[[str, Any], None] | None = None
        self.last_usage: Any = None
        self._prompt_session_id: str | None = None
        self._cancel_requested = False

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

    def _emit_update(self, kind: str, payload: Any) -> None:
        callback = self._on_update
        if callback is None:
            return
        try:
            callback(kind, payload)
        except Exception:
            logger.debug("ACP on_update callback failed for %s", kind, exc_info=True)

    def _notify(self, conn: AcpConnection, method: str, params: dict[str, Any]) -> None:
        """JSON-RPC notification (no id). Used for session/cancel."""
        proc = conn.process
        if proc is None or proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except Exception:
            logger.debug("ACP notify %s failed", method, exc_info=True)

    def cancel_prompt(self) -> None:
        """End the in-flight session/prompt like Codex finish_reason=tool_calls."""
        session_id = self._prompt_session_id
        conn = self._connection
        if not session_id or conn is None or self._cancel_requested:
            return
        self._cancel_requested = True
        self._notify(conn, "session/cancel", {"sessionId": session_id})

    def _remember_usage(self, payload: Any) -> None:
        usage = extract_acp_usage(payload)
        if usage is None:
            return
        self.last_usage = usage
        self._emit_update("usage", usage)

    def run_prompt(
        self,
        prompt_text: str,
        *,
        timeout_seconds: float,
        on_update: Callable[[str, Any], None] | None = None,
    ) -> tuple[str, str]:
        with self._prompt_lock:
            self._on_update = on_update
            self.last_usage = None
            self._cancel_requested = False
            self._prompt_session_id = None
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
                self._remember_usage(session)
                session_id = str(session.get("sessionId") or "").strip()
                if not session_id:
                    raise RuntimeError(f"{self.vendor_label} did not return a sessionId.")
                self._prompt_session_id = session_id
                result = self._request(
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
                self._remember_usage(result)
            except BaseException:
                self._discard_connection(conn)
                raise
            finally:
                self._prompt_session_id = None
                self._on_update = None
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
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
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
        last_heartbeat = time.monotonic()
        while True:
            now = time.monotonic()
            if self._on_update is not None and (now - last_heartbeat) >= 15.0:
                self._emit_update("heartbeat", None)
                last_heartbeat = now
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
                if method == "session/prompt" and self._cancel_requested:
                    self._remember_usage(msg.get("error"))
                    self._remember_usage(msg)
                    return {"stopReason": "cancelled"}
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

    def _handle_session_update(
        self,
        update: dict[str, Any],
        *,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
    ) -> None:
        self._remember_usage(update)
        kind = str(update.get("sessionUpdate") or update.get("type") or "").strip()
        content = update.get("content") or {}
        chunk_text = ""
        if isinstance(content, dict):
            chunk_text = str(content.get("text") or "")
        elif isinstance(content, str):
            chunk_text = content
        elif isinstance(content, list):
            bits: list[str] = []
            for item in content:
                if isinstance(item, str) and item:
                    bits.append(item)
                elif isinstance(item, dict):
                    inner = item.get("content") if isinstance(item.get("content"), dict) else item
                    if isinstance(inner, dict) and inner.get("text"):
                        bits.append(str(inner.get("text") or ""))
            chunk_text = "".join(bits)
        if kind == "agent_message_chunk" and chunk_text:
            if text_parts is not None:
                text_parts.append(chunk_text)
            self._emit_update("text", chunk_text)
            return
        if kind == "agent_thought_chunk" and chunk_text:
            if reasoning_parts is not None:
                reasoning_parts.append(chunk_text)
            self._emit_update("reasoning", chunk_text)
            return
        parsed = parse_acp_tool_update(update)
        if parsed is not None:
            logger.info(
                "ACP session/update tool kind=%s name=%s status=%s keys=%s",
                kind or parsed.get("name"),
                parsed.get("name"),
                parsed.get("status"),
                sorted(update.keys())[:16],
            )
            self._emit_update("tool", parsed)
            return
        if kind and kind not in {
            "agent_message_chunk",
            "agent_thought_chunk",
            "usage_update",
            "available_commands_update",
            "current_mode_update",
            "session_info_update",
        }:
            logger.info(
                "ACP session/update unparsed kind=%s keys=%s",
                kind,
                sorted(update.keys())[:16],
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
            self._remember_usage(params)
            for update in coerce_session_update(params):
                self._handle_session_update(
                    update,
                    text_parts=text_parts,
                    reasoning_parts=reasoning_parts,
                )
            return True
        if process.stdin is None:
            return True
        message_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            tool_call = params.get("toolCall") or params.get("tool_call")
            if isinstance(tool_call, dict):
                update = dict(tool_call)
                update.setdefault("sessionUpdate", "tool_call")
                parsed = parse_acp_tool_update(update)
                if parsed is not None:
                    self._emit_update("tool", parsed)
            response = self.permission_handler(message_id, params.get("options"))
        elif method in {"fs/read_text_file", "fs/write_text_file"}:
            response = jsonrpc_error(
                message_id,
                -32601,
                "Hermes owns file tools. Kiro fs bridge is disabled.",
            )
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
