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
entrypoints instead of adding a fourth mechanism. The two-line format remains
byte-compatible with the Rust and Electron readers. The location is the
install-wide Hermes root, even when a command originates from a named profile:

    <HERMES_ROOT>/.hermes-update-in-progress   body: "<pid>\\n<lease_at_unix>"

A well-formed marker counts as a live update whenever its pid is alive. Its
second line is a refreshed lease timestamp for compatibility and diagnostics,
but a clock jump, suspend, or missed heartbeat must never admit a second
checkout mutator while the owner still exists. Well-formed, confirmed-dead
claims are cleaned under an owner-checked mutex; malformed or unreadable state
fails closed. Heartbeat and explicit takeover writes publish only complete
atomic replacements, so a former owner cannot overwrite a handoff.

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

import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep this nominal diagnostic threshold in sync with
# UPDATE_MARKER_MAX_AGE_MS in apps/desktop/electron/update-marker.ts. A live
# PID remains authoritative after this age; heartbeats normally keep it fresh.
UPDATE_MARKER_MAX_AGE_SECONDS = 20 * 60

# Keep comfortably below the shared readers' 20-minute stale ceiling. This is
# public so the Rust updater can use the same cadence.
UPDATE_MARKER_HEARTBEAT_SECONDS = 30
MARKER_MUTEX_TIMEOUT_SECONDS = 2.0

MARKER_NAME = ".hermes-update-in-progress"

# Set by an orchestrating updater (the Tauri `hermes-setup --update` flow) to
# its own pid before spawning `hermes update` as a child stage. The parent
# holds the marker for its whole run, so without this the child refuses its
# own parent's lock and the GUI update can never complete. See update_child_env
# in apps/bootstrap-installer/src-tauri/src/update.rs — keep the name in sync.
HANDOFF_PID_ENV = "HERMES_UPDATE_HANDOFF_PID"

# An isolated rollout coordinator is a different process from the CLI that
# prepared it, but it must continue under that parent's already-held update
# lock. Unlike HANDOFF_PID_ENV, which intentionally leaves ownership with the
# Tauri parent, this opt-in variable authorizes an explicit atomic takeover.
# It grants nothing unless it names the confirmed-live marker owner.
COORDINATOR_TAKEOVER_PID_ENV = "HERMES_UPDATE_COORDINATOR_TAKEOVER_PID"

# Exit code meaning "another updater/instance owns this install right now".
# Already the de-facto contract: the Windows shim + venv-holder guards in
# _cmd_update_impl exit 2, and the Tauri updater matches on it
# (UPDATE_EXIT_CONCURRENT in apps/bootstrap-installer/src-tauri/src/update.rs)
# to show "Hermes is still running" instead of a generic failure. Naming it
# here keeps the concurrent-update refusal on that same understood contract.
UPDATE_EXIT_CONCURRENT = 2

_MAX_U32 = (1 << 32) - 1
_MAX_SAFE_WIRE_INTEGER = (1 << 53) - 1
_MARKER_WIRE_RE = re.compile(r"(?P<pid>[1-9][0-9]*)\r?\n(?P<lease>[0-9]+)(?:\r?\n)?\Z")


def update_marker_path() -> Path:
    """Path of the install-wide shared update marker.

    Named profiles have distinct process homes (``<root>/profiles/<name>``)
    but all mutate the same checkout. Resolve the default Hermes *root*, not
    the process/profile home, so profiles cannot acquire independent locks
    over one install. Custom and Docker roots remain custom roots.
    """
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root() / MARKER_NAME


