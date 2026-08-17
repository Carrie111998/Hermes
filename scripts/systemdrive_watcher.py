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
from datetime import datetime
from pathlib import Path
from typing import Deque, List

import psutil

JUNK_NAME = "%SystemDrive%"

_LOG_REL = Path(".hermes") / "logs" / "systemdrive-watcher.jsonl"

DEFAULT_SECS = 36000.0
DEFAULT_SAMPLE_MS = 250
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
    """

    def __init__(self, capacity: int = DEFAULT_RING) -> None:
        self._entries: Deque[dict] = collections.deque(maxlen=capacity)
        self._known: set = set()
        self._primed = False
        self._lock = threading.Lock()

    def sample(self) -> int:
        """One diff pass. Returns how many creations were recorded."""
        live = set(psutil.pids())
        new = live - self._known
        self._known = live
        if not self._primed:
            # The first pass is a baseline. Everything looks new, but nothing
            # actually started inside our window; recording ~1000 entries here
            # would bury the handful that matter.
            self._primed = True
            return 0
        entries = [describe_pid(pid) for pid in sorted(new)]
        with self._lock:
            self._entries.extend(entries)
        return len(entries)

    def dump(self) -> List[dict]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
