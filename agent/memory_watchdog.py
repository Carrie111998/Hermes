"""In-process memory watchdog for agent backends.

Catches unbounded memory growth inside a running Hermes agent process
(CLI, gateway turn, desktop/serve backend, cron run) *before* the kernel
OOM killer has to fire, and leaves a forensic record — a faulthandler dump
of every thread stack — pointing at the code that was ballooning.

Why this exists
---------------
Two Hermes desktop backends were kernel-OOM-killed on a 27 GB host within
15 minutes of each other (19.5 GB and 23 GB ``anon-rss``; the second kill
took the NoMachine session down with it). The growth (~200 MB/s) happened
inside silent terminal-wait windows while the backend sat in its poll loop,
and because the process died to the kernel OOM killer there was **zero
in-process evidence** of which code path was allocating. The victims were
never observed trimming; every post-mortem was a kernel task table, not a
stack trace.

The watchdog is deliberately a containment + forensics net, not a cure:
the leak itself has to be found and fixed (see the linked issue/PR), but
until then this bounds the blast radius (the process dies *before* the
machine freezes; supervisors restart it) and turns the next occurrence
from "kernel table forensics" into "here is the stack that ballooned".

Design constraints (from the incidents and the shutdown watchdog pattern
in ``gateway/shutdown_watchdog.py``):

* The kill path must survive a wedged/GIL-starved main loop: it uses only
  ``/proc`` reads (psutil fallback), ``faulthandler.dump_traceback`` (async
  signal under the hood, works while threads are stuck) and ``os._exit``.
* Never raises, never blocks: a forensics failure must not take the
  agent down (same contract as ``gateway.lifecycle_ledger``).
* One thread per process, not per agent: ``hermes serve``/gateway create a
  fresh AIAgent per request; without a process-level guard the watchdog
  itself would leak a thread per agent (the openrouter-prewarm guard in
  ``agent.agent_init`` exists for exactly this class of bug).
* Opt-in hard kill, default-on warn: the warn threshold produces a log
  line + a full thread-stack dump to a file on first crossing; the hard
  threshold (if configured) exits the process with a distinctive code so
  the supervisor restarts it instead of the kernel OOM-killing the machine
  into a freeze.

Config (``agent.memory_watchdog`` in config.yaml)::

    agent:
      memory_watchdog:
        enabled: true           # default true — warn-only unless thresholds set
        interval_s: 30          # sample cadence (default 30, min 5)
        warn_threshold_kib: 0   # KiB of RSS that triggers a warn dump; 0 = off
        hard_threshold_kib: 0   # KiB that triggers contained exit; 0 = off

Both thresholds default to 0 (feature silent) so the schema default is a
pure no-op; the desktop/gateway deployments that need containment set
them explicitly. Warn fires at most once per growth episode (re-arms when
RSS falls back below the threshold); hard exit is naturally one-shot.

Exit code: ``MEMORY_WATCHDOG_EXIT_CODE`` (42). Distinctive so a supervisor
or user can tell "watchdog contained a runaway" from a crash; the number
is not used by any other ``os._exit`` site in the tree (search before
reusing).
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Distinct from every other os._exit site in the tree (gateway uses 0 and
# 75; verified by grep before picking). 42: "the answer", not a secret.
MEMORY_WATCHDOG_EXIT_CODE = 42

DEFAULT_INTERVAL_S = 30.0
MIN_INTERVAL_S = 5.0

_DUMP_RELATIVE = ("logs", "agent-memory-watchdog.log")

# Process-level singletons: one watchdog per OS process, regardless of how
# many AIAgent instances are created inside it (gateway/serve create one
# per request). Guards against thread-per-agent leaks.
_thread_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_settings: Dict[str, Any] = {}


def _sample_rss_kib() -> int:
    """Cheap own-RSS read. Never raises; 0 = unknown (sampling skipped)."""
    # /proc first (sub-millisecond, no dependency), psutil fallback for
    # macOS/WSL corner cases. Same shape as gateway.lifecycle_ledger.
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])  # value is in kB
    except Exception:
        pass
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss // 1024
    except Exception:
        pass
    return 0


def _watchdog(
    *,
    interval_s: float,
    warn_threshold_kib: int,
    hard_threshold_kib: int,
    dump_path: Optional[Path],
) -> None:
    """Sample-and-trip loop. Runs on the watchdog daemon thread."""
    warn_armed = True
    while not _stop_event.wait(interval_s):
        rss = _sample_rss_kib()
        if rss == 0:
            continue  # sampling unavailable (e.g. exotic platform) — no-op
        if warn_threshold_kib and rss >= warn_threshold_kib and warn_armed:
            warn_armed = False
            _fire_warn(rss, dump_path)
        elif warn_threshold_kib and rss < warn_threshold_kib * 0.9:
            # Hysteresis: re-arm only once RSS falls 10% below the
            # threshold, so an RSS hovering at the line cannot flap between
            # armed/disarmed on every sample.
            warn_armed = True
        if hard_threshold_kib and rss >= hard_threshold_kib:
            _fire_exit(rss, dump_path)
            return  # unreachable when _fire_exit succeeds


def _fire_warn(rss_kib: int, dump_path: Optional[Path]) -> None:
    """Log + dump all thread stacks to the dump file (best-effort)."""
    try:
        logger.warning(
            "[memory-watchdog] RSS %d KiB crossed warn threshold — dumping "
            "all thread stacks to %s",
            rss_kib,
            dump_path,
        )
    except Exception:
        pass
    if dump_path is None:
        return
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        with open(dump_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"--- memory-watchdog warn {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                f"pid={os.getpid()} rss_kib={rss_kib} ---\n"
            )
            fh.flush()
            faulthandler.dump_traceback(file=fh, all_threads=True)
    except Exception:
        logger.debug("[memory-watchdog] warn dump failed", exc_info=True)


def _fire_exit(rss_kib: int, dump_path: Optional[Path]) -> None:
    """Log + dump + contained exit. The whole path is GIL-resilient."""
    try:
        logger.critical(
            "[memory-watchdog] RSS %d KiB crossed hard threshold — exiting "
            "with code %d (contained; supervisor will restart). Stacks "
            "dumped to %s",
            rss_kib,
            MEMORY_WATCHDOG_EXIT_CODE,
            dump_path,
        )
    except Exception:
        path = f"{dump_path}"
        print(
            f"[memory-watchdog] RSS {rss_kib} KiB crossed hard threshold; "
            f"exiting {MEMORY_WATCHDOG_EXIT_CODE}; dump: {path}",
            file=sys.stderr,
        )
    try:
        if dump_path is not None:
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            with open(dump_path, "a", encoding="utf-8") as dfh:
                dfh.write(
                    f"--- memory-watchdog EXIT {time.strftime('%Y-%m-%dT%H:%M:%S')} "
                    f"pid={os.getpid()} rss_kib={rss_kib} ---\n"
                )
                dfh.flush()
                faulthandler.dump_traceback(file=dfh, all_threads=True)
    except Exception:
        pass
    os._exit(MEMORY_WATCHDOG_EXIT_CODE)


def _resolve_dump_path() -> Optional[Path]:
    """``<HERMES_HOME>/logs/agent-memory-watchdog.log`` — never raises."""
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home().joinpath(*_DUMP_RELATIVE)
    except Exception:
        return None


def _normalize_threshold(value: Any) -> int:
    """Positive int KiB or 0 (off). Junk → 0, never an exception."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _normalize_interval(value: Any) -> float:
    """Positive float seconds, clamped to ≥ MIN_INTERVAL_S. Junk → default."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    if f <= 0:
        return DEFAULT_INTERVAL_S
    return max(f, MIN_INTERVAL_S)


def resolve_settings(section: Any) -> Dict[str, Any]:
    """Resolve ``agent.memory_watchdog`` config into effective settings.

    Tolerant: a malformed or missing section yields the schema default
    (warn-only, thresholds 0 = silent no-op). Never raises.
    """
    if not isinstance(section, dict):
        section = {}
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    return {
        "enabled": enabled,
        "interval_s": _normalize_interval(section.get("interval_s", DEFAULT_INTERVAL_S)),
        "warn_threshold_kib": _normalize_threshold(section.get("warn_threshold_kib", 0)),
        "hard_threshold_kib": _normalize_threshold(section.get("hard_threshold_kib", 0)),
    }


def start_memory_watchdog(settings: Dict[str, Any]) -> Optional[threading.Thread]:
    """Start the process-level watchdog thread (idempotent per process).

    First call wins: later AIAgent instances in the same process (serve /
    gateway create one per request) adopt the already-running thread. The
    thread samples at the *first* caller's cadence — deployments that need
    a different cadence set it in config, not per-request.
    """
    global _thread
    if not isinstance(settings, dict) or not settings.get("enabled", True):
        return _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return _thread
        interval_s = _normalize_interval(settings.get("interval_s", DEFAULT_INTERVAL_S))
        warn_threshold_kib = _normalize_threshold(settings.get("warn_threshold_kib", 0))
        hard_threshold_kib = _normalize_threshold(settings.get("hard_threshold_kib", 0))
        if warn_threshold_kib <= 0 and hard_threshold_kib <= 0:
            # Both thresholds off: nothing to watch. Don't spawn a thread
            # that only sleeps. (enabled=true with no thresholds is the
            # schema default state — silent no-op.)
            return None
        dump_path = _resolve_dump_path()
        _stop_event.clear()
        _thread = threading.Thread(
            target=_watchdog,
            kwargs={
                "interval_s": interval_s,
                "warn_threshold_kib": warn_threshold_kib,
                "hard_threshold_kib": hard_threshold_kib,
                "dump_path": dump_path,
            },
            daemon=True,
            name="agent-memory-watchdog",
        )
        _thread.start()
        return _thread


def stop_memory_watchdog() -> None:
    """Test/reset hook: stop the process-level thread."""
    global _thread
    with _thread_lock:
        _stop_event.set()
        _thread = None