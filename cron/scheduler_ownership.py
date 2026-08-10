"""Single-owner lease for the in-process cron scheduler.

Canonical gateway and local-primary Desktop fallback both participate in this
lease so only one process can run the tick loop for a given HERMES_HOME at a
time. ``cron/.tick.lock`` remains at-most-once serialization inside a tick, not
ownership admission.

Design:
- One process-lifetime exclusive lease file under ``$HERMES_HOME/cron/``.
- fcntl flock when available; O_EXCL create on no-fcntl hosts (Windows).
- Desktop fallback acquires only after proving the canonical gateway is absent,
  then re-checks gateway liveness while holding the lease.
- Gateway waits briefly for a desktop holder to yield once the gateway is live,
  so dual tick loops cannot coexist during handoff.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

_LEASE_NAME = "scheduler-owner.lease"
_lease_fh = None  # process-lifetime exclusive lease handle
_lease_fallback_path: Optional[Path] = None
_lease_role: Optional[str] = None


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_process_hermes_home

        return Path(get_process_hermes_home())
    except Exception:
        try:
            from hermes_constants import get_hermes_home

            return Path(get_hermes_home())
        except Exception:
            return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def scheduler_owner_lease_path(hermes_home: Optional[Path] = None) -> Path:
    home = Path(hermes_home) if hermes_home is not None else _hermes_home()
    return home / "cron" / _LEASE_NAME


def get_scheduler_owner_role() -> Optional[str]:
    """Return the role that currently holds the in-process owner lease, if any."""
    return _lease_role if _lease_fh is not None else None


def try_acquire_scheduler_ownership(role: str) -> bool:
    """Acquire the single scheduler-owner lease for *role*.

    Returns True when this process already holds the lease or newly acquired it.
    Returns False when another live process owns the lease.
    """
    global _lease_fh, _lease_fallback_path, _lease_role

    if not role:
        raise ValueError("scheduler ownership role is required")

    if _lease_fh is not None:
        _lease_role = role
        return True

    lease_path = scheduler_owner_lease_path()
    try:
        lease_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        _log.debug("scheduler owner lease directory creation failed", exc_info=True)
        return False

    try:
        import fcntl
    except ImportError:
        # Windows/no-fcntl: O_EXCL create; O_TEMPORARY removes on close when available.
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_TEMPORARY", 0)
        try:
            fd = os.open(str(lease_path), flags, 0o600)
        except FileExistsError:
            return False
        except OSError:
            return False
        fh = os.fdopen(fd, "w", encoding="utf-8")
        _lease_fallback_path = lease_path
    except Exception:
        _log.debug("scheduler owner lease backend failed", exc_info=True)
        return False
    else:
        try:
            fh = open(lease_path, "a+", encoding="utf-8")
        except OSError:
            _log.debug("scheduler owner lease open failed", exc_info=True)
            return False
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass
            return False

    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()} {role}\n")
        fh.flush()
    except OSError:
        pass

    _lease_fh = fh
    _lease_role = role
    return True


def release_scheduler_ownership() -> None:
    """Release the scheduler-owner lease when held by this process."""
    global _lease_fh, _lease_fallback_path, _lease_role

    fh = _lease_fh
    fallback_path = _lease_fallback_path
    _lease_fh = None
    _lease_fallback_path = None
    _lease_role = None
    if fh is None:
        return

    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        fh.close()
    except OSError:
        pass
    if fallback_path is not None:
        try:
            fallback_path.unlink(missing_ok=True)
        except OSError:
            _log.debug("scheduler owner fallback lease cleanup failed", exc_info=True)


def wait_for_scheduler_ownership(
    role: str,
    *,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.1,
) -> bool:
    """Block until the owner lease is acquired or *timeout_seconds* elapses.

    Used by the canonical gateway so a desktop fallback that still holds the
    lease can observe gateway liveness, stop, and release before dual loops run.
    """
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    poll = max(0.01, float(poll_seconds))
    while True:
        if try_acquire_scheduler_ownership(role):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
