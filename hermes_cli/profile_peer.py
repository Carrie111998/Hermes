"""Structured local Hermes profile invocation with stable session continuity.

This module is deliberately platform-neutral.  It owns the subprocess and
state-db mechanics needed to send a message to another local profile, while
callers remain responsible for authorization, prompt framing, redaction, and
audit logging.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Optional


STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_TIMEOUT = "timeout"


def _profile_home(profile: str) -> Optional[str]:
    try:
        from hermes_cli.profiles import get_profile_dir

        return str(get_profile_dir(profile))
    except Exception:
        if not profile or profile == "default":
            try:
                from hermes_cli.config import get_hermes_home

                return str(get_hermes_home())
            except Exception:
                return None
        return os.path.expanduser(f"~/.hermes/profiles/{profile}")


def safe_context_slug(value: str, max_len: int = 96) -> str:
    """Sanitize an untrusted context id before using it in a session title."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-._")
    return (slug or "ctx")[:max_len]


@dataclass(frozen=True)
class ProfilePeerResult:
    profile: str
    state: str
    text: str
    session_id: str
    context_id: str
    duration_ms: int
    error: Optional[str] = None


class ProfilePeerDispatcher:
    """Invoke local profiles while preserving one session per peer context."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str], str] = {}
        self._locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # First-contact session discovery searches by timestamp. Serialize it
        # per profile so two new contexts cannot claim the same newest row.
        self._creation_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, key: tuple[str, str, str]) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _creation_lock_for(self, profile: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._creation_locks.get(profile)
            if lock is None:
                lock = threading.Lock()
                self._creation_locks[profile] = lock
            return lock

    @staticmethod
    def _state_db(profile: str) -> Optional[str]:
        home = _profile_home(profile)
        return os.path.join(home, "state.db") if home else None

    def _lookup_session(self, profile: str, title: str) -> str:
        db = self._state_db(profile)
        if not db or not os.path.exists(db):
            return ""
        try:
            with sqlite3.connect(db, timeout=5) as con:
                row = con.execute(
                    "SELECT id FROM sessions WHERE title = ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (title,),
                ).fetchone()
            return str(row[0]) if row else ""
        except Exception:
            return ""

    def _session_exists(self, profile: str, session_id: str) -> bool:
        """Return whether a cached session still belongs to the current DB."""
        db = self._state_db(profile)
        if not db or not os.path.exists(db) or not session_id:
            return False
        try:
            with sqlite3.connect(db, timeout=5) as con:
                row = con.execute(
                    "SELECT 1 FROM sessions WHERE id = ? LIMIT 1", (session_id,)
                ).fetchone()
            return row is not None
        except Exception:
            # A locked, replaced, or unreadable DB must not make us blindly
            # resume a process-global cached id from an earlier profile state.
            return False

    def _latest_session(self, profile: str, source: str, started_after: float) -> str:
        db = self._state_db(profile)
        if not db or not os.path.exists(db):
            return ""
        try:
            with sqlite3.connect(db, timeout=5) as con:
                row = con.execute(
                    "SELECT id FROM sessions WHERE source = ? AND started_at >= ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (source, started_after - 2.0),
                ).fetchone()
            return str(row[0]) if row else ""
        except Exception:
            return ""

    def _title_session(self, profile: str, session_id: str, title: str) -> None:
        db = self._state_db(profile)
        if not db or not os.path.exists(db) or not session_id:
            return
        try:
            with sqlite3.connect(db, timeout=5) as con:
                con.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
                con.commit()
        except Exception:
            return

    def call(
        self,
        *,
        profile: str,
        message: str,
        context_id: str,
        source: str = "a2a",
        title_prefix: str = "a2a-peer",
        timeout: float = 300,
        env_extra: Optional[Mapping[str, str]] = None,
    ) -> ProfilePeerResult:
        """Send one final-output CLI turn to ``profile``.

        The returned result is structured but intentionally unredacted.  A
        platform or RPC boundary must redact it according to its own policy.
        """
        profile = str(profile or "default").strip() or "default"
        context_id = str(context_id or "")
        safe_ctx = safe_context_slug(context_id, max_len=80)
        context_digest = hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:12]
        title_prefix = str(title_prefix or "a2a-peer").strip("- ") or "a2a-peer"
        session_title = f"{title_prefix}-{safe_ctx}-{context_digest}"
        # Use the exact context in synchronization/cache identity. Sanitized or
        # truncated display slugs are not injective ("foo/a" and "foo a"), so
        # using one here could merge unrelated private conversations.
        key = (profile, title_prefix, context_id)
        started = time.monotonic()

        def result(state: str, text: str = "", session_id: str = "", error: Optional[str] = None):
            return ProfilePeerResult(
                profile=profile,
                state=state,
                text=text,
                session_id=session_id,
                context_id=context_id,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                error=error,
            )

        lock = self._lock_for(key)
        with lock:
            session_id = self._sessions.get(key) or ""
            if session_id and not self._session_exists(profile, session_id):
                # The target profile may have been deleted/restored or its
                # state DB replaced while this process remained alive.
                self._sessions.pop(key, None)
                session_id = ""
            session_id = session_id or self._lookup_session(profile, session_title)
            # Discovery of the session created by a CLI first turn is inherently
            # timestamp-based, so serialize only that creation path per profile.
            creation_lock = self._creation_lock_for(profile) if not session_id else None
            if creation_lock is not None:
                creation_lock.acquire()
                session_id = self._sessions.get(key) or ""
                if session_id and not self._session_exists(profile, session_id):
                    self._sessions.pop(key, None)
                    session_id = ""
                session_id = session_id or self._lookup_session(profile, session_title)
            try:
                cmd = ["hermes", "chat", "-q", message, "-Q", "--source", source]
                if session_id:
                    cmd.extend(["--resume", session_id])
                env = os.environ.copy()
                home = _profile_home(profile)
                if home:
                    env["HERMES_HOME"] = home
                if env_extra:
                    env.update({str(k): str(v) for k, v in env_extra.items()})
                wall_started = time.time()
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                        check=False,
                        stdin=subprocess.DEVNULL,
                    )
                except subprocess.TimeoutExpired:
                    return result(STATE_TIMEOUT, error="profile did not reply in time")
                except Exception as exc:
                    return result(STATE_FAILED, error=f"Profile dispatch failed: {exc}")

                if proc.returncode != 0:
                    error = (proc.stderr or proc.stdout or f"profile exited {proc.returncode}").strip()
                    return result(STATE_FAILED, session_id=session_id, error=error[-2000:])
                if not session_id:
                    session_id = self._latest_session(profile, source, wall_started)
                    if session_id:
                        self._sessions[key] = session_id
                        self._title_session(profile, session_id, session_title)
                return result(STATE_COMPLETED, (proc.stdout or "").strip(), session_id)
            finally:
                if creation_lock is not None:
                    creation_lock.release()


_dispatcher = ProfilePeerDispatcher()


def get_profile_peer_dispatcher() -> ProfilePeerDispatcher:
    """Return the process-wide dispatcher shared by local peer integrations."""
    return _dispatcher
