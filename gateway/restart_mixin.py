"""Gateway /restart notification marker helpers (shard s1 cluster c22).

Extracted verbatim from gateway/run.py (shard s1 cluster c22, wave 1).
Module-level functions re-imported into gateway.run so every call site
and monkeypatch contract (``gateway.run._restart_notification_pending``
etc.) is unchanged.  ``_hermes_home`` stays in gateway.run (tests
monkeypatch it on that module); moved functions read it via a deferred
import so the monkeypatch is seen at call time.
"""
from __future__ import annotations

from pathlib import Path


def _restart_notification_pending() -> bool:
    """Return True when a /restart completion marker is waiting to be delivered."""
    from gateway.run import (
        _hermes_home,
    )
    return (_hermes_home / ".restart_notify.json").exists()


def _planned_restart_notification_path() -> Path:
    from gateway.run import (
        _hermes_home,
    )
    return _hermes_home / ".restart_pending.json"


def _planned_restart_notification_pending() -> bool:
    """Return True when a non-chat planned restart should notify home channels."""
    return _planned_restart_notification_path().exists()


def _clear_planned_restart_notification() -> None:
    _planned_restart_notification_path().unlink(missing_ok=True)
