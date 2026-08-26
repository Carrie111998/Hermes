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

A marker counts as a live update while its pid is alive. Elapsed age is retained
for diagnostics but never authorizes a second updater to steal a live owner's
lock. A dead or malformed marker is removed on read by whoever notices it first,
so a crashed updater still self-heals instead of wedging every future update.

One layering wrinkle: the Tauri updater holds this marker for its WHOLE run and
then spawns ``hermes update`` as a child stage. Without a handoff the child
sees its own parent's live marker and refuses — the GUI update deadlocks
against itself on every attempt ("Hermes is still running", retry forever).
Two mechanisms recognize the orchestrating parent, and either suffices:

* The updater exports :data:`HANDOFF_PID_ENV` naming its own pid, and
  ``acquire`` treats a live holder matching that pid as the lock we are
  already running under. The env var alone grants nothing: the pid must also
  be the live marker owner, so a stale or forged value cannot bypass the lock.
* A live holder that is a *process ancestor* of ours is likewise our own
  orchestrator. This is the load-bearing path for the fleet: the staged
  ``hermes-setup`` binary under ``~/.hermes`` is only refreshed by a full
  installer run (``copy_self_to_hermes_home`` deliberately no-ops during
  ``--update``), so every desktop whose staged updater predates the
  HANDOFF_PID_ENV export runs an old parent against a new child. Without the
  ancestry check those users get exit 2 ("Hermes is still running") on every
  GUI update forever, with no Hermes process actually running.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MARKER_NAME = ".hermes-update-in-progress"
_OWNERSHIP_MUTEX_NAME = "Global\\HermesUpdateMarkerOwnership"
_fallback_mutex = threading.RLock()


@contextlib.contextmanager
def _ownership_transaction():
    """Serialize marker inspect/reclaim/publish across Windows entrypoints."""
    if os.name != "nt":
        with _fallback_mutex:
            yield
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateMutexW(None, False, _OWNERSHIP_MUTEX_NAME)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Could not create update ownership mutex")
    acquired = False
    try:
        result = kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        if result not in (0, 0x80):
            raise OSError(ctypes.get_last_error(), "Could not acquire update ownership mutex")
        acquired = True
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)

# Set by an orchestrating updater (the Tauri `hermes-setup --update` flow) to
# its own pid before spawning `hermes update` as a child stage. The parent
# holds the marker for its whole run, so without this the child refuses its
# own parent's lock and the GUI update can never complete. See update_child_env
# in apps/bootstrap-installer/src-tauri/src/update.rs — keep the name in sync.
HANDOFF_PID_ENV = "HERMES_UPDATE_HANDOFF_PID"

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


def _handoff_pid() -> int | None:
    """Pid of the orchestrating updater that spawned us, if any.

    Read from :data:`HANDOFF_PID_ENV`. Malformed values count as absent —
    a broken handoff must fall back to the normal refusal, never crash.
    """
    raw = os.environ.get(HANDOFF_PID_ENV, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _is_ancestor_pid(pid: int) -> bool:
    """True when ``pid`` is a live ancestor (parent chain) of this process.

    The orchestrating updater spawns ``hermes update`` as a (grand)child, so a
    live marker owned by one of our ancestors can only be the claim we are
    already running under — an unrelated concurrent updater is never in our
    parent chain. This heals the fleet of staged ``hermes-setup`` binaries
    that predate the HANDOFF_PID_ENV export and can never send it.

    Never includes our own pid, and any failure counts as "not an ancestor":
    an unprovable ancestry must fall back to the normal refusal.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return any(parent.pid == pid for parent in psutil.Process().parents())
    except Exception as exc:
        logger.debug("Could not walk process ancestry for pid %s: %s", pid, exc)
        return False


@dataclass(frozen=True)
class UpdateHolder:
    """A confirmed-live update currently holding the lock."""

    pid: int
    age_seconds: float


def _read_live_update_unlocked(marker: Path) -> UpdateHolder | None:
    """Return the live update holding the lock, or ``None``.

    Mirrors ``readLiveUpdateMarker`` in ``electron/update-marker.ts``: absent,
    unreadable, malformed, and dead-pid markers mean "no live update", and a
    dead marker file is deleted so it can't strand future runs. Elapsed age is
    diagnostic only: a confirmed-live owner keeps the operation lock until it
    exits or releases it. Never raises.
    """
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
        started_at = float("nan")

    age = time.time() - started_at
    if not math.isfinite(started_at) or not _pid_alive(pid):
        try:
            marker.unlink()
        except OSError:
            pass
        return None

    return UpdateHolder(pid=pid, age_seconds=age)


def read_live_update(*, path: Path | None = None) -> UpdateHolder | None:
    """Return the live owner while atomically pruning stale diagnostics."""
    marker = path or update_marker_path()
    with _ownership_transaction():
        return _read_live_update_unlocked(marker)


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

    ``acquired`` is False when another live update already holds it — callers
    decide whether that's a hard refusal (CLI/dashboard) or a wait. Releasing
    only removes the marker when *we* still own it, so a marker rewritten by a
    handoff partner (the Tauri updater overwrites it with its own pid) is never
    deleted out from under its new owner.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or update_marker_path()
        self.acquired = False
        self.holder: UpdateHolder | None = None
        self.failure_reason: str | None = None

    def _fail(self, action: str, exc: BaseException) -> bool:
        self.failure_reason = (
            f"✗ Hermes could not claim update ownership at {self.path} "
            f"while {action}: {exc}.\n\n"
            "  No update files were changed. Check access to the Hermes home "
            "directory, then retry."
        )
        logger.warning(
            "Could not claim update ownership at %s while %s: %s",
            self.path,
            action,
            exc,
        )
        return False

    def acquire(self) -> bool:
        """Claim the lock. Returns False (and sets ``holder``) if it's taken.

        A live holder whose pid matches :data:`HANDOFF_PID_ENV` — or is a
        process ancestor of ours — is our own orchestrating parent (the Tauri
        updater spawning `hermes update` as a stage): we run under ITS claim
        rather than refusing or re-writing the marker, and ``release`` leaves
        the parent's marker untouched. The ancestry path exists because staged
        updaters older than the HANDOFF_PID_ENV export never send the env var.
        """
        self.acquired = False
        self.holder = None
        self.failure_reason = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._fail("creating the marker directory", exc)

        try:
            with _ownership_transaction():
                existing = _read_live_update_unlocked(self.path)
                if existing is not None:
                    if existing.pid == _handoff_pid() or _is_ancestor_pid(existing.pid):
                        return True
                    self.holder = existing
                    return False
                claim = self.path.with_name(
                    f"{self.path.name}.claim-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
                )
                fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as marker:
                    marker.write(f"{os.getpid()}\n{int(time.time())}\n")
                try:
                    os.link(claim, self.path)
                finally:
                    claim.unlink(missing_ok=True)
                self.acquired = True
                return True
        except OSError as exc:
            return self._fail("publishing the ownership marker", exc)

    def release(self) -> None:
        """Drop the marker if this process still owns it. Never raises."""
        if not self.acquired:
            return
        self.acquired = False
        with _ownership_transaction():
            try:
                raw = self.path.read_text(encoding="utf-8")
                owner = int(raw.splitlines()[0].strip())
            except (OSError, IndexError, ValueError):
                return
            if owner != os.getpid():
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
