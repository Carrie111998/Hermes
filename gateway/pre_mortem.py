"""Pre-mortem handler — capture who/what kills the gateway on Windows.

The gateway has been dying UNCLEANLY every few hours on this Windows 11
install.  :mod:`gateway.lifecycle_ledger` detects the *fact* of an unclean
death (the next boot finds a ``phase=running`` sentinel from a dead PID),
but cannot answer **what killed it** — ``os.kill``/``TerminateProcess`` on
Windows bypasses every Python handler unless the process is alive to
*receive* the signal.

This module closes the gap with a defensive two-pronged approach:

1.  **Signal handlers** — ``SIGTERM``, ``SIGBREAK`` (Windows Ctrl+Break),
    and ``SIGINT`` (Ctrl+C; for detached runs that don't absorb it) all
    route through :func:`_on_term_signal`, which writes a JSONL record
    *immediately* (before the kernel reclaims us) and **does not return**.
    The non-returning call lets the OS terminate us with the same signal
    it arrived on — the natural exit pathway — so any in-flight
    ``lifecycle_ledger.mark_exited("graceful_shutdown")`` would also be
    skipped, preserving the "unclean" classification the next boot relies
    on for detection.

2.  **Parent-PID watcher thread** — a daemon thread that samples
    ``psutil.Process(parent_pid)`` every 30s and writes a
    ``pre_mortem.parent_changed`` record whenever our parent PID changes
    (re-parenting typically happens when a supervisor restarts the wrapper
    or our parent crashes and we get adopted by the system).  This catches
    the pattern where someone kills our *parent* first (e.g. the
    ``Hermes_Gateway`` scheduled task wrapper) and we subsequently die.

Every record is appended to ``<HERMES_HOME>/logs/gateway-exit-diag.log`` —
the same file the CLI's ``_exit_diag`` writes to — so existing tooling
already knows how to grep it.  A diagnostics failure must never block the
gateway from actually exiting, so every entry point is wrapped in a
``try/except Exception: pass``.

Idempotent — :func:`install_pre_mortem_handlers` may be called multiple
times safely (e.g. if ``start_gateway`` is called from both the CLI and
an embedded test); subsequent calls are no-ops.

Non-goals:

* We do NOT try to **prevent** the death.  Blocking SIGTERM would be
  worse than unhelpful on a service-managed process: a confused
  supervisor would think we're wedged and SIGKILL us, losing the
  shutdown it asked for.
* We do NOT try to **survive** the signal.  ``signal.raise`` after the
  write re-delivers the original signal cleanly, but on Windows
  ``SIGTERM`` is more often a ``TerminateProcess`` (no Python signal
  involvement) — that's exactly the case the parent-PID watcher exists
  to catch.

Restore history: this module has been dropped by an update-carry-replay
miss and restored at least twice before (2026-08-13, 2026-08-19). Its
presence + wiring is asserted by scripts/carry-audit.py so a future loss
surfaces same-day instead of silently, the next time the diagnostic is
needed for an actual crash.
"""
from __future__ import annotations

import errno
import json
import os
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_log = __import__("logging").getLogger(__name__)

# Path layout: HERMES_HOME / "logs" / "gateway-exit-diag.log" — matches the
# CLI's _exit_diag writer in hermes_cli/gateway.py:5124.
_EXIT_DIAG_RELATIVE = ("logs", "gateway-exit-diag.log")

# How often the parent-PID watcher samples.
_PARENT_WATCH_INTERVAL_S = 30.0

# Module-level guard: install_pre_mortem_handlers is idempotent.
_INSTALLED = False
_INSTALL_LOCK = threading.Lock()

# Serializes JSONL appends.  Reentrant: a signal can arrive while this
# thread is already mid-write, and the handler writes its own record.
_WRITE_LOCK = threading.RLock()
_WRITE_LOCK_TIMEOUT_S = 0.25

# Caps on the captured stacks.  A pre-mortem record has to stay small
# enough to append in one indivisible write (see _record), and a diag log
# nobody can skim is not evidence.  Deepest frames are the interesting
# ones, so truncation keeps the tail.
_MAX_STACK_THREADS = 24
_MAX_STACK_FRAMES = 12
_MAX_STACK_LINE_CHARS = 160


# ---------------------------------------------------------------------------
# Path resolution (HERMES_HOME)
# ---------------------------------------------------------------------------


def _resolve_hermes_home() -> Optional[Path]:
    """Return the HERMES_HOME used by this process, or None if unavailable.

    Mirrors :func:`gateway.lifecycle_ledger._process_hermes_home` — ignore
    task overrides (the gateway has no concept of a per-task home).
    """
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except Exception:
        return None


