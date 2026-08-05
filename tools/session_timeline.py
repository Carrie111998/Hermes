"""Per-session, per-turn step timeline — a live "what's happening now" view.

Sibling to ``tools/delegation_live_log.py`` but general-purpose: not tied to
delegated children, it covers every tool call any session makes (bare CLI,
gateway, desktop). It reuses the *shape* of delegation's event contract
(``tool.started`` / ``tool.completed`` with ``duration`` / error outcome)
and its truncated, redacted, file-backed philosophy, but none of the
delegation-specific manifest/retention machinery — that part is genuinely
delegation-only (per-child directories, live tail-able .log files, a
7-day-retention manifest.json). This module instead keeps one small bounded
JSON document per session.

Storage: Hermes is multi-process — ``hermes_cli/web_server.py`` (the API)
runs separately from ``gateway/run.py`` (the live agent) and from bare CLI
sessions. An in-memory-only buffer in the API process would be blind to
gateway/CLI sessions, so each session's timeline is a file under
``cache/timeline/<session_id>.json``, written atomically (tmp + rename, the
same pattern used by ``hermes_cli/active_sessions.py`` and the durable
action records in ``hermes_cli/web_server.py``). Within the *recording*
process, a small in-memory ring buffer per session_id acts as a cache (so a
burst of tool calls doesn't require re-reading the file to look up the step
being closed out) — every mutation still flushes the full buffer to disk so
any other process reading the file sees current state. This mirrors the
"in-memory cache with file/durable fallback" pattern already used for
action-status reconciliation, and the bounded-collection convention of
``DashboardHealth._error_times`` (``deque(maxlen=...)``).

Status vocabulary intentionally matches the ``outcome`` values
``agent/tool_executor.py``'s ``emit_event("tool.completed", ...)`` calls
already use (``succeeded`` / ``failed`` / ``blocked``), plus ``running`` for
an in-flight step — no new terminology invented.

``args_digest``: built via ``agent.display.build_tool_preview()`` for shape
and truncation (per existing convention — do not reinvent that), then
additionally passed through ``agent.redact.redact_sensitive_text(force=True)``
as a safety net. This second pass is necessary, not optional:
``build_tool_preview()`` only redacts through
``redact_tool_args_for_display()``, which — as of this writing — redacts
exactly one case (``browser_type``'s ``text`` arg). A secret embedded in,
say, a ``terminal`` command argument passes through
``build_tool_preview()`` untouched (it only compacts/truncates via
``summarize_shell_command``, it does not scan for credentials). Since a
timeline entry is written to a session-scoped file that may be read back
over the dashboard API, it needs the same general-purpose redaction sweep
``tools/delegation_live_log.py`` applies to everything it writes.

``events.jsonl`` is deliberately untouched by this module — the timeline is
a separate, ephemeral, bounded, session-scoped view, not a durable audit
trail.

Design constraints (mirrors ``delegation_live_log.py``):

* **Never raise into the agent loop.** Every public function is wrapped in
  a broad except; failures are swallowed and debug-logged. A timeline write
  must never be able to break a real tool call.
* **Bounded.** Ring buffer capped at ``TIMELINE_MAX_STEPS`` entries per
  session; oldest evicted first (FIFO). ``step_n`` keeps counting up even
  past eviction, so callers can tell how many steps have happened in total.
* **No config knobs.** One module constant for the cap, same spirit as
  delegation's ``LIVE_RETENTION_DAYS``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ring buffer cap: entries beyond this are evicted FIFO, per session.
TIMELINE_MAX_STEPS = 200

# Status vocabulary — mirrors the `outcome` values already used at
# agent/tool_executor.py's emit_event("tool.completed", ...) call sites.
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_BLOCKED = "blocked"


def timeline_root() -> Path:
    """Root directory for per-session timeline files (profile-safe)."""
    from hermes_constants import get_hermes_dir

    return get_hermes_dir("cache/timeline", "timeline_cache")


def _safe_session_id(session_id: Any) -> str:
    # alnum/-/_ only (no dots) — session_ids are uuid4-hex-shaped in
    # practice, and excluding dots means a hostile id like "../../etc"
    # can't even assemble a ".." substring, not just no path separator.
    s = str(session_id or "")
    cleaned = "".join(c for c in s if c.isalnum() or c in "-_")
    return cleaned or "unknown"


def _session_path(session_id: str, root: Optional[Path] = None) -> Path:
    base = root if root is not None else timeline_root()
    return base / f"{_safe_session_id(session_id)}.json"


def _digest(tool_name: str, args: Optional[Dict[str, Any]]) -> str:
    """Short, truncated, redacted preview of a tool call's primary argument.

    Never raises — an args shape build_tool_preview doesn't understand
    just yields an empty digest rather than breaking the timeline.
    """
    try:
        from agent.display import build_tool_preview
        from agent.redact import redact_sensitive_text

        preview = build_tool_preview(str(tool_name or ""), args or {})
        if not preview:
            return ""
        # Safety net: build_tool_preview only redacts browser_type's `text`
        # arg (via redact_tool_args_for_display) — every other tool's
        # preview can still carry a raw secret (e.g. a `terminal` command
        # with an Authorization header). This file is read back over the
        # API, so it gets the same general redaction sweep every other
        # secret-adjacent sink in the codebase already applies.
        return redact_sensitive_text(preview, force=True) or ""
    except Exception as exc:
        logger.debug("Session timeline args_digest failed for %s: %s", tool_name, exc)
        return ""


class _SessionState:
    """In-process ring buffer + step counter for ONE session_id."""

    __slots__ = ("entries", "next_step", "lock")

    def __init__(self) -> None:
        self.entries: Deque[Dict[str, Any]] = deque(maxlen=TIMELINE_MAX_STEPS)
        self.next_step: int = 0
        self.lock = threading.Lock()


_states: Dict[str, _SessionState] = {}
_states_lock = threading.Lock()


def _get_state(session_id: str) -> _SessionState:
    with _states_lock:
        state = _states.get(session_id)
        if state is None:
            state = _SessionState()
            _states[session_id] = state
        return state


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """tmp + os.replace — a reader never observes a partial file.

    Same pattern as hermes_cli/active_sessions.py's _write_entries and the
    durable action records in hermes_cli/web_server.py.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _flush(session_id: str, state: _SessionState, root: Optional[Path]) -> None:
    payload = {
        "session_id": session_id,
        "updated_at": time.time(),
        "steps": list(state.entries),
    }
    try:
        _write_atomic(_session_path(session_id, root), payload)
    except Exception as exc:
        logger.debug("Session timeline flush failed for %s: %s", session_id, exc)


