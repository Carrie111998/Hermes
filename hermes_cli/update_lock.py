"""Cross-process mutual exclusion for in-flight Hermes updates.

Three different surfaces can start an update of the same install tree:

* ``hermes update`` from a terminal,
* the dashboard's Update button (``POST /api/hermes/update`` →
  ``_spawn_hermes_action(["update"])``, detached),
* the desktop's Update button, which hands off to the Tauri
  ``hermes-setup --update`` and, on its failure screen, to install-mode
  bootstrap (``install.ps1`` / ``install.sh``).

Until now only the Tauri updater published an "update in progress" marker
(``UpdateMarkerGuard`` in ``apps/bootstrap-installer/src-tauri/src/update.rs``),
and only the Electron desktop consumed it (``electron/update-marker.ts``, to
gate local backend startup). Nothing stopped two *updaters* from running at
once — so a dashboard-spawned ``hermes update`` and an installer-driven
``git checkout`` could mutate the same checkout concurrently, rewriting source
under a live interpreter and leaving the tree half-updated.

This module makes that same marker the single lock for **all** update
entrypoints instead of adding a fourth mechanism. Format and location are
unchanged and remain byte-compatible with the Rust and Electron readers:

    <HERMES_HOME>/.hermes-update-in-progress   body: "<pid>\\n<started_at_unix>"

A marker only counts as a live update when its pid is alive AND it is younger
than :data:`UPDATE_MARKER_MAX_AGE_MS` — mirroring ``readLiveUpdateMarker`` so a
crashed updater self-heals instead of wedging every future update. A stale
marker is removed on read by whoever notices it first.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep in sync with UPDATE_MARKER_MAX_AGE_MS in
# apps/desktop/electron/update-marker.ts — the same marker is read by both, and
# a shorter ceiling here would let Python steal a lock Electron still considers
# live. A full update (git pull + uv sync + desktop rebuild) is minutes.
UPDATE_MARKER_MAX_AGE_SECONDS = 20 * 60

MARKER_NAME = ".hermes-update-in-progress"

# Exit code meaning "another updater/instance owns this install right now".
# Already the de-facto contract: the Windows shim + venv-holder guards in
# _cmd_update_impl exit 2, and the Tauri updater matches on it
# (UPDATE_EXIT_CONCURRENT in apps/bootstrap-installer/src-tauri/src/update.rs)
# to show "Hermes is still running" instead of a generic failure. Naming it
# here keeps the concurrent-update refusal on that same understood contract.
UPDATE_EXIT_CONCURRENT = 2


def update_marker_path() -> Path:
    """Path of the shared update marker.

    Uses the *process* Hermes home (never the context-local profile override):
    the Rust updater resolves ``$HERMES_HOME`` or the platform default, and the
    desktop pins that same value into the updater's env. A profile-scoped path
    here would put the lock somewhere the other two owners never look.
    """
    from hermes_constants import get_process_hermes_home

    return get_process_hermes_home() / MARKER_NAME


def _pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` currently exists.

    Delegates to :func:`gateway.status._pid_exists`, the project's existing
    no-kill probe. Do NOT hand-roll this with ``os.kill(pid, 0)``: on Windows
    that is not a no-op — CPython routes ``sig=0`` to
    ``GenerateConsoleCtrlEvent``, which Ctrl+C's the target's whole console
    process group (bpo-14484). A liveness check that killed the updater it was
    asking about would be a spectacular way to fix a concurrency bug.

    Any pid we cannot evaluate counts as dead: a corrupt marker must not wedge
    the lock forever.
    """
    if pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(pid))
    except Exception as exc:
        # Import failure or an unusable pid (e.g. larger than the platform's
        # pid_t). Treat the marker as stale rather than blocking updates.
        logger.debug("Could not probe pid %s: %s", pid, exc)
        return False


@dataclass(frozen=True)
class UpdateHolder:
    """A confirmed-live update currently holding the lock."""

    pid: int
    age_seconds: float


def read_live_update(*, path: Path | None = None) -> UpdateHolder | None:
    """Return the live update holding the lock, or ``None``.

    Mirrors ``readLiveUpdateMarker`` in ``electron/update-marker.ts``: absent,
    unreadable, malformed, dead-pid, and past-the-ceiling all mean "no live
    update", and a stale marker file is deleted so it can't strand future runs.
    Never raises.
    """
    marker = path or update_marker_path()
    try:
        raw = marker.read_text(encoding="utf-8")
    except OSError:
        return None  # absent or unreadable => no live update

    lines = raw.splitlines()
    try:
        pid = int(lines[0].strip())
    except (IndexError, ValueError):
        pid = -1
    try:
        started_at = float(lines[1].strip())
    except (IndexError, ValueError):
        started_at = float("-inf")

    age = time.time() - started_at
    if not _pid_alive(pid) or age > UPDATE_MARKER_MAX_AGE_SECONDS:
        try:
            marker.unlink()
        except OSError:
            pass
        return None

    return UpdateHolder(pid=pid, age_seconds=age)