def _diag_log_path() -> Optional[Path]:
    home = _resolve_hermes_home()
    if home is None:
        return None
    return home.joinpath(*_EXIT_DIAG_RELATIVE)


# ---------------------------------------------------------------------------
# JSONL append (never raises)
# ---------------------------------------------------------------------------


def _record(tag: str, **extra: Any) -> None:
    """Append a single JSONL record to gateway-exit-diag.log.

    Best-effort.  A failure here must NEVER crash the gateway, and it must
    NEVER block the actual exit.  Errors are swallowed.
    """
    path = _diag_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tag": tag,
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            **extra,
        }
        line = json.dumps(payload, default=str) + "\n"
        # Open with append + binary mode for atomic single write on Windows
        # (file is small enough that O_APPEND races are vanishingly rare,
        # and using os.write avoids the newline-conversion surprises of
        # ``open(..., "a")`` on text mode under some locales).
        #
        # "Small enough" is doing real work in that sentence: a single
        # os.write is only indivisible up to a modest size, and this module
        # has two concurrent writers by design — the parent-watcher thread
        # and the main thread's signal handler.  Once records grew past a
        # few hundred bytes, those two interleaved and split a record across
        # lines, which corrupts the JSONL for every reader (measured: 3 of
        # 40 runs).  The lock serializes our own writers; _MAX_STACK_* below
        # keeps records small enough that the underlying assumption holds.
        #
        # Reentrant because a signal can land on a thread already inside
        # this function — the handler then calls _record again, and a plain
        # Lock would self-deadlock a process that is trying to die.  The
        # timeout bounds the other case (watcher holds it while we're
        # dying): take the small corruption risk over blocking the exit,
        # which this module must never do.
        got_lock = _WRITE_LOCK.acquire(timeout=_WRITE_LOCK_TIMEOUT_S)
        try:
            fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
        finally:
            if got_lock:
                _WRITE_LOCK.release()
    except Exception:
        # We are dying — a logging failure here would block the exit path.
        # Drop the record silently; the next-life cleaner-upper will
        # surface it via the lifecycle_ledger sentinel mismatch.
        pass


# ---------------------------------------------------------------------------
# Who-am-I context
# ---------------------------------------------------------------------------


def _proc_context() -> Dict[str, Any]:
    """Snapshot of process identity: parent PID, current user, session id.

    Returns ``{}`` (not an error) on any failure so callers can spread the
    keys into a record without conditional logic.
    """
    out: Dict[str, Any] = {}
    try:
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process(os.getpid())
        out["ppid"] = proc.ppid()
        try:
            out["current_user"] = proc.username()
        except Exception:
            pass
        # Memory sample. Added 2026-08-16 because it is the ONE measurement
        # that separates the two explanations for an unclean exit, and it was
        # missing: every one of the 44 `gateway.previous_unclean_exit` records
        # in gateway-exit-diag.log carries last_mem=None, so the paired
        # `suspected_oom` field has never been a measurement — it is a constant
        # False. Without an rss series we cannot tell an OOM kill from an
        # external kill, and on 2026-08-16 three gateways died inside 30
        # minutes (23min, 7min, then 13min lifetimes) with nothing logged at
        # all, which is exactly the signature both explanations produce.
        # rss/vms are cheap (a single syscall) and this runs on the existing
        # 30s watcher tick, so the series costs nothing and makes the next
        # crash diagnosable instead of merely observable.
        try:
            _mi = proc.memory_info()
            out["rss_mb"] = round(_mi.rss / 1048576, 1)
            out["vms_mb"] = round(getattr(_mi, "vms", 0) / 1048576, 1)
            out["mem_percent"] = round(proc.memory_percent(), 2)
        except Exception:
            # Never let a diagnostic break the thing it is diagnosing.
            out["rss_mb"] = None
        try:
            _vm = psutil.virtual_memory()
            out["host_mem_available_mb"] = round(_vm.available / 1048576, 1)
            out["host_mem_percent_used"] = _vm.percent
        except Exception:
            pass
        # Session id is Windows-only.  POSIX returns None — that's fine,
        # we just record what we can.
        try:
            if hasattr(proc, "session_id"):
                out["session_id"] = proc.session_id()  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception:
        # psutil unavailable or denied — record the absence explicitly so
        # the watcher thread can tell "I tried and failed" apart from "I
        # wasn't installed".
        out["psutil_available"] = False
    return out


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------


# The original signal handler we replaced — restored on de-install (unused
# today but kept for tests).
_ORIGINAL_HANDLERS: Dict[int, Any] = {}


