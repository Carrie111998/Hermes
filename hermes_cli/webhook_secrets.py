"""Webhook secret-reference persistence and profile-scoped resolution.

Webhook route documents contain opaque environment-variable names only.  The
credential values live in the active profile's ``.env`` and are resolved via
the same fail-closed secret scope used by the gateway.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_SECRET_REF_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05


def validate_webhook_secret_ref(secret_ref: object) -> str:
    """Return a canonical reference or raise without echoing secret material."""
    if not isinstance(secret_ref, str):
        raise ValueError("webhook secret reference must be a string")
    ref = secret_ref.strip()
    if not _SECRET_REF_RE.fullmatch(ref):
        raise ValueError(
            "webhook secret reference must be an uppercase environment name"
        )
    return ref


def webhook_route_secret_ref(
    route_name: str,
    *,
    versioned: bool = False,
    namespace: str = "",
) -> str:
    """Build a collision-resistant reference for one route.

    Migrations use a deterministic suffix so retries are idempotent.  New or
    rotated subscriptions add a random version suffix so a failed route-file
    switch cannot change the credential used by the still-live old record.
    """
    if not isinstance(route_name, str) or not route_name:
        raise ValueError("webhook route name must be non-empty")
    normalized = re.sub(r"[^A-Z0-9_]", "_", route_name.upper()).strip("_")
    normalized = (normalized or "ROUTE")[:48]
    normalized_namespace = re.sub(
        r"[^A-Z0-9_]", "_", namespace.upper()
    ).strip("_")
    prefix = "WEBHOOK_ROUTE_"
    if normalized_namespace:
        prefix += f"{normalized_namespace[:24]}_"
    if versioned:
        suffix = secrets.token_hex(6).upper()
    else:
        suffix = hashlib.sha256(route_name.encode("utf-8")).hexdigest()[:12].upper()
    return f"{prefix}{normalized}_{suffix}"


def resolve_webhook_secret(secret_ref: object) -> str:
    """Resolve a reference from the active profile, failing closed on errors."""
    try:
        ref = validate_webhook_secret_ref(secret_ref)
        from agent.secret_scope import get_secret

        value = get_secret(ref, "")
    except Exception:
        return ""
    return str(value) if value else ""


def _try_lock(handle) -> bool:
    """Attempt one advisory lock acquisition without blocking."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                return False
            raise

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def webhook_secret_write_lock() -> Iterator[None]:
    """Serialize ``.env`` and route-store writers across threads/processes.

    This is a kernel-backed advisory lock, not a stale lockfile protocol.  A
    crashed process releases it automatically, and a slow live writer can
    never have its lock stolen merely because an mtime threshold elapsed.
    """
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / ".webhook-secrets.lock"
    with open(lock_path, "a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass

        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out waiting for webhook secret writer lock")
            time.sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(handle)


def store_webhook_secret_unlocked(secret_ref: str, value: str) -> None:
    """Persist and verify a secret while the caller owns the writer lock."""
    ref = validate_webhook_secret_ref(secret_ref)
    if not isinstance(value, str) or not value:
        raise ValueError("webhook secret value must be non-empty")

    from hermes_cli.config import get_env_value_prefer_dotenv, save_env_value

    save_env_value(ref, value)
    if get_env_value_prefer_dotenv(ref) != value:
        raise RuntimeError("webhook secret persistence verification failed")


def store_webhook_secret(secret_ref: str, value: str) -> None:
    """Persist and verify one webhook secret in the active profile."""
    with webhook_secret_write_lock():
        store_webhook_secret_unlocked(secret_ref, value)


__all__ = [
    "resolve_webhook_secret",
    "store_webhook_secret",
    "store_webhook_secret_unlocked",
    "validate_webhook_secret_ref",
    "webhook_route_secret_ref",
    "webhook_secret_write_lock",
]