def record_start(
    session_id: Optional[str],
    tool_call_id: Optional[str],
    tool_name: str,
    args: Optional[Dict[str, Any]] = None,
    *,
    started_at: Optional[float] = None,
    root: Optional[Path] = None,
) -> Optional[int]:
    """Record a step as newly started (``status="running"``).

    Returns the assigned ``step_n``, or ``None`` if recording failed /
    ``session_id`` was falsy. Best-effort: never raises.
    """
    if not session_id:
        return None
    try:
        state = _get_state(str(session_id))
        with state.lock:
            step_n = state.next_step
            state.next_step += 1
            entry: Dict[str, Any] = {
                "step_n": step_n,
                "tool": str(tool_name or "?"),
                "args_digest": _digest(tool_name, args),
                "action_id": str(tool_call_id) if tool_call_id else None,
                "started_at": started_at if started_at is not None else time.time(),
                "duration": None,
                "status": STATUS_RUNNING,
            }
            state.entries.append(entry)
            _flush(str(session_id), state, root)
            return step_n
    except Exception as exc:
        logger.debug("Session timeline record_start failed: %s", exc)
        return None


def record_end(
    session_id: Optional[str],
    tool_call_id: Optional[str],
    *,
    status: str,
    duration: Optional[float] = None,
    root: Optional[Path] = None,
) -> None:
    """Mark a previously-started step complete.

    Looks up the running entry by ``tool_call_id`` (the ``action_id`` stamped
    at ``record_start``), most-recent-first, so a matching id from an
    already-evicted step is never accidentally revived. If the step already
    scrolled out of the ring buffer, this is a silent no-op — there is
    nothing left to update, and that's fine for a bounded, best-effort view.
    Best-effort: never raises.
    """
    if not session_id:
        return
    try:
        state = _get_state(str(session_id))
        with state.lock:
            entry = None
            if tool_call_id:
                cid = str(tool_call_id)
                for candidate in reversed(state.entries):
                    if candidate.get("action_id") == cid and candidate.get("status") == STATUS_RUNNING:
                        entry = candidate
                        break
            if entry is not None:
                entry["status"] = status
                entry["duration"] = duration
            _flush(str(session_id), state, root)
    except Exception as exc:
        logger.debug("Session timeline record_end failed: %s", exc)


def read_timeline(session_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    """Read the file-backed timeline for a session — safe from any process.

    Returns ``{"session_id", "steps": [...], "running": bool}``. Missing or
    corrupt files degrade to an empty-but-valid timeline rather than
    raising, since this is read from request-handling code paths (the API
    route, the slash command) that must not 500/crash on a stale or
    half-written cache file.
    """
    path = _session_path(session_id, root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"session_id": session_id, "steps": [], "running": False}
    except Exception as exc:
        logger.debug("Session timeline read failed for %s: %s", session_id, exc)
        return {"session_id": session_id, "steps": [], "running": False}

    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list):
        steps = []
    steps = [s for s in steps if isinstance(s, dict)]
    running = any(s.get("status") == STATUS_RUNNING for s in steps)
    return {"session_id": session_id, "steps": steps, "running": running}


def clear_timeline(session_id: str, root: Optional[Path] = None) -> None:
    """Drop in-memory + on-disk state for a session. Best-effort; test/debug use."""
    try:
        with _states_lock:
            _states.pop(str(session_id), None)
        path = _session_path(session_id, root)
        path.unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("Session timeline clear failed for %s: %s", session_id, exc)
