"""Hermes gateway integration boundary for ARCH-001 runtime state."""

from __future__ import annotations

from dataclasses import dataclass
import contextvars
import os
from pathlib import Path
import socket
import threading
from typing import Any, Optional
from uuid import uuid4

from hermes_constants import get_hermes_home
from hermes_cli.profiles import get_active_profile_name, get_profile_dir
from runtime_state import (
    RuntimeStateDB,
    CasResult,
    cas_claim_owner,
    cas_release_owner,
    cas_update_columns,
    create_approval_state,
    create_compression_state,
    create_session_state,
    create_task_state,
)


class RuntimeStateIntegrationError(RuntimeError):
    """The gateway cannot safely use its profile-scoped runtime state."""


@dataclass(frozen=True)
class RuntimeApprovalLease:
    approval_id: str
    owner: str
    owner_version: int


@dataclass(frozen=True)
class RuntimeStateContext:
    """Explicit profile/task context copied into gateway worker threads."""

    profile: "RuntimeStateProfile"
    session_id: str
    task_id: str


_runtime_state_context: contextvars.ContextVar[
    Optional[RuntimeStateContext]
] = contextvars.ContextVar("runtime_state_context", default=None)


def bind_runtime_state_context(
    profile: "RuntimeStateProfile", session_id: str, task_id: str
):
    return _runtime_state_context.set(
        RuntimeStateContext(profile, session_id, task_id)
    )


def reset_runtime_state_context(token) -> None:
    _runtime_state_context.reset(token)


def get_runtime_state_context() -> Optional[RuntimeStateContext]:
    return _runtime_state_context.get()


def _profile_name() -> str:
    raw_name = get_active_profile_name()
    # No configured profile keeps the historical default; an explicitly empty
    # profile is invalid and must fail closed before any state write.
    name = ("default" if raw_name is None else str(raw_name)).strip()
    if not name:
        raise RuntimeStateIntegrationError("active Hermes profile name is empty")
    return name


def _resolve_db_path(config: Any, profile_home: Path) -> Path:
    raw = getattr(config, "runtime_state_db_path", None)
    candidate = Path(raw) if raw else Path("runtime_state.db")
    if not candidate.is_absolute():
        candidate = profile_home / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(profile_home.resolve())
    except ValueError as exc:
        raise RuntimeStateIntegrationError(
            "runtime_state_db_path must remain inside the active profile home"
        ) from exc
    if resolved.name in {"", ".", ".."}:
        raise RuntimeStateIntegrationError("runtime_state_db_path must name a database file")
    return resolved


@dataclass(frozen=True)
class RuntimeTaskLease:
    task_id: str
    owner: str
    owner_version: int