def _on_term_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    """Disarm, final-write a pre-mortem record, then re-raise the signal.

    On Windows, SIGTERM is the typical wrapper for `taskkill /F` *when the
    caller opts into graceful* (without ``/F``) — taskkill first sends
    WM_CLOSE, then CTRL_BREAK_EVENT for console processes, and only after
    a timeout escalates to TerminateProcess.  We catch the early signal,
    write what we know, then re-raise so the kernel's normal cleanup
    proceeds.  If the caller used ``/F`` (TerminateProcess), no handler
    runs at all — that's the parent-watcher thread's job.
    """
    # DISARM FIRST.  `signal.raise_signal(signum)` below re-delivers the same
    # signal, and a handler is *not* reset on entry — so re-raising while we
    # are still installed re-enters this function instead of terminating
    # (verified: 30 re-entries from a single SIGINT).  Restoring SIG_DFL up
    # front also means an impatient supervisor's second SIGTERM kills us
    # immediately rather than stacking another frame while we write below.
    try:
        signal.signal(signum, signal.SIG_DFL)
    except Exception:
        pass

    sig_name = _signal_name(signum)
    try:
        _record(
            "pre_mortem.signal",
            signal_name=sig_name,
            signal_number=signum,
            stacks=_capture_stacks(),
            **_proc_context(),
        )
    finally:
        # Re-raise so the natural exit pathway runs (signal handlers must
        # not return normally to be useful here — that would let the
        # process keep running after a SIGTERM).  We disarmed above, so this
        # dispatches to SIG_DFL and terminates rather than recursing.
        try:
            signal.raise_signal(signum)  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            # signal.raise_signal is Python 3.8+ POSIX.  Fall back to
            # os.kill(self, sig) which works on both platforms.
            try:
                os.kill(os.getpid(), signum)
            except Exception:
                # Last resort: hard-exit with the conventional
                # 128+N "killed by signal N" code so the lifecycle ledger
                # sees an exit code rather than an os._exit suppression.
                os._exit(128 + signum)


def _capture_stacks() -> Dict[str, list]:
    """Every thread's stack, as JSON-embeddable lists of strings.

    Deliberately NOT ``faulthandler.dump_traceback(file=...)``.  The diag
    log is strict JSONL — the CLI's ``_exit_diag`` writer, the on-call
    triage script and this module's own reader all parse it line by line —
    so dumping a raw multi-line traceback beside the records corrupts the
    file for every consumer.  Embedding the stacks as a field keeps the
    evidence in the one place people already grep, and keeps it parseable.

    We are executing inside a Python-level handler, so the interpreter is
    healthy and ``sys._current_frames()`` is sound here; faulthandler's
    ability to dump from a wedged interpreter buys us nothing at this point.

    Best-effort: returns ``{}`` rather than raising, since a diagnostics
    failure must never delay the exit.
    """
    try:
        names = {t.ident: t.name for t in threading.enumerate()}
        out: Dict[str, list] = {}
        frames = sys._current_frames()
        # Our own thread first — it is the one that took the signal — then
        # the rest, so truncation never drops the frames that matter most.
        me = threading.get_ident()
        for tid in sorted(frames, key=lambda t: (t != me, t)):
            if len(out) >= _MAX_STACK_THREADS:
                out["_truncated_threads"] = [
                    f"{len(frames) - len(out)} more thread(s) omitted"
                ]
                break
            lines = [
                line.rstrip("\n")[:_MAX_STACK_LINE_CHARS]
                for line in traceback.format_stack(frames[tid])
            ]
            dropped = len(lines) - _MAX_STACK_FRAMES
            if dropped > 0:
                # Keep the deepest frames; note what was cut.
                lines = [f"... {dropped} outer frame(s) omitted"] + lines[-_MAX_STACK_FRAMES:]
            out[f"{names.get(tid, 'unknown')}({tid})"] = lines
        return out
    except Exception:
        return {}


def _signal_name(signum: int) -> str:
    """Best-effort signal name from its integer constant."""
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):
        return f"signal_{signum}"


# ---------------------------------------------------------------------------
# Parent-PID watcher thread
# ---------------------------------------------------------------------------


