"""Cross-process serialization for the mutable Desktop build preflight."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import IO


class DesktopBuildLock:
    """Advisory lock held while npm installs and packages Hermes Desktop.

    ``node_modules`` and ``apps/desktop/release`` are checkout-scoped even
    when two commands use different Hermes profiles.  The lock therefore
    lives under the profile-common Hermes root and is keyed by the resolved
    checkout path.  Keeping it out of the checkout preserves ``--skip-build``
    launches from read-only/prebuilt source trees.  The open handle owns the
    lock, so the OS releases it automatically if a builder crashes.
    """

    def __init__(self, project_root: Path) -> None:
        from hermes_constants import get_default_hermes_root

        resolved = os.path.normcase(str(project_root.resolve(strict=False)))
        checkout_key = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]
        self.path = get_default_hermes_root() / "locks" / f"desktop-build-{checkout_key}.lock"
        self._handle: IO[str] | None = None

    def acquire(self) -> bool:
        """Try to acquire the lock without waiting.

        Returns ``False`` only when another process owns the lock.  Filesystem
        errors propagate so callers fail explicitly instead of silently
        falling back to the corrupting concurrent behavior.
        """
        if self._handle is not None:
            return True

        from gateway.status import _try_acquire_file_lock

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if not _try_acquire_file_lock(handle):
            handle.close()
            return False

        self._handle = handle
        return True

    def release(self) -> None:
        """Release the lock when held.  Safe to call more than once."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None

        try:
            from gateway.status import _release_file_lock

            _release_file_lock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "DesktopBuildLock":
        if not self.acquire():
            raise RuntimeError("desktop build lock is already held")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
