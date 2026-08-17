"""Runner-agnostic watcher for the literal ``%SystemDrive%`` junk tree.

A literal ``%SystemDrive%/ProgramData/Microsoft/Windows/Caches/`` tree
periodically appears in repository roots on this box. ``HKLM\\...\\ProfileList``
holds ``%SystemDrive%\\ProgramData`` as a REG_EXPAND_SZ; a process whose
environment lacks SYSTEMDRIVE cannot expand it, uses the literal string as a
RELATIVE path, and builds the known-folder cache under its own CWD.

One writer was found and fixed on 2026-08-16 (``run_secret_cli``). A later
sighting in a path that fix does not touch suggests a second writer is open.

This watcher is deliberately NOT part of the parallel test runner. The probe
it replaces lived inside ``run_tests_parallel.py`` and could only observe
spawns that runner made -- but the writer reproduced from ONE plain sequential
``python -m pytest`` run, so that probe was structurally unable to see it.

Usage:
    python scripts/systemdrive_watcher.py [ROOT ...]

Runs with ``cwd`` left to the caller; the parallel runner starts it with
``cwd=$HOME`` so the watcher can never be its own suspect.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Deque, List

import psutil

JUNK_NAME = "%SystemDrive%"

_LOG_REL = Path(".hermes") / "logs" / "systemdrive-watcher.jsonl"

DEFAULT_SECS = 36000.0
# 100ms, not 250ms: measured on this machine, one sample() costs ~16.65ms with
# ~1000 live processes, and that cost is NOT constant -- only NEW pids get
# enriched, so it grows with process churn, and churn peaks during exactly the
# runs this watcher is armed for. At a 250ms cadence a sampler captured only
# 9 of 12 short-lived Python children (each living 350-514ms); at 50ms it
# caught 12 of 12. 100ms costs roughly 17% of one core and stays comfortably
# below that 350-514ms lifetime, leaving margin for churn-induced slowdown.
# Cost was never the binding constraint here -- capture probability is.
DEFAULT_SAMPLE_MS = 100
DEFAULT_POLL_MS = 250
DEFAULT_RING = 4000


def log_path() -> Path:
    """Where sightings are appended.

    Keyed off home for the same reason as the runner's slot dir:
    scripts/run_tests.sh execs under ``env -i`` and HOME/USERPROFILE are among
    the few things that survive it. Writing inside a watched root would also
    mean the watcher littering the directory it is watching.
    """
    try:
        return Path.home() / _LOG_REL
    except (RuntimeError, OSError):
        return Path(tempfile.gettempdir()) / "systemdrive-watcher.jsonl"


def write_record(log: Path, event: str, **fields) -> dict:
    """Append one record as JSONL and shout about it on stderr.

    Written at the moment of the event rather than at exit: this class of run
    gets killed, times out, or loses its terminal often enough that end-of-run
    reporting would be the one copy of the evidence that does not survive.
    """
    record = {
        "event": event,
        "at": datetime.now().isoformat(timespec="seconds"),
        "watcher_pid": os.getpid(),
        **fields,
    }
    line = json.dumps(record, default=str)
    print(f"  [junk-watcher] {line}", file=sys.stderr, flush=True)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:  # a diagnostic must never take the run down
        print(f"  [junk-watcher] could not write log: {exc}", file=sys.stderr, flush=True)
    return record


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="systemdrive_watcher.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "roots", nargs="*", metavar="ROOT",
        help="Directories to watch for a %%SystemDrive%% child (default: cwd)",
    )
    parser.add_argument(
        "--secs", type=float, default=DEFAULT_SECS,
        help="Self-limit. Backstop against an orphaned sidecar (default: %(default)s)",
    )
    parser.add_argument(
        "--sample-ms", type=int, default=DEFAULT_SAMPLE_MS,
        help="Process-creation sampler cadence (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-ms", type=int, default=DEFAULT_POLL_MS,
        help="Polling-backend interval, fallback only (default: %(default)s)",
    )
    parser.add_argument(
        "--ring", type=int, default=DEFAULT_RING,
        help="Process-creation ring buffer capacity (default: %(default)s)",
    )
    parser.add_argument(
        "--stop-file", type=Path, default=None,
        help="Shut down gracefully once this path exists",
    )
    parser.add_argument("--log", type=Path, default=None, help="Override the JSONL log path")
    parser.add_argument(
        "--force-polling", action="store_true",
        help="Skip ReadDirectoryChangesW; use the polling backend everywhere",
    )
    return parser


def describe_pid(pid: int) -> dict:
    """One process, captured as completely as permissions allow.

    Fields are read INDIVIDUALLY on purpose. ``cwd`` raises AccessDenied for
    many Windows processes, and a single try/except around the whole block
    would then also lose ``cmdline`` -- which is the field that actually names
    a writer.
    """
    entry: dict = {"pid": pid, "seen_at": datetime.now().isoformat(timespec="milliseconds")}
    try:
        proc = psutil.Process(pid)
    except Exception as exc:
        # Vanished between pids() and here. Keep it: a PID we saw appear and
        # could not read is still evidence that SOMETHING started.
        entry["error"] = type(exc).__name__
        return entry
    for field, getter in (
        ("name", proc.name),
        ("ppid", proc.ppid),
        ("create_time", proc.create_time),
        ("cmdline", proc.cmdline),
        ("cwd", proc.cwd),
    ):
        try:
            entry[field] = getter()
        except Exception as exc:
            entry.setdefault("errors", {})[field] = type(exc).__name__
    return entry


def cwd_matches(entry: dict, root: Path) -> bool:
    """Does this process hold the watched root as its working directory?

    The established mechanism REQUIRES this: the junk lands under the writer's
    CWD. This is what narrows a ~1000-process table to a shortlist.

    Best-effort by nature -- an entry whose cwd could not be read (already
    exited, or AccessDenied) simply does not match.
    """
    cwd = entry.get("cwd")
    if not cwd:
        return False
    try:
        return Path(cwd).resolve() == root
    except OSError:
        return False


class ProcessRing:
    """Bounded history of process CREATIONS.

    Sampling ``psutil.pids()`` is one cheap syscall; only the NEW pids get
    enriched. Cost therefore scales with process CHURN, not with the ~1000
    live processes, which is what makes a short cadence affordable.

    This is the piece that attacks the prototype's documented failure: on
    2026-08-16 the watcher fired correctly but the writer had already exited,
    so a full snapshot named nobody. A creation history makes attribution
    independent of whether the writer is still alive at sighting time.

    Thread-safety, precisely: ``sample()`` calls are serialized against each
    other (via ``_sample_lock``), so ``_known``/``_primed`` are never read or
    mutated by two callers at once. ``dump()`` and ``__len__()`` only ever
    touch ``_entries`` under the separate, cheaper ``_lock``, so they are safe
    to call concurrently from other threads -- including while a ``sample()``
    call is in flight.
    """

    def __init__(self, capacity: int = DEFAULT_RING) -> None:
        self._entries: Deque[dict] = collections.deque(maxlen=capacity)
        self._known: set = set()
        self._primed = False
        self._lock = threading.Lock()
        # Serializes sample() end-to-end (see class docstring). Deliberately
        # separate from `_lock`: enrichment takes tens of milliseconds, and
        # `_lock` is what dump()/__len__() use, so holding it that long would
        # block readers for no reason.
        self._sample_lock = threading.Lock()
        self._sample_errors = 0

    def sample(self) -> int:
        """One diff pass. Returns how many creations were recorded.

        Never raises: a diagnostic must never take down the run it observes.
        Any failure here is swallowed and counted in ``_sample_errors``
        instead (with a one-time stderr note on the first occurrence) so the
        loop survives the entire run without spamming stderr on every tick.
        """
        with self._sample_lock:
            try:
                live = set(psutil.pids())
                new = live - self._known
                if not self._primed:
                    # The first pass is a baseline. Everything looks new, but
                    # nothing actually started inside our window; recording
                    # ~1000 entries here would bury the handful that matter.
                    self._primed = True
                    self._known = live
                    return 0
                entries = [describe_pid(pid) for pid in sorted(new)]
                # Commit `_known` only AFTER the entries are safely appended.
                # If enrichment or the append below raised with `_known`
                # already advanced to `live`, those pids would be marked
                # "known" and the next sample() would never see them as new
                # again -- the creations would be silently and permanently
                # lost, with no retry. Advance `_known` last, once recording
                # has actually succeeded.
                with self._lock:
                    self._entries.extend(entries)
                self._known = live
                return len(entries)
            except Exception as exc:  # never take the observed run down
                self._sample_errors += 1
                if self._sample_errors == 1:
                    print(
                        f"  [junk-watcher] sample() failed (will keep counting"
                        f" silently): {type(exc).__name__}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                return 0

    def dump(self) -> List[dict]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


def watch_polling(root: Path, on_hit, stop: threading.Event, interval_s: float) -> None:
    """Fallback backend: one ``exists()`` per tick.

    Used on non-Windows, when the root cannot be opened for a directory
    watch, or under --force-polling. Deliberately does NOT walk or stat the
    tree -- the sizes and version counters inside it are the evidence.
    """
    target = root / JUNK_NAME
    while not stop.is_set():
        try:
            if target.exists():
                on_hit(root, "polling")
                return
        except OSError:
            pass
        stop.wait(interval_s)


class Watcher:
    """Watches N roots for a literal %SystemDrive% child."""

    def __init__(
        self,
        roots,
        log: "Path | None" = None,
        ring_capacity: int = DEFAULT_RING,
        sample_ms: int = DEFAULT_SAMPLE_MS,
        poll_ms: int = DEFAULT_POLL_MS,
        secs: float = DEFAULT_SECS,
        stop_file: "Path | None" = None,
        force_polling: bool = False,
    ) -> None:
        self.roots = [Path(r).resolve() for r in roots]
        self.log = log or log_path()
        self.secs = secs
        self.stop_file = stop_file
        self.force_polling = force_polling
        self._sample_s = sample_ms / 1000.0
        self._poll_s = poll_ms / 1000.0
        # Kept as its own attribute rather than read back off the deque's
        # maxlen: the `armed` record reports it, and reaching into another
        # object's private state to report your own config is a trap.
        self._ring_capacity = ring_capacity
        self._ring = ProcessRing(ring_capacity)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._hit_roots: set = set()
        self.sightings = 0

    def record_preexisting(self) -> None:
        """A tree already present at arm time blames nobody.

        It is also latched, so it can never be re-reported as a fresh
        transition later in this watch.
        """
        for root in self.roots:
            target = root / JUNK_NAME
            try:
                present = target.exists()
            except OSError:
                continue
            if present:
                with self._lock:
                    self._hit_roots.add(str(root))
                write_record(
                    self.log, "preexisting", root=str(root), path=str(target),
                    note="present before the watch started - cannot attribute; "
                         "delete it and re-run to make this root usable",
                )

    def on_hit(self, root: Path, backend: str) -> None:
        """Record the first absent->present transition for ``root``.

        Ordering is the whole point: the ring buffer is dumped FIRST because
        it is already in memory, then the live table is enumerated. Ancestry
        is the perishable part; the directory is not.
        """
        key = str(root)
        with self._lock:
            if key in self._hit_roots:
                return
            self._hit_roots.add(key)
            self.sightings += 1

        ring = self._ring.dump()                      # in memory, instant
        live = [describe_pid(pid) for pid in psutil.pids()]

        snapshot: "Path | None" = self.log.with_name(
            f"systemdrive-sighting-{datetime.now():%Y%m%d-%H%M%S-%f}.json"
        )
        try:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(
                json.dumps({"ring": ring, "live": live}, indent=2, default=str),
                encoding="utf-8",
            )
        except OSError:
            snapshot = None

        write_record(
            self.log, "SIGHTING",
            root=key,
            path=str(root / JUNK_NAME),
            backend=backend,
            # The falsifier for the whole mechanism story. A sighting recorded
            # while SYSTEMDRIVE IS present kills the missing-SYSTEMDRIVE
            # explanation and restarts the hunt.
            watcher_has_systemdrive="SYSTEMDRIVE" in os.environ,
            live_cwd_matches=[e for e in live if cwd_matches(e, root)],
            ring_cwd_matches=[e for e in ring if cwd_matches(e, root)],
            live_process_count=len(live),
            ring_size=len(ring),
            snapshot_file=str(snapshot) if snapshot else None,
        )

    def stop(self) -> None:
        self._stop.set()

    def _sampler_loop(self) -> None:
        while not self._stop.is_set():
            # Deduct the sample's own cost from the sleep. A fixed sleep AFTER
            # sample() would make the true cadence `interval + cost`, and cost
            # grows with process churn -- so the gap between samples would
            # stretch precisely during a busy run, which is exactly when the
            # writer is most likely to appear. Measured: one sample costs
            # ~16.65ms with ~1000 live processes, more under churn.
            started = time.monotonic()
            try:
                self._ring.sample()
            except Exception:  # sampling must never kill the watch
                pass
            if self.stop_file is not None:
                try:
                    if self.stop_file.exists():
                        self._stop.set()
                        return
                except OSError:
                    pass
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self._sample_s - elapsed))

    def _start_backends(self) -> dict:
        """Start one detection thread per root; return backend by root."""
        chosen = {}
        for root in self.roots:
            target = watch_polling
            name = "polling"
            args = (root, self.on_hit, self._stop, self._poll_s)
            chosen[str(root)] = name
            threading.Thread(target=target, args=args, daemon=True).start()
        return chosen

    def run(self) -> int:
        """Watch until the deadline, the stop-file, or a stop() call."""
        self.record_preexisting()
        threading.Thread(target=self._sampler_loop, daemon=True).start()
        backends = self._start_backends()
        write_record(
            self.log, "armed",
            roots=[str(r) for r in self.roots],
            backend_by_root=backends,
            sample_ms=int(self._sample_s * 1000),
            poll_ms=int(self._poll_s * 1000),
            ring_capacity=self._ring_capacity,
            watcher_has_systemdrive="SYSTEMDRIVE" in os.environ,
            watcher_cwd=os.getcwd(),
        )
        started = time.monotonic()
        deadline = started + self.secs
        while not self._stop.is_set() and time.monotonic() < deadline:
            self._stop.wait(0.2)
        self._stop.set()
        watched = round(time.monotonic() - started, 1)
        write_record(
            self.log, "done",
            sightings=self.sightings,
            roots=[str(r) for r in self.roots],
            watched_secs=watched,
            note=(
                f"NEGATIVE - watched {len(self.roots)} root(s) for {watched}s, "
                "no %SystemDrive% appeared"
                if not self.sightings else "see SIGHTING record(s)"
            ),
        )
        return 0