class RuntimeStateProfile:
    """Thread-safe profile-scoped runtime-state repository."""

    def __init__(self, config: Any, profile_name: str, profile_home: Path) -> None:
        self.profile_name = profile_name
        self.profile_home = profile_home.resolve()
        self.db_path = _resolve_db_path(config, self.profile_home)
        self.db = RuntimeStateDB(self.db_path)
        self.owner = f"gateway:{socket.gethostname()}:{os.getpid()}"
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def ensure_session(self, session_id: str, user_id: Optional[str]) -> CasResult:
        with self._lock:
            result = create_session_state(
                self.db.connection,
                self.profile_name,
                session_id,
                user_id=user_id or "unknown",
                workspace=str(self.profile_home),
                target_host=socket.gethostname(),
                deployment_target="hermes-gateway",
            )
        if not result.success:
            raise RuntimeStateIntegrationError(
                f"runtime session creation failed: {result.error}"
            )
        return result

    def ensure_compression(self, session_id: str) -> CasResult:
        with self._lock:
            result = create_compression_state(
                self.db.connection,
                self.profile_name,
                session_id,
            )
        if not result.success:
            raise RuntimeStateIntegrationError(
                f"runtime compression state creation failed: {result.error}"
            )
        return result

    def begin_task(self, session_id: str) -> RuntimeTaskLease:
        task_id = f"gateway:{uuid4().hex}"
        with self._lock:
            created = create_task_state(
                self.db.connection,
                self.profile_name,
                task_id,
                session_id,
            )
            if not created.success:
                raise RuntimeStateIntegrationError(
                    f"runtime task creation failed: {created.error}"
                )
            claimed = cas_claim_owner(
                self.db.connection,
                "task_state",
                self.profile_name,
                task_id,
                self.owner,
                0,
            )
            if not claimed.success:
                raise RuntimeStateIntegrationError(
                    f"runtime task claim failed: {claimed.error}"
                )
            running = cas_update_columns(
                self.db.connection,
                "task_state",
                self.profile_name,
                task_id,
                self.owner,
                claimed.owner_version,
                {"status": "running"},
            )
            if not running.success:
                raise RuntimeStateIntegrationError(
                    f"runtime task start failed: {running.error}"
                )
            return RuntimeTaskLease(task_id, self.owner, running.owner_version)

    def begin_approval(
        self, session_id: str, task_id: str
    ) -> RuntimeApprovalLease:
        approval_id = f"gateway:{uuid4().hex}"
        with self._lock:
            created = create_approval_state(
                self.db.connection,
                self.profile_name,
                approval_id,
                session_id=session_id,
                task_id=task_id,
            )
            if not created.success:
                raise RuntimeStateIntegrationError(
                    f"runtime approval creation failed: {created.error}"
                )
            claimed = cas_claim_owner(
                self.db.connection,
                "approval_state",
                self.profile_name,
                approval_id,
                self.owner,
                0,
            )
            if not claimed.success:
                raise RuntimeStateIntegrationError(
                    f"runtime approval claim failed: {claimed.error}"
                )
            return RuntimeApprovalLease(
                approval_id, self.owner, claimed.owner_version
            )

    def finish_approval(self, lease: RuntimeApprovalLease, status: str) -> None:
        if status not in {"approved", "denied", "expired"}:
            raise RuntimeStateIntegrationError(
                f"invalid runtime approval status: {status}"
            )
        with self._lock:
            updated = cas_update_columns(
                self.db.connection,
                "approval_state",
                self.profile_name,
                lease.approval_id,
                lease.owner,
                lease.owner_version,
                {"approval_status": status},
            )
            if not updated.success:
                raise RuntimeStateIntegrationError(
                    f"runtime approval completion failed: {updated.error}"
                )
            released = cas_release_owner(
                self.db.connection,
                "approval_state",
                self.profile_name,
                lease.approval_id,
                lease.owner,
                updated.owner_version,
            )
            if not released.success:
                raise RuntimeStateIntegrationError(
                    f"runtime approval release failed: {released.error}"
                )

    def record_compression(self, session_id: str, status: str = "succeeded") -> None:
        """Record a completed compression callback through CAS ownership."""

        if status not in {"succeeded", "failed", "degraded", "disabled"}:
            raise RuntimeStateIntegrationError(
                f"invalid runtime compression status: {status}"
            )
        with self._lock:
            row = self.db.connection.execute(
                "SELECT owner_version FROM compression_state "
                "WHERE profile_name = ? AND session_id = ?",
                (self.profile_name, session_id),
            ).fetchone()
            if row is None:
                raise RuntimeStateIntegrationError(
                    "runtime compression row is missing"
                )
            claimed = cas_claim_owner(
                self.db.connection,
                "compression_state",
                self.profile_name,
                session_id,
                self.owner,
                int(row[0]),
            )
            if not claimed.success:
                raise RuntimeStateIntegrationError(
                    f"runtime compression claim failed: {claimed.error}"
                )
            updated = cas_update_columns(
                self.db.connection,
                "compression_state",
                self.profile_name,
                session_id,
                self.owner,
                claimed.owner_version,
                {"compression_status": status},
            )
            if not updated.success:
                raise RuntimeStateIntegrationError(
                    f"runtime compression update failed: {updated.error}"
                )
            released = cas_release_owner(
                self.db.connection,
                "compression_state",
                self.profile_name,
                session_id,
                self.owner,
                updated.owner_version,
            )
            if not released.success:
                raise RuntimeStateIntegrationError(
                    f"runtime compression release failed: {released.error}"
                )

    def finish_task(self, lease: RuntimeTaskLease, status: str) -> None:
        with self._lock:
            updated = cas_update_columns(
                self.db.connection,
                "task_state",
                self.profile_name,
                lease.task_id,
                lease.owner,
                lease.owner_version,
                {"status": status},
            )
            if not updated.success:
                raise RuntimeStateIntegrationError(
                    f"runtime task completion failed: {updated.error}"
                )
            released = cas_release_owner(
                self.db.connection,
                "task_state",
                self.profile_name,
                lease.task_id,
                lease.owner,
                updated.owner_version,
            )
            if not released.success:
                raise RuntimeStateIntegrationError(
                    f"runtime task release failed: {released.error}"
                )


class RuntimeStateManager:
    """Own profile repositories and perform gateway startup preflight."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._profiles: dict[str, RuntimeStateProfile] = {}
        self._lock = threading.RLock()

    def profile(self, profile_name: Optional[str] = None) -> RuntimeStateProfile:
        name = (profile_name or _profile_name()).strip()
        if not name:
            raise RuntimeStateIntegrationError("runtime profile name is empty")
        with self._lock:
            profile = self._profiles.get(name)
            if profile is None:
                home = (
                    get_hermes_home()
                    if name == _profile_name()
                    else get_profile_dir(name)
                )
                profile = RuntimeStateProfile(self.config, name, home)
                self._profiles[name] = profile
            return profile

    def preflight(self) -> RuntimeStateProfile:
        """Open the active profile before any gateway adapter starts."""

        return self.profile()

    def close(self) -> None:
        with self._lock:
            profiles = list(self._profiles.values())
            self._profiles.clear()
        for profile in profiles:
            profile.close()
