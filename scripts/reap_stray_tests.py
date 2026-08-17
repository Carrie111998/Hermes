#!/usr/bin/env python3
"""Reap stray pytest / run_tests_parallel processes — scoped to YOUR OWN session.

Why this exists
---------------
On 2026-08-16 a concurrent Claude session ran an ad-hoc psutil sweep to free RAM:

    mine = {os.getpid()} | {a.pid for a in psutil.Process(os.getpid()).parents()}
    for p in psutil.process_iter(['pid', 'cmdline']):
        if p.info['pid'] in mine: continue
        ...
        if runner or pytest_w: p.kill()

It killed a SIBLING session's live `python -u -m pytest .../tests/cron` run 3m43s
into 1052 tests. `psutil.Process.kill()` on Windows is
``TerminateProcess(handle, SIGTERM)``, so the victim exited with code **15** and no
pytest summary — a log that reads exactly like a hang.

Excluding ``getpid()`` + ``parents()`` protects the sweeper's OWN SUBTREE and
nothing else. On a box that routinely runs 10+ concurrent sessions that is a
cross-session ``pkill -f pytest``. This script scopes kills to the caller's own
Claude session subtree instead, and refuses to act when it cannot prove ownership.

Usage
-----
    python scripts/reap_stray_tests.py                     # plan + reap my strays
    python scripts/reap_stray_tests.py --dry-run           # plan only
    python scripts/reap_stray_tests.py --min-age-minutes 30
    python scripts/reap_stray_tests.py --all-sessions      # box-wide, deliberate

Design notes
------------
* Matching is **structural on argv**, never a substring test against the joined
  command line. The original sweep's earlier variants used ``'-m pytest' in cl``
  against the joined string, which matched their own ``python -c "..."`` source
  text — they killed themselves (their tool results read literally ``Exit code 15``).
* Ancestry links are validated with ``create_time(child) >= create_time(parent)``
  so a dangling or recycled ppid cannot smuggle a foreign process into the subtree.
* Default-deny: an unresolvable session root reaps nothing. Same convention as
  ``cull-claude-sessions.py`` (unresolvable idle age -> never kill).
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Process records are plain dicts so every decision below is testable without a
# live process tree: {pid, ppid, name, cmdline, create_time, rss}.

_SESSION_MARKERS = ("--output-format", "claude-code")


def is_claude_session(record: dict) -> bool:
    """True if this record is a Claude Code CLI session (not an Electron helper).

    Mirrors the classifier in ``cull-claude-sessions.py``, which has been
    live-verified on this box: ``claude.exe`` carrying ``--output-format`` or
    ``claude-code``, with ``--type=`` (Electron helper) excluded.
    """
    name = os.path.basename(str(record.get("name") or "")).lower()
    if name not in {"claude.exe", "claude"}:
        return False
    argv = [str(a) for a in (record.get("cmdline") or [])]
    if any(a.startswith("--type=") for a in argv):
        return False
    joined = " ".join(argv)
    return any(marker in joined for marker in _SESSION_MARKERS)


def is_test_process(cmdline) -> bool:
    """True if argv is a pytest / run_tests_parallel invocation.

    STRUCTURAL on argv elements. ``-m`` must be immediately followed by
    ``pytest`` as a separate element, so text merely *containing* ``-m pytest``
    inside one quoted element (a script body, a shell wrapper's -Command string)
    does not match.
    """
    argv = [str(a) for a in (cmdline or [])]
    if not argv:
        return False
    if any(a.endswith("run_tests_parallel.py") for a in argv):
        return True
    if "-m" in argv:
        i = argv.index("-m")
        if i + 1 < len(argv) and argv[i + 1] == "pytest":
            return True
    return False


def _ancestor_chain(pid: int, by_pid: dict) -> list[dict]:
    """Records from ``pid`` upward, following ppid with create_time validation."""
    chain: list[dict] = []
    seen: set[int] = set()
    cur = by_pid.get(pid)
    while cur is not None and cur["pid"] not in seen:
        seen.add(cur["pid"])
        chain.append(cur)
        parent = by_pid.get(cur.get("ppid"))
        if parent is None:
            break
        # A parent that started AFTER its claimed child means the ppid was
        # recycled -- the real parent is gone. Stop rather than climb a lie.
        if parent.get("create_time", 0.0) > cur.get("create_time", 0.0):
            break
        cur = parent
    return chain


def resolve_session_root(pid: int, by_pid: dict) -> int | None:
    """Topmost Claude session ancestor of ``pid`` (inclusive), or None."""
    root = None
    for record in _ancestor_chain(pid, by_pid):
        if is_claude_session(record):
            root = record["pid"]
    return root


def descendants_of(root_pid: int, records) -> set[int]:
    """Every process reachable downward from ``root_pid`` via validated ppid links."""
    by_parent: dict[int, list[dict]] = {}
    for r in records:
        by_parent.setdefault(r.get("ppid"), []).append(r)
    by_pid = {r["pid"]: r for r in records}

    out: set[int] = set()
    stack = [root_pid]
    while stack:
        cur_pid = stack.pop()
        parent = by_pid.get(cur_pid)
        for child in by_parent.get(cur_pid, []):
            if child["pid"] in out:
                continue
            if parent is not None and child.get("create_time", 0.0) < parent.get(
                "create_time", 0.0
            ):
                # Child predates its claimed parent -> recycled ppid, not ours.
                continue
            out.add(child["pid"])
            stack.append(child["pid"])
    return out


def build_plan(
    records,
    *,
    my_pid: int,
    now: float,
    min_age_minutes: float = 0.0,
    all_sessions: bool = False,
) -> tuple[list[dict], str]:
    """Decide what to kill. Returns ``(victims, note)``; never raises.

    ``victims`` entries carry pid, session_root, age_minutes, rss, cmdline so the
    caller can print a full plan before doing anything.
    """
    by_pid = {r["pid"]: r for r in records}
    my_root = resolve_session_root(my_pid, by_pid)

    protected = {r["pid"] for r in _ancestor_chain(my_pid, by_pid)}
    protected.add(my_pid)

    if all_sessions:
        candidates = {r["pid"] for r in records}
        note = "ALL SESSIONS — box-wide reap (every victim attributed below)"
    else:
        if my_root is None:
            return [], (
                "refusing to reap: could not resolve my Claude session root, so "
                "ownership is unprovable (default-deny). Use --all-sessions to "
                "override deliberately."
            )
        candidates = descendants_of(my_root, records)
        note = f"own session only (root PID {my_root})"

    victims: list[dict] = []
    for pid in sorted(candidates):
        if pid in protected:
            continue
        record = by_pid.get(pid)
        if record is None or not is_test_process(record.get("cmdline")):
            continue
        age_minutes = max(0.0, (now - record.get("create_time", now)) / 60.0)
        if age_minutes < min_age_minutes:
            continue
        victims.append(
            {
                "pid": pid,
                "session_root": resolve_session_root(pid, by_pid),
                "age_minutes": age_minutes,
                "rss": record.get("rss", 0),
                "cmdline": [str(a) for a in (record.get("cmdline") or [])],
            }
        )
    return victims, note


# --------------------------------------------------------------------------- live

def snapshot(psutil_mod) -> list[dict]:
    """Build process records from a live psutil module."""
    records = []
    for proc in psutil_mod.process_iter(
        ["pid", "ppid", "name", "cmdline", "create_time", "memory_info"]
    ):
        try:
            info = proc.info
            mem = info.get("memory_info")
            records.append(
                {
                    "pid": info["pid"],
                    "ppid": info.get("ppid"),
                    "name": info.get("name"),
                    "cmdline": info.get("cmdline") or [],
                    "create_time": info.get("create_time") or 0.0,
                    "rss": getattr(mem, "rss", 0) if mem else 0,
                }
            )
        except Exception:
            continue
    return records


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", "--plan-only", dest="dry_run", action="store_true",
                    help="print the plan and exit without killing anything")
    ap.add_argument("--min-age-minutes", type=float, default=0.0,
                    help="only reap processes older than this (default 0)")
    ap.add_argument("--all-sessions", action="store_true",
                    help="box-wide reap across OTHER sessions too (deliberate; "
                         "every victim is printed with its owning session first)")
    args = ap.parse_args(argv)

    try:
        import psutil
    except ImportError:
        print("psutil is required", file=sys.stderr)
        return 2

    records = snapshot(psutil)
    victims, note = build_plan(
        records,
        my_pid=os.getpid(),
        now=time.time(),
        min_age_minutes=args.min_age_minutes,
        all_sessions=args.all_sessions,
    )

    print("=== REAP PLAN ===")
    print(f"Scope: {note}")
    if args.all_sessions:
        print("WARNING: --all-sessions can kill ANOTHER session's live test run.")
    if not victims:
        print("Nothing to reap.")
        return 0
    for v in victims:
        print(
            f"  PID {v['pid']:<7} session={v['session_root']}  "
            f"age={v['age_minutes']:.1f}min  rss={v['rss'] / 2**20:.0f}MB  "
            f"{' '.join(v['cmdline'])[:90]}"
        )
    print(f"Total: {len(victims)}")

    if args.dry_run:
        print("[dry-run — no kills executed]")
        return 0

    print("=== EXECUTING ===")
    killed = 0
    for v in victims:
        try:
            proc = psutil.Process(v["pid"])
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                proc.kill()
            killed += 1
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:  # pragma: no cover - environment dependent
            print(f"  FAILED pid={v['pid']}: {exc}")
    print(f"Reaped {killed} / {len(victims)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
