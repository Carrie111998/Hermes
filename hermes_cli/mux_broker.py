"""Typed rmux/tmux session broker used by Desktop gateway RPCs.

The renderer never receives a shell door: every operation is a fixed argv and
all mux/session identifiers are validated before subprocess creation.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ALLOWED_KEYS = frozenset(
    {
        "Enter",
        "Escape",
        "Tab",
        "BSpace",
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PPage",
        "NPage",
        "C-c",
        "C-d",
        "C-z",
    }
)
_SESSION_FORMAT = "#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_activity}\t#{pane_current_path}\t#{pane_width}x#{pane_height}"
_PANE_PID_FORMAT = "#{session_name}\t#{pane_pid}"


class MuxError(RuntimeError):
    """A typed mux validation or execution failure."""


def _session_name(value: str) -> str:
    name = str(value or "").strip()
    if not _SESSION_RE.fullmatch(name):
        raise MuxError("invalid session name")
    return name


def parse_session_rows(raw: str) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in str(raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        name, windows, attached, activity, cwd, size = parts
        if not _SESSION_RE.fullmatch(name) or name in seen:
            continue
        try:
            window_count = max(0, int(windows))
            attached_bool = int(attached) > 0
            activity_int = max(0, int(activity))
        except (TypeError, ValueError):
            continue
        seen.add(name)
        sessions.append(
            {
                "name": name,
                "windows": window_count,
                "attached": attached_bool,
                "activity": activity_int,
                "cwd": cwd,
                "size": size,
                "status": "attached" if attached_bool else "detached",
            }
        )
    return sessions


def parse_pane_pid_rows(raw: str) -> dict[str, int]:
    pids: dict[str, int] = {}
    for line in str(raw or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name, pane_pid = parts
        if not _SESSION_RE.fullmatch(name) or name in pids:
            continue
        try:
            value = int(pane_pid)
        except (TypeError, ValueError):
            continue
        if value > 0:
            pids[name] = value
    return pids


class MuxBroker:
    shell = False

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        timeout: float = 5.0,
    ) -> None:
        self._run = run
        self._timeout = timeout
        self._binary = which("rmux")
        self.engine = "rmux" if self._binary else None
        if not self._binary:
            self._binary = which("tmux")
            self.engine = "tmux" if self._binary else None

    def _exec(self, args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if not self._binary:
            raise MuxError("mux unavailable")
        result = self._run(
            [self._binary, *args],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=self._timeout if timeout is None else timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "mux command failed").strip()
            raise MuxError(detail[:500])
        return result

    def list_sessions(self) -> dict[str, Any]:
        if not self._binary:
            return {"available": False, "engine": None, "sessions": [], "error": "mux_unavailable"}
        try:
            result = self._exec(["list-sessions", "-F", _SESSION_FORMAT])
        except MuxError as exc:
            if "no server" in str(exc).lower() or "failed to connect" in str(exc).lower():
                return {"available": True, "engine": self.engine, "sessions": []}
            raise
        sessions = parse_session_rows(result.stdout)
        if sessions:
            pane_result = self._exec(["list-panes", "-a", "-F", _PANE_PID_FORMAT])
            pane_pids = parse_pane_pid_rows(pane_result.stdout)
            for session in sessions:
                pane_pid = pane_pids.get(session["name"])
                if pane_pid is not None:
                    session["pane_pid"] = pane_pid
        return {"available": True, "engine": self.engine, "sessions": sessions}

    def create(self, session: str, *, cwd: str, command: list[str] | None = None) -> dict[str, Any]:
        name = _session_name(session)
        working_dir = os.path.realpath(os.path.expanduser(str(cwd or "")))
        if not os.path.isdir(working_dir):
            raise MuxError("working directory does not exist")
        args = ["new-session", "-d", "-s", name, "-c", working_dir]
        if command:
            if not all(isinstance(part, str) and part and "\x00" not in part for part in command):
                raise MuxError("invalid command argv")
            args.append(shlex.join(command))
        self._exec(args)
        return {"ok": True, "engine": self.engine, "session": name, "cwd": working_dir}

    def capture(self, session: str, *, lines: int = 500) -> dict[str, Any]:
        name = _session_name(session)
        bounded = max(20, min(5000, int(lines)))
        result = self._exec(["capture-pane", "-p", "-e", "-S", f"-{bounded}", "-t", name])
        return {"engine": self.engine, "session": name, "ansi": result.stdout}

    def resize(self, session: str, *, cols: int, rows: int) -> dict[str, Any]:
        name = _session_name(session)
        width = int(cols)
        height = int(rows)
        if not 20 <= width <= 500 or not 5 <= height <= 300:
            raise MuxError("invalid terminal size")
        self._exec(["resize-pane", "-t", name, "-x", str(width), "-y", str(height)])
        return {"ok": True, "engine": self.engine, "session": name, "cols": width, "rows": height}

    def send_input(self, session: str, *, text: str | None = None, key: str | None = None) -> dict[str, Any]:
        name = _session_name(session)
        if (text is None) == (key is None):
            raise MuxError("exactly one of text or key is required")
        if text is not None:
            payload = str(text)
            if len(payload.encode("utf-8")) > 8192:
                raise MuxError("input too large")
            self._exec(["send-keys", "-l", "-t", name, "--", payload])
        else:
            normalized = str(key or "")
            if normalized not in _ALLOWED_KEYS:
                raise MuxError("unsupported key")
            self._exec(["send-keys", "-t", name, normalized])
        return {"ok": True, "engine": self.engine, "session": name}

    def close(self, session: str) -> dict[str, Any]:
        name = _session_name(session)
        self._exec(["kill-session", "-t", name])
        return {"ok": True, "engine": self.engine, "session": name}
