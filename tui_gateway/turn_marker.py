"""Durable interrupted-turn markers for the desktop/TUI auto-continue path.

A running turn's progress lives only in process memory (the agent flushes to
SQLite at turn end, not mid-turn), so an app/backend/machine death mid-turn
leaves no durable trace of the interrupted prompt. This sidecar is that
trace: a marker is written when a turn starts running and cleared when the
turn concludes — success, handled error, or interrupt all clear it, so only
a process death leaves one behind. ``session.resume`` reads the marker to
decide whether to auto-continue the interrupted turn (see
``_maybe_schedule_auto_continue`` in ``tui_gateway/server.py``).

Markers are stored per ``HERMES_HOME`` (callers pass the session's home so
profile sessions keep their state in their own profile directory) and the
layout is *session-scoped*: every session gets its own file under
``<HERMES_HOME>/desktop/interrupted_turns/<session_key>.json``. Per-session
files are structurally isolated — one session's marker never shares bytes
with another session's, so two backends sharing a ``HERMES_HOME`` (Desktop
primary + isolated ``hermes serve``), or two sessions concurrently resumed
inside one process, cannot let session B's resume observe session A's
interrupted prompt and schedule a duplicate auto-continue (issue #94778).
The file name is the sanitized ``session_key``; unsafe keys are rejected
silently rather than allowed to escape the marker directory.

Every function is best-effort by design — marker bookkeeping must never
break a turn — so I/O errors degrade to "no marker" instead of raising.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MARKER_DIR = "desktop"
_MARKER_SUBDIR = "interrupted_turns"
_MARKER_SUFFIX = ".json"
# Cap so a runaway scan over ``interrupted_turns/`` stays bounded; the per-session
# layout already isolates sessions, this just defends against a profile blowing
# up the directory.
_MAX_AGE_SECS = 24 * 3600
# Enough to re-submit any realistic prompt; guards the sidecar against a
# pathological multi-megabyte paste being journaled on every turn.
_MAX_PROMPT_CHARS = 64_000
# Conservative: hex session_keys are 32 chars today but future formats may grow.
# Anything outside this set is rejected so a malicious or buggy caller cannot
# craft a key that escapes the marker directory.
_SAFE_SESSION_KEY = re.compile(r"^[A-Za-z0-9._\-]{1,128}$")

_lock = threading.Lock()


def _marker_dir(home: Path | str) -> Path:
    return Path(home) / _MARKER_DIR / _MARKER_SUBDIR


def _marker_path(home: Path | str, session_key: str) -> Path:
    """Path to the per-session marker file. Returns ``None``-equivalent (a
    sentinel) for unsafe keys so callers can short-circuit — but this layer
    also rejects unsafe keys explicitly in public APIs."""
    return _marker_dir(home) / f"{session_key}{_MARKER_SUFFIX}"


def _safe_session_key(session_key: str) -> bool:
    return bool(session_key) and bool(_SAFE_SESSION_KEY.match(session_key))


def _load(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        logger.debug("unreadable turn-marker file %s; treating as absent", path, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _prune_in_place(path: Path, now: float) -> None:
    """Drop stale marker files in the per-session directory (best-effort).

    Only the per-session files older than ``_MAX_AGE_SECS`` are removed, so
    a slow background process cannot pop another session's still-live marker.
    Each session's lifecycle is owned by its own writer.
    """
    try:
        dirpath = path.parent
        for entry in dirpath.iterdir():
            if not entry.is_file() or not entry.name.endswith(_MARKER_SUFFIX):
                continue
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if now - mtime > _MAX_AGE_SECS:
                entry.unlink(missing_ok=True)
    except Exception:
        logger.debug("turn-marker prune failed under %s", path.parent, exc_info=True)


def _store(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".turn-marker-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _remove(path: Path) -> None:
    """Best-effort unlink; missing is fine."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        logger.debug("failed to remove turn-marker file %s", path, exc_info=True)


def record_turn_start(
    home: Path | str, session_key: str, prompt: str, *, attempts: int = 0
) -> None:
    """Persist the marker for a turn that is about to run.

    ``attempts`` counts how many auto-continues led to this run: 0 for a
    user-initiated turn, N for the Nth automatic re-run — the crash-loop
    breaker reads it back on the next resume. Writes to a per-session
    file so two backends sharing ``HERMES_HOME`` cannot overwrite each
    other's marker; unsafe ``session_key`` values are rejected silently.
    """
    if not _safe_session_key(session_key) or not prompt:
        return
    now = time.time()
    entry = {
        "attempts": max(0, int(attempts)),
        "prompt": prompt[:_MAX_PROMPT_CHARS],
        "started_at": now,
        "session_key": session_key,
    }
    try:
        with _lock:
            path = _marker_path(home, session_key)
            _prune_in_place(path, now)
            _store(path, entry)
    except Exception:
        logger.debug("failed to record turn marker for %s", session_key, exc_info=True)


def clear_turn_marker(home: Path | str, session_key: str) -> None:
    """Remove the marker once its turn concluded (any outcome the client saw).

    Only removes the per-session marker file — never touches anything else
    in ``HERMES_HOME``, so clearing B cannot disturb A.
    """
    if not _safe_session_key(session_key):
        return
    try:
        with _lock:
            _remove(_marker_path(home, session_key))
    except Exception:
        logger.debug("failed to clear turn marker for %s", session_key, exc_info=True)


def read_turn_marker(home: Path | str, session_key: str) -> dict[str, Any] | None:
    """The marker left by a turn that never concluded, or None.

    Reads from the per-session file for ``session_key``; the file is the
    only place session data lives, so this read can never observe another
    session's marker regardless of how many backends share ``HERMES_HOME``.
    The in-file ``session_key`` stamp is the writer's claim: a marker whose
    recorded owner disagrees with the requested key is treated as absent, so
    no caller — including the auto-continue scheduling path, which admits a
    fresh marker as crash evidence — can act on a foreign marker.
    """
    if not _safe_session_key(session_key):
        return None
    try:
        with _lock:
            path = _marker_path(home, session_key)
            data = _load(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    recorded_owner = data.get("session_key")
    if recorded_owner is not None and str(recorded_owner) != session_key:
        # The file claims to belong to another session. The per-session file
        # name is the primary owner key, but the body stamp must agree too:
        # a marker whose writer's claim differs from the resumed session is
        # never crash evidence (issue #94778 / owner-check parity with the
        # state.db records of #86786).
        logger.debug(
            "turn-marker owner mismatch for %s (recorded %s); treating as absent",
            session_key,
            recorded_owner,
        )
        return None
    prompt = str(data.get("prompt") or "")
    if not prompt.strip():
        return None
    try:
        started_at = float(data.get("started_at") or 0)
        attempts = max(0, int(data.get("attempts") or 0))
    except (TypeError, ValueError):
        return None
    return {"attempts": attempts, "prompt": prompt, "started_at": started_at}