class _ParentWatcher(threading.Thread):
    """Sample our parent PID; write a record when it changes.

    This catches the most common Windows pattern of "someone SIGKILLed my
    parent wrapper then SIGKILLed me a moment later" — the kind of death
    that leaves no trace inside Python.  The state-change record goes to
    ``gateway-exit-diag.log`` so we can correlate the wrapper death with
    ours.
    """

    daemon = True
    name = "gateway-pre-mortem-parent-watcher"

    def __init__(self, interval_s: float) -> None:
        super().__init__(daemon=True)
        self._interval_s = interval_s
        self._stop_event = threading.Event()
        self._last_ppid: Optional[int] = None

    def run(self) -> None:
        try:
            import psutil  # type: ignore[import-not-found]
        except Exception:
            _record("pre_mortem.parent_watcher.disabled", reason="psutil_unavailable")
            return

        proc = psutil.Process(os.getpid())
        # First sample establishes the baseline — record it so we have a
        # starting point even if no change ever happens.
        try:
            self._last_ppid = proc.ppid()
            # _proc_context() already includes ppid — passing it again as
            # an explicit kwarg raises TypeError, which _record then
            # silently swallows and the baseline write vanishes. Take
            # ppid from _proc_context() and fall back to the live sample
            # only when psutil didn't populate it.
            ctx = _proc_context()
            ctx.setdefault("ppid", self._last_ppid)
            _record("pre_mortem.parent_baseline", **ctx)
        except Exception:
            return

        while not self._stop_event.is_set():
            # Sleep in short slices so we exit promptly on shutdown.
            # clamp to >=0 — time.monotonic() can race past end_at by a
            # hair under load and a negative sleep raises ValueError.
            end_at = time.monotonic() + self._interval_s
            while time.monotonic() < end_at and not self._stop_event.is_set():
                remaining = max(0.0, end_at - time.monotonic())
                time.sleep(min(0.5, remaining))
            if self._stop_event.is_set():
                return
            try:
                new_ppid = proc.ppid()
            except psutil.NoSuchProcess:
                _record("pre_mortem.parent_disappeared", ppid=self._last_ppid)
                return
            except Exception as exc:
                _record("pre_mortem.parent_sample_error", error=repr(exc))
                continue
            if new_ppid != self._last_ppid:
                _record(
                    "pre_mortem.parent_changed",
                    old_ppid=self._last_ppid,
                    new_ppid=new_ppid,
                    **_proc_context(),
                )
                self._last_ppid = new_ppid

    def stop(self) -> None:
        self._stop_event.set()


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install_pre_mortem_handlers(interval_s: float = _PARENT_WATCH_INTERVAL_S) -> bool:
    """Install signal handlers + parent-PID watcher thread.

    Idempotent.  Returns ``True`` if this call performed the install,
    ``False`` if a previous call already installed.

    On non-Windows POSIX, only SIGTERM and SIGINT handlers are installed
    (SIGBREAK doesn't exist).  On Windows, SIGBREAK is also wired so
    Ctrl+Break in the launcher console produces a record.

    Never raises — every failure mode is logged and absorbed.
    """
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        try:
            # SIGTERM — universal kill signal
            _ORIGINAL_HANDLERS[signal.SIGTERM] = signal.signal(
                signal.SIGTERM, _on_term_signal
            )
        except Exception:
            pass
        try:
            # SIGINT — Ctrl+C; some detached launches don't absorb it
            _ORIGINAL_HANDLERS[signal.SIGINT] = signal.signal(
                signal.SIGINT, _on_term_signal
            )
        except Exception:
            pass
        # SIGBREAK — Windows only; not in signal.Signals on POSIX
        if sys.platform == "win32":
            sig_break = getattr(signal, "SIGBREAK", 21)
            try:
                _ORIGINAL_HANDLERS[sig_break] = signal.signal(
                    sig_break, _on_term_signal
                )
            except Exception:
                pass

        # Parent-PID watcher — thread is daemon, never blocks exit
        watcher = _ParentWatcher(interval_s)
        try:
            watcher.start()
        except Exception:
            pass
        _record(
            "pre_mortem.installed",
            handlers=sorted(_ORIGINAL_HANDLERS.keys()),
            watcher_interval_s=interval_s,
            **_proc_context(),
        )
        _INSTALLED = True
        return True


# ---------------------------------------------------------------------------
# Manual helpers (used by tests + debugging)
# ---------------------------------------------------------------------------


def is_installed() -> bool:
    """Whether :func:`install_pre_mortem_handlers` has run in this process."""
    return _INSTALLED


def simulate_terminate(reason: str = "test") -> None:
    """Record a synthetic pre-mortem record without actually dying.

    Used by tests to verify the JSONL shape and the parent-context keys
    without needing to send a real signal.
    """
    _record("pre_mortem.simulated", reason=reason, **_proc_context())


__all__ = [
    "install_pre_mortem_handlers",
    "is_installed",
    "simulate_terminate",
]
