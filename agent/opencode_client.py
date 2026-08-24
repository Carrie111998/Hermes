"""OpenCode delegate-session client (server API, no TUI scraping).

Hermes drives a headless ``opencode serve`` process over its HTTP JSON API:
create a session, run prompts asynchronously (``prompt_async`` returns HTTP
204), poll messages until the assistant reply lands, and supervise native
interactive questions (``GET /question`` -> auto-answer -> ``POST
/question/{id}/reply`` with ``{answers:[[label], ...]}``).

The HTTP transport is injectable for hermetic tests; by default a small
``urllib`` transport talks to a lazily-spawned, reference-counted shared server
process (one per Hermes process, any cwd — the per-session directory is a query
parameter on each call).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

# Answerer contract shared with the Pi backend: (method, question, options) -> str | None
QuestionAnswerer = Callable[[str, str, List[str]], Optional[str]]

_DEFAULT_READY_TIMEOUT = 30.0
_DEFAULT_POLL_INTERVAL = 0.5


class _Transport(Protocol):
    def request(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, str]:
        ...


class _UrllibTransport:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, str]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Shared server lifecycle (one opencode serve per Hermes process)
# ---------------------------------------------------------------------------

_server_lock = threading.Lock()
_server_handle: Optional["_ServerHandle"] = None


class _ServerHandle:
    def __init__(self, base_url: str, proc: Optional[subprocess.Popen]):
        self.base_url = base_url
        self.proc = proc
        self.refcount = 0


def _server_state() -> Optional[_ServerHandle]:
    return _server_handle


def _reset_server_singleton() -> None:
    """Test hook: forget the singleton without killing anything."""
    global _server_handle
    with _server_lock:
        _server_handle = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _opencode_command() -> List[str]:
    raw = os.environ.get("HERMES_OPENCODE_BIN", "").strip()
    if raw:
        return raw.split()
    binary = shutil.which("opencode") or str(os.path.join(Path_home(), ".local", "bin", "opencode"))
    return [binary]


def Path_home() -> str:
    return os.path.expanduser("~")


def _wait_ready(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            transport = _UrllibTransport(base_url, timeout=5.0)
            status, _text = transport.request("GET", "/session")
            if status == 200:
                return
        except Exception as exc:  # noqa: BLE001 - readiness polling
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"opencode server not ready at {base_url}: {last_error or 'no response'}")


def acquire_opencode_server(ready_timeout: float = _DEFAULT_READY_TIMEOUT) -> _ServerHandle:
    """Return the shared server handle, spawning ``opencode serve`` if needed."""
    global _server_handle
    with _server_lock:
        if _server_handle is not None:
            proc = _server_handle.proc
            if proc is None or proc.poll() is None:
                _server_handle.refcount += 1
                return _server_handle
            _server_handle = None  # dead process: respawn below

        external = os.environ.get("HERMES_OPENCODE_SERVER_URL", "").strip()
        if external:
            # Externally managed server: never spawned, never terminated here.
            handle = _ServerHandle(external.rstrip("/"), None)
        else:
            port = _free_port()
            # ``--pure``: delegate sessions are headless workers; user TUI
            # plugins are not loaded (they have hung the model stream on real
            # installs, e.g. oh-my-opencode variants, and add no value here).
            cmd = _opencode_command() + [
                "serve", "--pure", "--port", str(port), "--hostname", "127.0.0.1",
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            handle = _ServerHandle(f"http://127.0.0.1:{port}", proc)
        # Readiness-gate only servers we spawned; an externally managed URL
        # (HERMES_OPENCODE_SERVER_URL) is trusted as-is so a temporarily
        # unreachable remote does not break startup — real calls surface
        # connection errors themselves.
        try:
            if handle.proc is not None:
                _wait_ready(handle.base_url, ready_timeout)
        except Exception:
            if handle.proc is not None:
                handle.proc.terminate()
            raise
        handle.refcount = 1
        _server_handle = handle
        return handle


def release_opencode_server(handle: _ServerHandle) -> None:
    global _server_handle
    with _server_lock:
        handle.refcount -= 1
        if handle.refcount > 0:
            return
        if _server_handle is handle:
            _server_handle = None
    proc = handle.proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class OpenCodeClient:
    """Persistent OpenCode delegate-session client with question supervision."""

    def __init__(
        self,
        *,
        persistent_session: bool = True,
        session_id: str = "",
        session_name: str = "",
        acp_cwd: str = ".",
        question_answerer: Optional[QuestionAnswerer] = None,
        transport_factory: Optional[Callable[[str], _Transport]] = None,
    ):
        self.persistent_session = persistent_session
        self.session_id = session_id
        self.session_name = session_name or f"Hermes {session_id[:8]}"
        self.cwd = os.path.abspath(acp_cwd)
        self.question_answerer = question_answerer
        self._transport_factory = transport_factory
        self._transport: Optional[_Transport] = None
        self._server_handle: Optional[_ServerHandle] = None
        self.native_session_id: Optional[str] = None
        self.is_closed = False

    # -- internals ---------------------------------------------------------

    def _qdir(self) -> str:
        return "?" + urllib.parse.urlencode({"directory": self.cwd})

    def _http(self) -> _Transport:
        if self._transport is not None:
            return self._transport
        if self._transport_factory is not None:
            self._transport = self._transport_factory(self.cwd)
            return self._transport
        if self._server_handle is None:
            self._server_handle = acquire_opencode_server()
        self._transport = _UrllibTransport(self._server_handle.base_url)
        return self._transport

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, Any]:
        status, text = self._http().request(method, path, body)
        if status >= 400:
            raise RuntimeError(f"opencode server {method} {path} failed: HTTP {status}: {text[:500]}")
        if not text:
            return status, None
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    # -- backend surface ---------------------------------------------------

    def start(self, timeout: float = 30.0) -> Dict[str, Any]:
        if self.native_session_id:
            # Reopen a persisted session: verify it is still addressable.
            try:
                self._request("GET", f"/session/{self.native_session_id}")
                return {"sessionId": self.native_session_id}
            except RuntimeError:
                logger.info("Stored opencode session %s missing; creating a new one", self.native_session_id)
        _status, payload = self._request("POST", f"/session{self._qdir()}", {"title": self.session_name})
        if not isinstance(payload, dict) or not payload.get("id"):
            raise RuntimeError(f"opencode session create returned no id: {payload!r}"[:500])
        self.native_session_id = str(payload["id"])
        return {"sessionId": self.native_session_id}

    def _message_sort_key(self, message: dict) -> tuple:
        info = message.get("info") or {}
        created = (info.get("time") or {}).get("created") or 0
        return (created, info.get("id") or "")

    def _assistant_text(self, message: dict) -> str:
        parts = message.get("parts") or []
        return "\n".join(str(p.get("text") or "") for p in parts if p.get("type") == "text").strip()

    def _baseline_messages(self) -> List[dict]:
        path = f"/session/{self.native_session_id}/message{self._qdir()}"
        _status, data = self._request("GET", path)
        messages = data if isinstance(data, list) else (data or {}).get("data") or []
        return sorted(messages, key=self._message_sort_key)

    def _pending_questions(self) -> List[dict]:
        # NOTE: /question requires the ?directory= scope; without it OpenCode
        # silently returns [] even when a question is pending for the session.
        _status, data = self._request("GET", f"/question{self._qdir()}")
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        return [row for row in rows if row.get("sessionID") == self.native_session_id]

    def _answer_pending_questions(self) -> None:
        for request in self._pending_questions():
            request_id = str(request.get("id") or "")
            if not request_id:
                continue
            questions = request.get("questions") or []
            answers: List[List[str]] = []
            reject = False
            for question in questions:
                options = [str(opt.get("label")) for opt in (question.get("options") or []) if opt.get("label")]
                method = "select" if options else "ask"
                answer = None
                if self.question_answerer is not None:
                    answer = self.question_answerer(method, str(question.get("question") or ""), options)
                if answer is None:
                    reject = True
                    break
                if options:
                    answer = self._match_label(answer, options)
                    if answer is None:
                        reject = True
                        break
                answers.append([answer])
            if reject:
                self._request("POST", f"/question/{request_id}/reject{self._qdir()}", {})
                logger.info("Rejected unanswerable opencode question %s", request_id)
            else:
                self._request("POST", f"/question/{request_id}/reply{self._qdir()}", {"answers": answers})
                logger.info("Replied to opencode question %s: %s", request_id, answers)

    @staticmethod
    def _match_label(answer: str, options: List[str]) -> Optional[str]:
        low = answer.strip().strip(" \"'`.,!\t\n").casefold()
        for option in options:
            if option.strip().casefold() == low:
                return option
        if low.isdigit():
            index = int(low)
            if 1 <= index <= len(options):
                return options[index - 1]
        return None

    def run_session_prompt(
        self,
        message: str,
        timeout_seconds: float = 900.0,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        if not self.native_session_id:
            self.start(timeout=min(30.0, timeout_seconds))
        baseline_ids = {m.get("info", {}).get("id") for m in self._baseline_messages()}
        path = f"/session/{self.native_session_id}/prompt_async{self._qdir()}"
        self._request("POST", path, {"parts": [{"type": "text", "text": message}]})
        deadline = time.monotonic() + timeout_seconds
        last_snapshot: Optional[tuple] = None
        stable_since: Optional[float] = None
        # OpenCode's /question registration and message-part updates lag behind
        # the live turn by several seconds; completion therefore requires the
        # transcript to be unchanged for this long (not merely two polls).
        stable_window = min(max(5.0, poll_interval * 4), max(1.0, timeout_seconds / 3.0))
        while time.monotonic() < deadline:
            self._answer_pending_questions()
            _status, data = self._request("GET", f"/session/{self.native_session_id}/message{self._qdir()}")
            messages = data if isinstance(data, list) else (data or {}).get("data") or []
            messages = sorted(messages, key=self._message_sort_key)
            fresh = [m for m in messages if m.get("info", {}).get("id") not in baseline_ids]
            # Snapshot must cover message IDs AND part payloads: OpenCode
            # streams parts into an existing message, so an ID-only snapshot
            # goes "stable" while the turn is still mid-flight.
            snapshot = tuple(
                (
                    (m.get("info") or {}).get("id"),
                    tuple(sorted(str(p) for p in (m.get("parts") or []))),
                )
                for m in messages
            )
            if snapshot == last_snapshot:
                if stable_since is None:
                    stable_since = time.monotonic()
            else:
                stable_since = None
                last_snapshot = snapshot
            # A multi-step turn emits intermediate assistant text before tool
            # calls, so completion is "transcript unchanged for the stable
            # window, nothing pending, and no tool part still running" rather
            # than "first text part".  (A question tool can be 'running'
            # briefly before its entry appears in /question.)
            tools_running = any(
                (part or {}).get("type") == "tool"
                and (part.get("state") or {}).get("status") == "running"
                for m in fresh
                for part in (m.get("parts") or [])
            )
            if (
                stable_since is not None
                and time.monotonic() - stable_since >= stable_window
                and fresh
                and not tools_running
                and not self._pending_questions()
            ):
                text = "\n".join(
                    part
                    for m in fresh
                    if (m.get("info") or {}).get("role") == "assistant"
                    for part in [self._assistant_text(m)]
                    if part
                ).strip()
                if text:
                    return {
                        "text": text,
                        "state": {"sessionId": self.native_session_id},
                        "duration_s": time.monotonic() - started,
                    }
                raise RuntimeError(
                    "OpenCode turn ended without an assistant text reply "
                    "(a pending question may have been rejected)"
                )
            time.sleep(poll_interval)
        # Timed out: best-effort abort, then surface the timeout.
        try:
            self.abort(timeout=10.0)
        except Exception:
            logger.debug("opencode abort after timeout failed", exc_info=True)
        raise TimeoutError(f"OpenCode turn timed out after {timeout_seconds:.0f}s")

    def steer(self, message: str, timeout_seconds: float = 30.0) -> Dict[str, Any]:
        # OpenCode prompt_async has no native live-steer injection; the tool
        # layer degrades steer to a queued follow-up turn when no turn is
        # running.  Reaching this method means the caller chose to send anyway.
        return {"text": "", "note": "opencode has no live steer; message queued", "queued": True}

    def abort(self, timeout: float = 10.0) -> None:
        if not self.native_session_id:
            return
        self._request("POST", f"/session/{self.native_session_id}/abort{self._qdir()}")

    def get_messages(self, timeout: float = 30.0) -> List[dict]:
        _status, data = self._request("GET", f"/session/{self.native_session_id}/message{self._qdir()}")
        messages = data if isinstance(data, list) else (data or {}).get("data") or []
        return sorted(messages, key=self._message_sort_key)

    def _pending_payload_for(self, session_id: str) -> Optional[Dict[str, Any]]:
        _status, data = self._request("GET", f"/question{self._qdir()}")
        rows = data if isinstance(data, list) else (data or {}).get("data") or []
        for row in rows:
            if row.get("sessionID") != session_id:
                continue
            first = (row.get("questions") or [{}])[0]
            options = [str(opt.get("label")) for opt in (first.get("options") or []) if opt.get("label")]
            return {
                "method": "select" if options else "ask",
                "question": str(first.get("question") or ""),
                "options": options[:50],
                "created_at": time.time(),
            }
        return None

    def pending_question_payload(self) -> Optional[Dict[str, Any]]:
        if not self.native_session_id:
            return None
        return self._pending_payload_for(self.native_session_id)

    def is_dead(self) -> bool:
        handle = self._server_handle
        if handle is None or handle.proc is None:
            return False
        return handle.proc.poll() is not None

    def close(self) -> None:
        if self.is_closed:
            return
        self.is_closed = True
        handle = self._server_handle
        self._transport = None
        self._server_handle = None
        if handle is not None:
            try:
                release_opencode_server(handle)
            except Exception:
                logger.debug("opencode server release failed", exc_info=True)
