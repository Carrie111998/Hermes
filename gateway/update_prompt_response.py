"""Correlation-bound response writer for gateway update confirmations.

Platform callbacks are long-lived UI objects.  A button from an older update
must never authorize the prompt currently occupying ``.update_prompt.json``.
This module keeps the filesystem comparison and atomic response write identical
for Telegram, Discord, and typed-message fallbacks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _current_pending(home: Path) -> dict[str, Any] | None:
    for name in (".update_pending.claimed.json", ".update_pending.json"):
        value = _read_object(home / name)
        if value is not None:
            return value
    return None


def write_update_confirmation_response(
    control_home: str | os.PathLike[str],
    *,
    prompt_id: str,
    correlation_id: str,
    session_key: str,
    actor_id: str,
    answer: str,
) -> bool:
    """Atomically answer exactly one current ``update_confirmation`` prompt.

    Returns ``False`` for stale, replayed, malformed, cross-session, or
    cross-origin callbacks.  It never falls back to a raw answer format.
    """
    home = Path(control_home)
    prompt = _read_object(home / ".update_prompt.json")
    pending = _current_pending(home)
    normalized = str(answer).strip().lower()

    if normalized in {"y", "yes", "approve", "approved"}:
        normalized = "yes"
    elif normalized in {"n", "no", "deny", "denied"}:
        normalized = "no"
    else:
        return False

    if (
        not str(prompt_id)
        or not str(correlation_id)
        or not str(session_key)
        or not str(actor_id).strip()
    ):
        return False

    if not prompt or not pending or prompt.get("kind") != "update_confirmation":
        return False
    if str(prompt.get("id") or "") != str(prompt_id):
        return False
    if str(prompt.get("correlation_id") or "") != str(correlation_id):
        return False
    if str(pending.get("correlation_id") or "") != str(correlation_id):
        return False
    if str(pending.get("session_key") or "") != str(session_key):
        return False
    # The session key is not an authorization boundary: two actors can share
    # one chat/session, while an actor can also move between sessions.  Bind
    # the callback to the identity captured when /update created the prompt.
    expected_actor = pending.get("user_id")
    if expected_actor is None or str(expected_actor) != str(actor_id):
        return False

    context = prompt.get("context")
    if not isinstance(context, dict):
        return False
    for pending_key, context_key in (
        ("origin_profile", "origin_profile"),
        ("profile_home", "profile_home"),
        ("control_home", "control_home"),
        ("install_root", "install_root"),
        ("install_id", "install_id"),
    ):
        expected = str(pending.get(pending_key) or "")
        current = str(context.get(context_key) or "")
        if not expected or current != expected:
            return False

    response_path = home / ".update_response"
    if response_path.exists():
        return False

    payload = json.dumps(
        {
            "id": str(prompt_id),
            "correlation_id": str(correlation_id),
            "answer": normalized,
        },
        separators=(",", ":"),
    )
    tmp = home / f".update_response.{prompt_id}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # A hard link publishes the fully written inode only when no response
        # already exists; simultaneous duplicate callbacks cannot overwrite it.
        os.link(tmp, response_path)
        return True
    except (FileExistsError, OSError):
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
