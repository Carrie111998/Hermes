#!/usr/bin/env python3
"""Approval gate + pending store for Hermes self-updates.

Unlike memory/skills, updates have a single subsystem and no inline approval
path: when the gate is on, every mutating ``hermes update`` invocation stages a
pending request under ``<HERMES_HOME>/pending/updates/`` instead of applying the
update immediately. The pending request can later be reviewed with:

  * ``hermes update pending``
  * ``hermes update approve <id>``
  * ``hermes update reject <id>``
  * ``hermes update approval <on|off>``

and, from the interactive CLI, the matching slash commands under ``/update``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

SUBSYSTEM = "updates"
CONFIG_KEY = "apply_approval"

# Internal env toggle used only while replaying an already-approved pending
# update. Public users never need to set this by hand.
#
# This is process-global, which is unsafe in a long-lived multi-session
# process: child processes inherit it (a spawned `hermes update` re-entering
# the gate sees it disabled), and concurrent gateway sessions would see it
# too. The in-process replay path (hermes_cli.main.cmd_update) uses the
# thread-local approval_bypass() context manager below instead, and passes
# an explicit approved=True down the recursive call — BYPASS_ENV stays
# supported only as a backward-compatible fallback for external callers and
# tests. See GHSA-qg5c-hvr5-hjgr for the same bug class, already fixed for
# the approval/sudo callbacks in tools/terminal_tool.py.
BYPASS_ENV = "HERMES_UPDATE_APPROVAL_BYPASS"

_bypass_tls = threading.local()


@contextmanager
def approval_bypass():
    """Mark the update-approval gate as bypassed for the current thread only.

    Use as ``with approval_bypass(): cmd_update(replay_args, approved=True)``
    when replaying an already-approved pending update. Scoped to the calling
    thread so it can never leak into child processes or other concurrent
    sessions — see the BYPASS_ENV comment above.
    """
    prior = getattr(_bypass_tls, "active", False)
    _bypass_tls.active = True
    try:
        yield
    finally:
        _bypass_tls.active = prior


def apply_approval_enabled() -> bool:
    """Return whether ``updates.apply_approval`` is enabled.

    Defaults to ``True`` when unset or invalid, per the user-requested policy:
    self-updates require explicit approval unless the admin turns the gate off.
    """
    try:
        from hermes_cli.config import load_config, cfg_get

        cfg = load_config()
        raw = cfg_get(cfg, SUBSYSTEM, CONFIG_KEY, default=True)
    except Exception:
        return True
    return _normalize_enabled(raw)



def _normalize_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in {"on", "true", "yes", "1", "enable", "enabled", "approve"}:
            return True
        if norm in {"off", "false", "no", "0", "disable", "disabled"}:
            return False
    return True



def approval_bypass_active() -> bool:
    """True if the update-approval gate is bypassed for the current thread.

    Checks the thread-local flag first (set by approval_bypass()), then
    falls back to the process-global BYPASS_ENV for backward compatibility
    with external callers/tests. The thread-local check is what makes this
    safe in a multi-session process — see the BYPASS_ENV comment above.
    """
    if getattr(_bypass_tls, "active", False):
        return True
    return os.environ.get(BYPASS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}



def _pending_dir() -> Path:
    return get_hermes_home() / "pending" / SUBSYSTEM



def stage_update(payload: Dict[str, Any], *, summary: str, origin: str = "foreground") -> Dict[str, Any]:
    pid = uuid.uuid4().hex[:8]
    record = {
        "id": pid,
        "subsystem": SUBSYSTEM,
        "action": "update",
        "summary": (summary or "").strip(),
        "origin": origin or "foreground",
        "created_at": time.time(),
        "payload": payload,
    }
    d = _pending_dir()
    path = d / f"{pid}.json"
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        logger.error("Failed to stage pending update: %s", e, exc_info=True)
        raise RuntimeError(f"Could not write pending update record to {path}: {e}") from e

    if not path.exists():
        raise RuntimeError(
            f"Pending update record write reported success but {path} does not exist"
        )

    return record



def list_pending() -> List[Dict[str, Any]]:
    d = _pending_dir()
    if not d.exists():
        return []
    records: List[Dict[str, Any]] = []
    for p in d.glob("*.json"):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            logger.warning("Skipping unreadable pending update record: %s", p)
    records.sort(key=lambda r: r.get("created_at", 0))
    return records



def get_pending(pending_id: str) -> Optional[Dict[str, Any]]:
    path = _pending_dir() / f"{pending_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None



def discard_pending(pending_id: str) -> bool:
    """Remove a pending update record.

    Returns ``False`` only when no record for ``pending_id`` exists. A real
    I/O failure while removing an existing record raises ``RuntimeError``
    instead, so callers can distinguish "nothing to reject" from "rejection
    itself failed" rather than treating both as the same silent no-op.
    """
    path = _pending_dir() / f"{pending_id}.json"
    if not path.exists():
        return False
    try:
        path.unlink()
    except Exception as e:
        logger.error("Failed to discard pending update %s: %s", pending_id, e)
        raise RuntimeError(f"Could not remove pending update record {path}: {e}") from e

    if path.exists():
        raise RuntimeError(f"Unlink reported success but {path} still exists")

    return True



def pending_count() -> int:
    d = _pending_dir()
    if not d.exists():
        return 0
    try:
        return sum(1 for _ in d.glob("*.json"))
    except Exception:
        return 0



def update_summary(payload: Dict[str, Any]) -> str:
    branch = (payload.get("branch") or "main").strip() or "main"
    flags = []
    if payload.get("backup"):
        flags.append("full-backup")
    if payload.get("no_backup"):
        flags.append("no-backup")
    if payload.get("force"):
        flags.append("force")
    if payload.get("force_venv"):
        flags.append("force-venv")
    suffix = f" ({', '.join(flags)})" if flags else ""
    return f"update Hermes Agent from branch '{branch}'{suffix}"



def payload_from_args(args) -> Dict[str, Any]:
    return {
        "branch": getattr(args, "branch", None),
        "backup": bool(getattr(args, "backup", False)),
        "no_backup": bool(getattr(args, "no_backup", False)),
        "force": bool(getattr(args, "force", False)),
        "force_venv": bool(getattr(args, "force_venv", False)),
    }