def _pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` currently exists.

    This safety boundary imports nothing from the changing checkout. POSIX uses
    the stdlib signal-0 probe. Windows uses OpenProcess/GetExitCodeProcess
    because CPython's signal-0 path can signal the target console (bpo-14484).

    Permission denied or an indeterminate platform error counts as alive:
    liveness uncertainty must fail closed, never prune a possibly-live claim.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            query_limited_information = 0x1000
            still_active = 259
            invalid_parameter = 87
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(query_limited_information, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER is Windows' no-such-process result.
                # Access denied and unknown failures stay conservatively live.
                return ctypes.get_last_error() != invalid_parameter
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except (OverflowError, ValueError):
            return False
        except Exception as exc:
            logger.debug("Indeterminate Windows pid probe for %s: %s", pid, exc)
            return True

    try:
        os.kill(
            pid, 0
        )  # windows-footgun: ok — Windows returns above; this is POSIX-only
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OverflowError, ValueError):
        return False
    except OSError as exc:
        logger.debug("Indeterminate POSIX pid probe for %s: %s", pid, exc)
        return True


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


def _coordinator_takeover_pid() -> int | None:
    """Explicit live-owner pid an isolated coordinator may take over."""
    raw = os.environ.get(COORDINATOR_TAKEOVER_PID_ENV, "").strip()
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
    """A confirmed-live owner, or a conservative unresolved marker claim."""

    pid: int | None
    age_seconds: float
    unavailable_reason: str | None = None


class UpdateLockUnavailable(RuntimeError):
    """The updater could not prove exclusive ownership of the install."""


def _write_all(fd: int, payload: bytes) -> None:
    """Write every payload byte, tolerating interrupts and short writes."""
    remaining = memoryview(payload)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("short write while publishing update marker")
        remaining = remaining[written:]


def _open_marker_mutex_no_follow(path: Path) -> int:
    """Open the mutex without following a symlink or Windows reparse point."""

    flags = os.O_RDWR | os.O_CREAT
    if sys.platform != "win32":
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags, 0o600)

    import ctypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_always = 4
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    file_attribute_reparse_point = 0x00000400
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(
        str(path),
        generic_read | generic_write,
        share_all,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.WinError(error))
    fd: int | None = None
    transferred = False
    try:
        import msvcrt

        fd = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_BINARY)
        transferred = True
        metadata = os.fstat(fd)
        if bool(
            getattr(metadata, "st_file_attributes", 0) & file_attribute_reparse_point
        ):
            os.close(fd)
            fd = None
            raise OSError(0, f"mutex path is a reparse point: {path}")
        assert fd is not None
        return fd
    except BaseException:
        # open_osfhandle transfers ownership only when it succeeds.  Close the
        # matching resource for either the CRT fd or the raw Win32 handle.
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        elif not transferred:
            close_handle(handle)
        raise


class _MarkerMutex:
    """Short cross-process mutex for Python marker read/modify/write cycles.

    The public marker must remain the exact two-line file consumed by old
    Electron and Rust builds, so a token cannot be added to its wire format.
    This sidecar serializes Python and Rust marker mutations. Electron only
    publishes a complete initial claim without replacing an existing marker,
    so it does not need to participate in owner-replacement cycles.

    Failure is surfaced to the caller. Mutation entrypoints fail closed when
    exclusivity cannot be proved; silently proceeding would recreate the
    checkout-corruption race this lock exists to prevent.
    """

    def __init__(self, marker: Path) -> None:
        self.path = marker.with_name(f"{marker.name}.mutex")
        self.fd: int | None = None
        self.locked = False

    def __enter__(self) -> "_MarkerMutex":
        try:
            from hermes_cli.update_rollout import validate_no_reparse_topology

            validate_no_reparse_topology(self.path.parent)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            validate_no_reparse_topology(self.path.parent)
            validate_no_reparse_topology(self.path)
            self.fd = _open_marker_mutex_no_follow(self.path)
            deadline = time.monotonic() + MARKER_MUTEX_TIMEOUT_SECONDS
            while True:
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        if os.fstat(self.fd).st_size == 0:
                            _write_all(self.fd, b"\0")
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.locked = True
                    break
                except (BlockingIOError, OSError) as exc:
                    if time.monotonic() >= deadline:
                        raise UpdateLockUnavailable(
                            f"timed out locking update marker mutex {self.path}"
                        ) from exc
                    time.sleep(0.05)
        except UpdateLockUnavailable:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            raise
        except OSError as exc:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            raise UpdateLockUnavailable(
                f"could not lock update marker mutex {self.path}: {exc}"
            ) from exc
        except Exception as exc:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            raise UpdateLockUnavailable(
                f"could not lock update marker mutex {self.path}: {exc}"
            ) from exc
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.fd is None:
            return
        try:
            if self.locked:
                if sys.platform == "win32":
                    import msvcrt

                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
            self.locked = False


@dataclass(frozen=True)
class _MarkerSnapshot:
    raw: str
    pid: int
    lease_at: float


@dataclass(frozen=True)
class _MarkerRead:
    snapshot: _MarkerSnapshot | None
    unavailable_reason: str | None = None


def _read_marker_snapshot(marker: Path) -> _MarkerRead:
    """Read one marker version, distinguishing absence from uncertainty."""
    try:
        # read_text() performs universal-newline translation, which would turn
        # lone CR bytes into LF and make Python accept a wire Rust/Electron
        # reject. Decode explicit bytes so CAS also retains exact payload bytes.
        raw = marker.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return _MarkerRead(snapshot=None)
    except (OSError, UnicodeError) as exc:
        return _MarkerRead(
            snapshot=None,
            unavailable_reason=f"could not read update marker {marker}: {exc}",
        )
    match = _MARKER_WIRE_RE.fullmatch(raw)
    if match is None:
        pid = -1
        lease_at = float("-inf")
    else:
        pid = int(match.group("pid"))
        lease_value = int(match.group("lease"))
        lease_at = (
            float(lease_value)
            if lease_value <= _MAX_SAFE_WIRE_INTEGER
            else float("inf")
        )
    return _MarkerRead(snapshot=_MarkerSnapshot(raw=raw, pid=pid, lease_at=lease_at))


def _snapshot_invalid_reason(snapshot: _MarkerSnapshot) -> str | None:
    if snapshot.pid <= 0 or snapshot.pid > _MAX_U32:
        return "update marker has an invalid pid"
    if (
        not math.isfinite(snapshot.lease_at)
        or snapshot.lease_at < 0
        or not snapshot.lease_at.is_integer()
        or snapshot.lease_at > _MAX_SAFE_WIRE_INTEGER
    ):
        return "update marker has an invalid lease timestamp"
    return None


def _unavailable_holder(reason: str) -> UpdateHolder:
    return UpdateHolder(pid=None, age_seconds=0, unavailable_reason=reason)


def _read_marker_payload(marker: Path) -> tuple[int, float] | None:
    """Parse the compatibility marker without applying liveness/age policy."""
    read = _read_marker_snapshot(marker)
    snapshot = read.snapshot
    if snapshot is None or _snapshot_invalid_reason(snapshot) is not None:
        return None
    return snapshot.pid, snapshot.lease_at


def _holder_from_snapshot(snapshot: _MarkerSnapshot) -> UpdateHolder | None:
    """Interpret one coherent marker snapshot with live-PID authority."""
    if _snapshot_invalid_reason(snapshot) is not None:
        return None
    if not _pid_alive(snapshot.pid):
        return None
    age = time.time() - snapshot.lease_at
    # Heartbeats keep normal age diagnostics fresh. A confirmed-live PID stays
    # authoritative even past the nominal ceiling: a suspend, clock jump, or
    # missed heartbeat must never admit a second checkout mutator.
    return UpdateHolder(pid=snapshot.pid, age_seconds=age)


def _atomic_write_marker(marker: Path, *, pid: int, lease_at: float) -> None:
    """Atomically publish one complete, exactly-two-line marker payload."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{marker.name}.", suffix=".tmp", dir=str(marker.parent)
    )
    tmp = Path(raw_tmp)
    try:
        payload = f"{pid}\n{int(lease_at)}\n".encode("utf-8")
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, marker)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink()
        except OSError:
            pass


def _create_marker_exclusive(marker: Path, *, pid: int, lease_at: float) -> None:
    """Publish a complete marker atomically without clobbering a winner."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{marker.name}.", suffix=".claim", dir=str(marker.parent)
    )
    tmp = Path(raw_tmp)
    try:
        _write_all(fd, f"{pid}\n{int(lease_at)}\n".encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        fd = -1
        # hard-link is an atomic no-replace publish on POSIX and Windows. A
        # reader can observe only the fully-written inode, never an empty file.
        os.link(tmp, marker)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass


def claim_desktop_handoff_marker(
    marker: Path,
    *,
    owner_pid: int,
    desktop_pid: int | None,
    lease_at: int,
) -> str | None:
    """Atomically claim ``marker`` for a repo-owned Desktop handoff script.

    Electron publishes a short bridge claim before it quits.  The script may
    replace that claim only when it names the explicitly supplied Desktop PID
    (or the script already owns it).  An absent/dead marker is claimed with the
    same staged-hard-link protocol as :class:`UpdateLock`; a live foreign or
    malformed/unreadable marker is left byte-for-byte untouched.

    ``None`` means success.  A string is a fail-closed refusal reason suitable
    for the handoff log.  The script process, rather than this short-lived
    Python helper, is recorded as owner so its later ``hermes update`` child can
    borrow the claim through the existing process-ancestry rule.
    """
    if owner_pid <= 0 or owner_pid > _MAX_U32:
        return "handoff script has an invalid owner pid"
    if desktop_pid is not None and (desktop_pid <= 0 or desktop_pid > _MAX_U32):
        return "Desktop handoff has an invalid predecessor pid"
    if lease_at < 0 or lease_at > _MAX_SAFE_WIRE_INTEGER:
        return "Desktop handoff has an invalid lease timestamp"
    if not _pid_alive(owner_pid):
        return "handoff script owner is not live"

    try:
        with _MarkerMutex(marker):
            # Electron does not take the sidecar mutex for its initial
            # no-clobber link, so an absent read can still lose that publish
            # race.  Re-read once and authorize the resulting complete claim.
            for _attempt in range(2):
                read = _read_marker_snapshot(marker)
                if read.unavailable_reason is not None:
                    return read.unavailable_reason
                snapshot = read.snapshot
                if snapshot is None:
                    try:
                        _create_marker_exclusive(
                            marker, pid=owner_pid, lease_at=lease_at
                        )
                    except FileExistsError:
                        continue
                    except OSError as exc:
                        return f"could not publish update marker {marker}: {exc}"
                    return None

                invalid_reason = _snapshot_invalid_reason(snapshot)
                if invalid_reason is not None:
                    return invalid_reason

                if snapshot.pid == owner_pid:
                    # Idempotent re-entry: preserve the acquisition time rather
                    # than making a long handoff look newly started.
                    _atomic_write_marker(
                        marker, pid=owner_pid, lease_at=snapshot.lease_at
                    )
                    return None

                if desktop_pid is not None and snapshot.pid == desktop_pid:
                    if int(snapshot.lease_at) != lease_at:
                        return "Desktop bridge marker lease does not match this handoff"
                    _atomic_write_marker(
                        marker, pid=owner_pid, lease_at=snapshot.lease_at
                    )
                    return None

                if _pid_alive(snapshot.pid):
                    return f"update marker is owned by live foreign pid {snapshot.pid}"

                # Only a strict, confirmed-dead snapshot reaches here.  All
                # cooperating replacers hold this mutex; re-read the raw body
                # before unlink so a changed claim is never removed.
                current_read = _read_marker_snapshot(marker)
                if current_read.unavailable_reason is not None:
                    return current_read.unavailable_reason
                current = current_read.snapshot
                if current is None:
                    continue
                current_invalid = _snapshot_invalid_reason(current)
                if current_invalid is not None:
                    return current_invalid
                if current.raw != snapshot.raw:
                    return "update marker changed while checking its owner"
                try:
                    marker.unlink()
                except OSError as exc:
                    return f"could not remove dead update marker {marker}: {exc}"

                try:
                    _create_marker_exclusive(marker, pid=owner_pid, lease_at=lease_at)
                except FileExistsError:
                    continue
                except OSError as exc:
                    return f"could not publish update marker {marker}: {exc}"
                return None

            return "another updater won the marker publication race"
    except (OSError, UpdateLockUnavailable) as exc:
        return f"could not establish exclusive update handoff: {exc}"


def _read_live_update_locked(marker: Path) -> UpdateHolder | None:
    """Read and CAS-clean only a well-formed, confirmed-dead marker."""
    read = _read_marker_snapshot(marker)
    if read.unavailable_reason is not None:
        return _unavailable_holder(read.unavailable_reason)
    snapshot = read.snapshot
    if snapshot is None:
        return None
    invalid_reason = _snapshot_invalid_reason(snapshot)
    if invalid_reason is not None:
        return _unavailable_holder(invalid_reason)
    holder = _holder_from_snapshot(snapshot)
    if holder is not None:
        return holder

    # Re-read immediately before unlink. Atomic heartbeat/takeover writes
    # change the complete payload; never delete a version different from the
    # stale one we evaluated.
    current_read = _read_marker_snapshot(marker)
    if current_read.unavailable_reason is not None:
        return _unavailable_holder(current_read.unavailable_reason)
    current = current_read.snapshot
    if current is None:
        return None
    current_invalid = _snapshot_invalid_reason(current)
    if current_invalid is not None:
        return _unavailable_holder(current_invalid)
    if current.raw != snapshot.raw:
        return _holder_from_snapshot(current) or _unavailable_holder(
            "update marker changed while checking its owner"
        )
    try:
        marker.unlink()
    except OSError as exc:
        return _unavailable_holder(
            f"could not remove dead update marker {marker}: {exc}"
        )
    return None


def read_live_update(*, path: Path | None = None) -> UpdateHolder | None:
    """Return the live or conservatively unresolved claim, or ``None``.

    Only absence or a well-formed, confirmed-dead marker means "no update".
    Unreadable/malformed state returns a holder with ``pid=None`` so callers
    fail closed. Dead files are deleted only after an owner-CAS recheck. A
    confirmed-live PID remains authoritative past the nominal age ceiling.
    Never raises.
    """
    marker = path or update_marker_path()
    read = _read_marker_snapshot(marker)
    if read.unavailable_reason is not None:
        return _unavailable_holder(read.unavailable_reason)
    snapshot = read.snapshot
    if snapshot is None:
        return None
    invalid_reason = _snapshot_invalid_reason(snapshot)
    if invalid_reason is not None:
        return _unavailable_holder(invalid_reason)
    holder = _holder_from_snapshot(snapshot)
    if holder is not None:
        return holder
    try:
        with _MarkerMutex(marker):
            return _read_live_update_locked(marker)
    except UpdateLockUnavailable as exc:
        logger.debug("Could not clean stale update marker %s: %s", marker, exc)
        return _unavailable_holder(str(exc))


def describe_holder(holder: UpdateHolder | None) -> str:
    """One-line, user-facing explanation of who holds the update lock."""
    if holder is None or holder.pid is None:
        return (
            "✗ Hermes could not establish exclusive ownership of this install.\n"
            "\n"
            "  The update lock is unavailable or unwritable. No files were\n"
            "  changed. Check the Hermes home permissions, then retry."
        )
    minutes, seconds = divmod(int(max(holder.age_seconds, 0)), 60)
    elapsed = f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
    return (
        f"✗ Another Hermes update is already running (PID {holder.pid}, "
        f"last active {elapsed} ago).\n"
        "\n"
        "  Two updates mutating the same checkout corrupt it: one rewrites\n"
        "  source while the other is mid-install. Wait for it to finish, or\n"
        "  close the window/dashboard tab that started it, then retry."
    )


class UpdateLock:
    """Context manager owning the shared update marker for this process.

    ``acquired`` is False when another live update already holds it — callers
    decide whether that's a hard refusal (CLI/dashboard) or a wait. Releasing
    only removes the marker when *we* still own it, so an explicitly handed-off
    marker is never deleted out from under its new owner.
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        heartbeat_seconds: float = UPDATE_MARKER_HEARTBEAT_SECONDS,
    ) -> None:
        self.path = path or update_marker_path()
        self.acquired = False
        self._borrowed = False
        self.holder: UpdateHolder | None = None
        self._owner_pid: int | None = None
        self._heartbeat_seconds = max(float(heartbeat_seconds), 0.01)
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_failed = False

    def _start_heartbeat(self) -> bool:
        self._heartbeat_stop = threading.Event()
        self._heartbeat_failed = False

        def _run() -> None:
            while not self._heartbeat_stop.wait(self._heartbeat_seconds):
                if not self.refresh_lease():
                    if self._heartbeat_failed:
                        self._fence_after_heartbeat_failure()
                    return

        self._heartbeat_thread = threading.Thread(
            target=_run,
            name=f"hermes-update-lease-{self._owner_pid or 'unknown'}",
            daemon=True,
        )
        try:
            self._heartbeat_thread.start()
        except RuntimeError as exc:
            logger.error("Could not start update lease heartbeat: %s", exc)
            self._heartbeat_thread = None
            return False
        return True

    def _fence_after_heartbeat_failure(self) -> None:
        """Hold the shared mutex until release after a failed lease write."""
        while not self._heartbeat_stop.is_set():
            try:
                with _MarkerMutex(self.path):
                    self._heartbeat_stop.wait()
                    return
            except UpdateLockUnavailable as exc:
                logger.error(
                    "Could not establish update heartbeat failure fence: %s", exc
                )
                self._heartbeat_stop.wait(1.0)

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        self._heartbeat_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def refresh_lease(self) -> bool:
        """Refresh our lease iff the marker still names this lock's owner."""
        owner_pid = self._owner_pid
        if not (self.acquired or self._borrowed) or owner_pid is None:
            return False
        try:
            with _MarkerMutex(self.path):
                read = _read_marker_snapshot(self.path)
                if read.unavailable_reason is not None:
                    raise UpdateLockUnavailable(read.unavailable_reason)
                snapshot = read.snapshot
                if snapshot is None:
                    raise UpdateLockUnavailable("update marker disappeared")
                invalid_reason = _snapshot_invalid_reason(snapshot)
                if invalid_reason is not None:
                    raise UpdateLockUnavailable(invalid_reason)
                if snapshot.pid != owner_pid:
                    if _pid_alive(snapshot.pid):
                        return False
                    raise UpdateLockUnavailable(
                        "update marker changed to a confirmed-dead owner"
                    )
                _atomic_write_marker(self.path, pid=owner_pid, lease_at=time.time())
        except (OSError, UpdateLockUnavailable) as exc:
            # Do not continue as though the lease were refreshed when marker
            # ownership could not be proved. The heartbeat worker fences the
            # install mutex until release as an additional fail-closed fence.
            logger.error("Could not refresh update lease %s: %s", self.path, exc)
            self._heartbeat_failed = True
            return False
        return True

    def acquire(self) -> bool:
        """Claim the lock. Returns False (and sets ``holder``) if it's taken.

        A live holder whose pid matches :data:`HANDOFF_PID_ENV` — or is a
        process ancestor of ours — is our own orchestrating parent (the Tauri
        updater spawning `hermes update` as a stage): we run under ITS claim
        rather than refusing or re-writing the marker, and ``release`` leaves
        the parent's marker untouched. The ancestry path exists because staged
        updaters older than the HANDOFF_PID_ENV export never send the env var.
        """
        owner_pid = os.getpid()
        borrowed_pid: int | None = None
        try:
            with _MarkerMutex(self.path):
                existing = _read_live_update_locked(self.path)
                if existing is not None:
                    if existing.pid is not None and (
                        existing.pid == _handoff_pid() or _is_ancestor_pid(existing.pid)
                    ):
                        borrowed_pid = existing.pid
                    else:
                        self.holder = existing
                        return False
                if borrowed_pid is None:
                    try:
                        _create_marker_exclusive(
                            self.path, pid=owner_pid, lease_at=time.time()
                        )
                    except FileExistsError:
                        # A non-Python owner can win between stale cleanup and
                        # no-clobber publish. Re-read and refuse its claim.
                        existing = _read_live_update_locked(self.path)
                        if existing is not None:
                            self.holder = existing
                            return False
                        try:
                            _create_marker_exclusive(
                                self.path, pid=owner_pid, lease_at=time.time()
                            )
                        except OSError as exc:
                            logger.error(
                                "Could not claim update marker %s: %s",
                                self.path,
                                exc,
                            )
                            return False
                    except OSError as exc:
                        logger.error(
                            "Could not write update marker %s: %s", self.path, exc
                        )
                        return False
        except UpdateLockUnavailable as exc:
            logger.error("Could not establish exclusive update lock: %s", exc)
            self.holder = None
            return False

        self.holder = None
        self._owner_pid = borrowed_pid if borrowed_pid is not None else owner_pid
        if borrowed_pid is not None:
            # The Rust/Tauri parent still owns deletion, but its Python child
            # may run for longer than the legacy 20-minute ceiling. Refresh
            # that confirmed parent's lease and stop without unlinking.
            self._borrowed = True
        else:
            self.acquired = True
        if not self._start_heartbeat():
            if self._borrowed:
                self._borrowed = False
                self._owner_pid = None
            else:
                self.release()
            return False
        return True

    def take_over_handoff(self) -> bool:
        """Atomically assume an explicitly-authorized parent's live claim.

        This is for an isolated rollout coordinator re-exec. The explicit
        takeover environment pid must match the current live marker owner.
        Ordinary Tauri handoffs keep using acquire() and stay non-owning.
        """
        expected_pid = _coordinator_takeover_pid()
        if expected_pid is None:
            return False
        owner_pid = os.getpid()
        try:
            with _MarkerMutex(self.path):
                existing = _read_live_update_locked(self.path)
                if existing is None or existing.pid != expected_pid:
                    self.holder = existing
                    return False
                # Replacement, not in-place truncation, so every reader sees a
                # complete old or new owner payload.
                _atomic_write_marker(self.path, pid=owner_pid, lease_at=time.time())
        except (OSError, UpdateLockUnavailable) as exc:
            logger.error("Could not take over update marker %s: %s", self.path, exc)
            self.holder = None
            return False
        self.holder = None
        self._owner_pid = owner_pid
        self._borrowed = False
        self.acquired = True
        if not self._start_heartbeat():
            self.release()
            return False
        return True

    def release(self) -> None:
        """Drop the marker if this process still owns it. Never raises."""
        if not (self.acquired or self._borrowed):
            return
        borrowed = self._borrowed
        self.acquired = False
        self._borrowed = False
        self._stop_heartbeat()
        owner_pid = self._owner_pid
        self._owner_pid = None
        if borrowed:
            return
        try:
            with _MarkerMutex(self.path):
                payload = _read_marker_payload(self.path)
                if payload is None or payload[0] != owner_pid:
                    # A handoff partner took ownership. Leave its live marker.
                    return
                self.path.unlink()
        except (OSError, UpdateLockUnavailable) as exc:
            # Fail closed: leaving our marker behind is safer than deleting a
            # claim whose ownership could not be proved. Dead-PID cleanup will
            # reclaim it on the next safe acquisition.
            logger.error("Could not release update marker %s: %s", self.path, exc)

    def __enter__(self) -> "UpdateLock":
        self.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()