def _holder_is_packaged_updater_ancestor(
    holder: UpdateHolder, *, marker_path: Path
) -> bool:
    """True only for the packaged updater's live ancestor-owned marker.

    Windows venv/distlib shims can put one or more launcher processes between
    ``Hermes-Setup`` and the Python process running ``hermes update``, so the
    owner need not be our direct parent. Ancestry alone is insufficient,
    though: an unrelated ancestor or an install-mode helper must never bypass
    mutual exclusion. Require the marker owner to be the stable updater executable
    beside the marker and to be running the internal ``--update`` mode. Fail
    closed when process identity or arguments cannot be inspected.
    """
    updater_name = "hermes-setup.exe" if os.name == "nt" else "hermes-setup"
    expected_exe = marker_path.parent / updater_name
    try:
        import psutil

        owner = next(
            (parent for parent in psutil.Process().parents() if parent.pid == holder.pid),
            None,
        )
        if owner is None:
            return False
        actual_norm = os.path.normcase(str(Path(owner.exe()).resolve()))
        expected_norm = os.path.normcase(str(expected_exe.resolve()))
        identity_matches = actual_norm == expected_norm
        update_mode = "--update" in owner.cmdline()[1:]
        return identity_matches and update_mode
    except Exception as exc:
        logger.debug("Could not verify packaged updater ancestry: %s", exc)
        return False


def describe_holder(holder: UpdateHolder) -> str:
    """One-line, user-facing explanation of who holds the update lock."""
    minutes, seconds = divmod(int(max(holder.age_seconds, 0)), 60)
    elapsed = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    return (
        f"✗ Another Hermes update is already running (PID {holder.pid}, "
        f"started {elapsed} ago).\n"
        "\n"
        "  Two updates mutating the same checkout corrupt it: one rewrites\n"
        "  source while the other is mid-install. Wait for it to finish, or\n"
        "  close the window/dashboard tab that started it, then retry."
    )


class UpdateLock:
    """Context manager owning the shared update marker for this process.

    ``acquired`` is True only when this process wrote and owns the marker. A
    descendant of the live marker owner may borrow that ancestor-owned lock
    (the Tauri updater → launcher shim → ``hermes update`` handoff); borrowing
    returns True from :meth:`acquire` but leaves ``acquired`` False so the child
    never removes the parent's marker before the rebuild stage. Every other
    live owner is refused. Releasing only removes the marker when *we* still own
    it, so a marker rewritten by a handoff partner is never deleted out from
    under its new owner.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or update_marker_path()
        self.acquired = False
        self.holder: UpdateHolder | None = None

    def acquire(self) -> bool:
        """Claim the lock. Returns False (and sets ``holder``) if it's taken."""
        existing = read_live_update(path=self.path)
        if existing is not None:
            if _holder_is_packaged_updater_ancestor(existing, marker_path=self.path):
                # The packaged updater owns the cross-process marker while a
                # descendant ``hermes update`` process mutates the checkout.
                # Borrow without rewriting it: the ancestor must keep the
                # Electron launch gate closed through the later rebuild stage.
                logger.debug(
                    "Borrowing update marker owned by ancestor pid %s", existing.pid
                )
                return True
            self.holder = existing
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                f"{os.getpid()}\n{int(time.time())}\n", encoding="utf-8"
            )
        except OSError as exc:
            # Best-effort, exactly like the Rust guard: an unwritable marker
            # must not block the update itself (that would be a worse failure
            # than the race it prevents). Degrade to the pre-lock behavior.
            logger.debug("Could not write update marker %s: %s", self.path, exc)
            return True
        self.acquired = True
        return True

    def release(self) -> None:
        """Drop the marker if this process still owns it. Never raises."""
        if not self.acquired:
            return
        self.acquired = False
        try:
            raw = self.path.read_text(encoding="utf-8")
            owner = int(raw.splitlines()[0].strip())
        except (OSError, IndexError, ValueError):
            return
        if owner != os.getpid():
            # A handoff partner took ownership (e.g. the Tauri updater wrote
            # its own pid). Leave it alone — it's still a live update.
            return
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "UpdateLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
