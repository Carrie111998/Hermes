"""Profile-wide admission for actual delegated child agents."""

from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from hermes_constants import get_hermes_home

DEFAULT_PROFILE_REAL_CHILD_CEILING = 15

_lock = threading.Lock()
_leases_by_profile: dict[str, dict[str, int]] = {}
_lease_profile: dict[str, str] = {}
_profile_context: ContextVar[str] = ContextVar(
    "delegation_admission_profile",
    default="",
)


def _profile_key() -> str:
    """Return the active profile's resolved Hermes home."""
    return _profile_context.get() or str(get_hermes_home().resolve())


@dataclass(frozen=True)
class Admission:
    accepted: bool
    lease_id: str = ""
    real_children: int = 0
    ceiling: int = DEFAULT_PROFILE_REAL_CHILD_CEILING
    error: str = ""


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def active_real_children() -> int:
    """Return the number of currently admitted child agents."""
    profile_key = _profile_key()
    with _lock:
        return sum(_leases_by_profile.get(profile_key, {}).values())


def try_acquire(*, real_children: int, ceiling: int) -> Admission:
    """Atomically admit ``real_children`` or reject the entire request."""
    requested = int(real_children or 0)
    resolved_ceiling = _positive_int(ceiling, DEFAULT_PROFILE_REAL_CHILD_CEILING)
    if requested <= 0:
        return Admission(
            accepted=False,
            real_children=requested,
            ceiling=resolved_ceiling,
            error="real_children must be positive",
        )

    profile_key = _profile_key()
    with _lock:
        profile_leases = _leases_by_profile.setdefault(profile_key, {})
        active = sum(profile_leases.values())
        if active + requested > resolved_ceiling:
            return Admission(
                accepted=False,
                real_children=requested,
                ceiling=resolved_ceiling,
                error=(
                    "Profile real-child delegation capacity reached: "
                    f"{active}/{resolved_ceiling} active; requested {requested}."
                ),
            )
        lease_id = f"childlease_{uuid.uuid4().hex[:12]}"
        profile_leases[lease_id] = requested
        _lease_profile[lease_id] = profile_key

    return Admission(
        accepted=True,
        lease_id=lease_id,
        real_children=requested,
        ceiling=resolved_ceiling,
    )


def release(lease_id: str) -> bool:
    """Release a prior admission lease. Repeated releases are no-ops."""
    lease_id = str(lease_id or "")
    with _lock:
        profile_key = _lease_profile.pop(lease_id, "")
        if not profile_key:
            return False
        profile_leases = _leases_by_profile.get(profile_key)
        if profile_leases is None:
            return False
        removed = profile_leases.pop(lease_id, None) is not None
        if not profile_leases:
            _leases_by_profile.pop(profile_key, None)
        return removed


def _reset_for_tests() -> None:
    with _lock:
        _leases_by_profile.clear()
        _lease_profile.clear()
