"""``hermes watch`` subcommand — watch filesystem events.

Attaches an inotify / kqueue / ReadDirectoryChangesW watcher to one or more
paths, printing events as they arrive.  Useful for CI feedback, log tailing,
or triggering downstream actions on file changes.

Usage:
  hermes watch <path> [<path> ...]
  hermes watch . --pattern "*.py" --command "pytest tests/"
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ── platform-specific watcher ────────────────────────────────────────────────

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True


    class _ChangeHandler(FileSystemEventHandler):
        """Print filesystem events matching the configured patterns."""

        def __init__(self, patterns: list[str] | None, ignore: list[str] | None):
            super().__init__()
            self._patterns = patterns or ["*"]
            self._ignore = ignore or []
            self._count = 0

        def _matches(self, path: str) -> bool:
            name = os.path.basename(path)
            if any(fnmatch(name, p) for p in self._ignore):
                return False
            if self._patterns == ["*"]:
                return True
            return any(fnmatch(name, p) for p in self._patterns)

        def on_any_event(self, event):
            if event.is_directory:
                return
            path = getattr(event, "dest_path", None) or event.src_path
            if not self._matches(path):
                return
            self._count += 1
            kind = event.event_type
            print(f"[{self._count:04d}] {kind:10s} {path}", flush=True)

except ImportError:
    _HAS_WATCHDOG = False


def _run_watchdog(
    paths: list[Path],
    patterns: list[str] | None,
    ignore: list[str] | None,
    recursive: bool,
    command: str | None,
) -> int:
    """Run the watchdog observer loop."""
    handler = _ChangeHandler(patterns, ignore)
    observer = Observer()

    for p in paths:
        resolved = p.resolve() if p.exists() else p
        if not resolved.exists():
            print(f"watch: skipping non-existent path: {p}", file=sys.stderr)
            continue
        observer.schedule(handler, str(resolved), recursive=recursive)

    if not observer._watches:  # type: ignore[attr-defined]
        print("watch: no valid paths to watch", file=sys.stderr)
        return 1

    observer.start()
    print(f"Watching {len(paths)} path(s) — Ctrl+C to stop", flush=True)

    shutdown = [False]

    def _handle_signal(signum, frame):
        shutdown[0] = True
        print("\nStopping watcher...", flush=True)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not shutdown[0]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join(timeout=3)
        print(f"Done. {handler._count} events.", flush=True)

    if command and handler._count > 0:
        print(f"Running: {command}", flush=True)
        return os.system(command)

    return 0


def _run_polling(
    paths: list[Path],
    patterns: list[str] | None,
    ignore: list[str] | None,
    interval: float,
) -> int:
    """Fallback polling watcher (no watchdog dependency)."""
    patterns = patterns or ["*"]
    ignore = ignore or []

    tracked: dict[str, tuple[str, float]] = {}
    watched_dirs: list[str] = []
    for p in paths:
        resolved = p.resolve() if p.exists() else p
        if not resolved.exists():
            print(f"watch: skipping non-existent path: {p}", file=sys.stderr)
            continue
        watched_dirs.append(str(resolved))
        for root, _, files in os.walk(str(resolved)):
            for fname in files:
                fpath = os.path.join(root, fname)
                tracked[fpath] = (fname, os.path.getmtime(fpath))

    if not watched_dirs:
        print("watch: no valid paths to watch", file=sys.stderr)
        return 1

    print(
        f"Watching {len(paths)} path(s) [polling {interval}s] — Ctrl+C to stop",
        flush=True,
    )

    count = 0
    shutdown = [False]

    def _handle_signal(signum, frame):
        shutdown[0] = True
        print("\nStopping watcher...", flush=True)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not shutdown[0]:
            time.sleep(interval)
            for fpath, (fname, mtime) in list(tracked.items()):
                if not os.path.exists(fpath):
                    if any(fnmatch(fname, p) for p in ignore):
                        continue
                    if not any(fnmatch(fname, p) for p in patterns) and patterns != ["*"]:
                        continue
                    count += 1
                    print(f"[{count:04d}] {'deleted':10s} {fpath}", flush=True)
                    del tracked[fpath]
                    continue

                new_mtime = os.path.getmtime(fpath)
                if new_mtime != mtime:
                    tracked[fpath] = (fname, new_mtime)
                    if any(fnmatch(fname, p) for p in ignore):
                        continue
                    if not any(fnmatch(fname, p) for p in patterns) and patterns != ["*"]:
                        continue
                    count += 1
                    print(f"[{count:04d}] {'modified':10s} {fpath}", flush=True)

            for root, _, files in os.walk(watched_dirs[0]):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    if fpath not in tracked:
                        tracked[fpath] = (fname, os.path.getmtime(fpath))
                        if any(fnmatch(fname, p) for p in ignore):
                            continue
                        if not any(fnmatch(fname, p) for p in patterns) and patterns != ["*"]:
                            continue
                        count += 1
                        print(f"[{count:04d}] {'created':10s} {fpath}", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"Done. {count} events.", flush=True)
    return 0


# ── CLI entry point ──────────────────────────────────────────────────────────


def cmd_watch(args: argparse.Namespace) -> int:
    """Handler for ``hermes watch``."""
    paths = [Path(p) for p in args.paths]
    patterns = args.pattern.split(",") if args.pattern else None
    ignore = args.ignore.split(",") if args.ignore else None

    if _HAS_WATCHDOG:
        return _run_watchdog(
            paths, patterns, ignore, args.recursive, args.command
        )
    else:
        return _run_polling(paths, patterns, ignore, args.interval)


# ── argparse builder ─────────────────────────────────────────────────────────


def build_watch_parser(subparsers, *, cmd_watch: Callable = cmd_watch) -> None:
    """Attach the ``watch`` subcommand to ``subparsers``."""
    watch_parser = subparsers.add_parser(
        "watch",
        help="Watch files/directories for changes",
        description=(
            "Watch one or more paths for filesystem events (created, modified, "
            "deleted) and print them as they happen. Uses inotify (Linux), "
            "FSEvents (macOS), or ReadDirectoryChangesW (Windows) when the "
            "``watchdog`` package is available; falls back to polling otherwise."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  hermes watch .                          Watch current directory
  hermes watch src/ tests/                Watch multiple directories
  hermes watch . --pattern "*.py"         Only show Python file changes
  hermes watch . --ignore "*.pyc,__pycache__/*"  Ignore patterns
  hermes watch . --command "pytest"       Run command after changes
  hermes watch . --interval 2.0           Polling interval (fallback mode)
""",
    )
    watch_parser.add_argument(
        "paths", nargs="+", help="Files or directories to watch"
    )
    watch_parser.add_argument(
        "-p",
        "--pattern",
        default=None,
        help="Only show events matching these glob patterns (comma-separated)",
    )
    watch_parser.add_argument(
        "-i",
        "--ignore",
        default=None,
        help="Ignore events matching these glob patterns (comma-separated)",
    )
    watch_parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        default=True,
        help="Watch directories recursively (default: True)",
    )
    watch_parser.add_argument(
        "--no-recursive",
        action="store_false",
        dest="recursive",
        help="Do not watch directories recursively",
    )
    watch_parser.add_argument(
        "-c",
        "--command",
        default=None,
        help="Shell command to run after changes are detected",
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (fallback mode, default: 1.0)",
    )
    watch_parser.set_defaults(func=cmd_watch)