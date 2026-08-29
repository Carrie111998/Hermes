#!/usr/bin/env python3
"""browser_owner_watchdog.py — detached owner-death supervisor for agent-browser.

Problem this fixes (kanban t_8a1037d1): the Hermes browser tool launches a
headless Chromium via the ``agent-browser`` CLI. That CLI double-forks a
detached daemon which spawns Chromium with ``--user-data-dir=/tmp/agent-browser-
chrome-<uuid>``. All of Hermes's own cleanup — atexit handlers, the background
inactivity/orphan-reap thread, ``cleanup_all_browsers`` — runs *inside the
agent process*. If the agent is killed hard (``SIGKILL``, an OS-level crash, a
force-quit), that code never runs, the daemon survives, and the Chromium root
is reparented to pid 1 / systemd --user: an orphan holding swap. t_9b49cd19
built an hourly external reaper to bound the damage; this watchdog closes the
gap at the source so the leak never accumulates in the first place.

Mechanism (mirrors ``tools/mcp_stdio_watchdog.py``): instead of relying on the
agent's own teardown, the browser tool spawns this tiny supervisor as a
detached child *of the agent*. Being a child, it survives the agent's SIGKILL
(it is reparented to init, not killed) and it can detect the owner's death by
watching its own ``getppid()``: the instant the original parent is gone,
``os.getppid()`` no longer equals the recorded original PPID. On owner death
the watchdog reaps every agent-browser daemon whose owning hermes PID is dead
plus its Chromium tree, removes the stale socket dirs and the
``/tmp/agent-browser-chrome-*`` profile dirs, then exits. It never touches a
browser still owned by a live agent (cross-process safe via ``owner_pid``).

Self-termination (so the watchdog cannot itself become a leak):
  1. Owner death  -> reap, then exit.
  2. Absolute lifetime cap (default 24h) -> hard exit regardless.

The watchdog does NOT exit while the owner is alive even when no /tmp socket
dirs remain: it is spawned once per agent process and guards the owner for its
whole lifetime, so every browser session in a long-lived gateway/CLI process is
covered. Exiting on empty dirs would leave later sessions unprotected
(t_8a1037d1 review, round 1).

Stdlib-only, POSIX-only (the spawn site gates on ``os.name == "posix"``), and
fast to start. It does NOT import the heavy ``tools.browser_tool`` module.

Usage (see the spawn site in ``tools/browser_tool.py``)::

    python3 -m tools.browser_owner_watchdog --ppid <original_parent_pid>

Env:
  BROWSER_OWNER_WATCHDOG_POLL_S   poll interval in seconds (default 2)
  BROWSER_OWNER_WATCHDOG_MAX_S    absolute lifetime cap in seconds (default 86400)
  BROWSER_OWNER_WATCHDOG_DRY_RUN  set to 1 to log without killing/removing
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

import psutil

_POLL_S = float(os.environ.get("BROWSER_OWNER_WATCHDOG_POLL_S", "2"))
_MAX_S = float(os.environ.get("BROWSER_OWNER_WATCHDOG_MAX_S", str(24 * 3600)))
_DRY_RUN = os.environ.get("BROWSER_OWNER_WATCHDOG_DRY_RUN") == "1"

# Minimum age before a profile dir may be removed. This watchdog fires on ITS
# OWNER's death, but /tmp is shared: another agent may have just mkdir'd its
# profile dir and not yet exec'd Chromium, so that dir is referenced by no live
# process for a moment. Without an age gate we delete a live agent's session out
# from under it. Mirrors the fail-safe intent of
# ``browser_tool.BROWSER_ORPHAN_GRACE_SECONDS`` (unknown age -> do not touch),
# with a shorter default because the window we are closing is a launch race
# (seconds), not an idle-session ceiling. Anything skipped here is still caught
# by the hourly orphan reaper in browser_tool.
_PROFILE_MIN_AGE_S = float(
    os.environ.get("BROWSER_OWNER_WATCHDOG_PROFILE_MIN_AGE_S", "300")
)

UDD_PREFIX = "--user-data-dir=/tmp/agent-browser-chrome-"
TMP_GLOB = "agent-browser-chrome-*"


def _cmdline(pid: int) -> list[str]:
    """Argv tokens. Chromium collapses its argv into ONE space-joined blob, so
    split on whitespace too — matching NUL-separated tokens alone silently
    misses every Chromium process (t_9b49cd19 verified this)."""
    try:
        raw = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return []
    tokens: list[str] = []
    for chunk in raw:
        tokens.extend(t for t in chunk.split() if t)
    return tokens


def _ppid_of(pid: int) -> int:
    try:
        return psutil.Process(pid).ppid()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
        return -1


def _is_systemd_user(pid: int) -> bool:
    argv = _cmdline(pid)
    return bool(argv) and "systemd" in argv[0] and "--user" in argv


def _alive(pid: int) -> bool:
    """True if the PID is a live, non-zombie process.

    We treat zombies (state 'Z') as NOT alive: a zombie holds no memory/swap
    (its resources are already reclaimed) and is only a placeholder awaiting
    reap by its parent — in production that parent is init/systemd after the
    owning agent dies, so it is reaped promptly. Killing a zombie is a no-op;
    the goal is that nothing *consumes resources*, which a zombie does not.
    """
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.AccessDenied, OSError):
        # Cannot inspect it -> treat as not alive (fail-safe: we do not reap
        # what we cannot identify).
        return False


def _owner_is_gone(original_ppid: int) -> bool:
    """True once the original parent is no longer our parent (reparented to
    init) or has exited outright."""
    try:
        if os.getppid() != original_ppid:
            return True
    except OSError:
        return True
    return not _alive(original_ppid)


def _tree_kill(pid: int) -> None:
    """SIGTERM then SIGKILL the process and its descendants, children first.

    A process group is not usable here — the agent-browser daemon detached via
    setsid/double-fork, so it is not in our group. psutil walks the descendant
    tree for us (and is already a hard dependency of this repo, used the same
    way in tools/browser_tool.py).
    """
    try:
        root = psutil.Process(pid)
        targets = root.children(recursive=True) + [root]
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return
    for stage in ("terminate", "kill"):
        for proc in targets:
            try:
                getattr(proc, stage)()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                pass
        _gone, alive = psutil.wait_procs(targets, timeout=0.5)
        if not alive:
            return
        targets = alive


def _socket_safe_tmpdir() -> str:
    """Mirror of ``browser_tool._socket_safe_tmpdir``.

    browser_tool creates its socket dirs under this path, so the watchdog must
    look in the SAME place or it silently finds nothing to reap. Kept as a local
    copy rather than an import because this module is spawned as a standalone
    script and deliberately depends only on the stdlib plus psutil. If the
    original changes, change this with it.
    """
    if sys.platform == "darwin":
        return "/tmp"
    return tempfile.gettempdir()


def _proc_pids() -> list[int]:
    """Every live PID, via psutil (cross-platform; no /proc dependency)."""
    try:
        return psutil.pids()
    except (psutil.Error, OSError):
        return []


def _socket_dirs() -> list[str]:
    tmpdir = _socket_safe_tmpdir()
    out: list[str] = []
    for pattern in ("agent-browser-h_*", "agent-browser-cdp_*",
                    "agent-browser-hermes_*", "agent-browser-rp_*"):
        out.extend(str(p) for p in Path(tmpdir).glob(pattern))
    return sorted(set(out))


def _daemon_of_socket_dir(socket_dir: str) -> int | None:
    """Read the daemon PID from ``<session>.pid`` inside a socket dir."""
    try:
        for entry in Path(socket_dir).iterdir():
            if entry.name.endswith(".pid"):
                return int(entry.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return None


def _owner_of_socket_dir(socket_dir: str) -> int | None:
    """Read the owning hermes PID from ``<session>.owner_pid`` if present."""
    try:
        for entry in Path(socket_dir).iterdir():
            if entry.name.endswith(".owner_pid"):
                return int(entry.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return None


def _reap_owner_browsers() -> None:
    """Reap agent-browser daemons + Chromium whose owning hermes PID is dead,
    and remove stale /tmp socket + profile dirs. Never touches a browser whose
    owner is alive (cross-process safe).

    Two mechanisms, both keyed to owner death (the watchdog only runs this on
    owner death, so no age-based safety margin is needed):
      1. Daemons: read each ``agent-browser-*`` socket dir's ``owner_pid``; if
         that hermes PID is dead (or missing and untrackable), tree-kill the
         daemon (which carries its Chromium children in production) and remove
         the socket dir.
      2. Chromium roots: any Chromium root carrying
         ``--user-data-dir=/tmp/agent-browser-chrome-*`` that is orphaned
         (PPid 1 / systemd --user — i.e. its launching agent is gone) is
         tree-killed and its profile dir removed. Profile-dir REMOVAL is age
         gated (_PROFILE_MIN_AGE_S): /tmp is shared, so a dir with no live
         referencing process may be a concurrent agent mid-launch, not a leak.
    """
    live_owner_pids: set[int] = set()
    reap_daemons: list[tuple[int, str]] = []  # (daemon_pid, socket_dir)
    stale_dirs: list[str] = []

    for socket_dir in _socket_dirs():
        owner = _owner_of_socket_dir(socket_dir)
        daemon = _daemon_of_socket_dir(socket_dir)
        if owner is not None and _alive(owner):
            live_owner_pids.add(owner)
            continue
        # Owner dead, or missing owner_pid.
        if daemon is not None and _alive(daemon):
            reap_daemons.append((daemon, socket_dir))
        else:
            stale_dirs.append(socket_dir)

    # Reap Chromium roots that are orphaned (PPid 1 / systemd --user) and whose
    # profile dir is not held by a process that will SURVIVE this reap.
    #
    # Order matters. A previous version built the keep-set from every live
    # process first, then skipped any candidate whose profile dir was in it —
    # but a candidate always carries its own --user-data-dir, so it put its own
    # dir in the keep-set and then skipped itself. The condition was always true
    # and this whole mechanism was unreachable. Identify candidates FIRST, then
    # build the keep-set from processes that are not about to die with one.
    candidates: list[tuple[int, str]] = []  # (pid, profile_dir)
    for _pid in _proc_pids():
        argv = _cmdline(_pid)
        udd = next((a for a in argv if a.startswith(UDD_PREFIX)), None)
        if not udd:
            continue
        if any(a.startswith("--type=") for a in argv):
            continue  # child process; dies with its root
        parent = _ppid_of(_pid)
        if parent != 1 and not _is_systemd_user(parent):
            continue  # still owned by a live daemon/agent
        candidates.append((_pid, udd.split("=", 1)[1]))

    # Everything that dies when we reap the candidates: the roots plus their
    # descendants. Those must not vote to keep a profile dir alive.
    doomed: set[int] = {pid for pid, _ in candidates}
    for pid, _ in list(candidates):
        try:
            doomed.update(c.pid for c in psutil.Process(pid).children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass

    live_profile_dirs: set[str] = set()
    for _pid in _proc_pids():
        if _pid in doomed:
            continue
        for arg in _cmdline(_pid):
            if arg.startswith(UDD_PREFIX):
                live_profile_dirs.add(arg.split("=", 1)[1])

    orphan_chromium: list[tuple[int, str]] = [
        (pid, profile_dir)
        for pid, profile_dir in candidates
        if profile_dir not in live_profile_dirs
    ]

    reaped = 0
    for daemon_pid, socket_dir in reap_daemons:
        # Identity guard: never tree-kill an arbitrary PID from a world-writable
        # temp dir; confirm it is actually bound to this agent-browser session.
        argv = " ".join(_cmdline(daemon_pid)).lower()
        # Identity guard: never tree-kill an arbitrary PID read out of a
        # world-writable temp dir. The PID comes from a file under /tmp, so it
        # can be stale (the real daemon exited and the number was reused) or
        # planted. Confirm from the process's OWN argv that it really is an
        # agent-browser daemon before signalling it.
        #
        # This previously also OR'd in a basename(socket_dir) check, which made
        # the condition dead: every socket_dir comes from _socket_dirs(), which
        # globs only "agent-browser-*", so that conjunct was ALWAYS False and
        # `argv-check and False` could never be True — the guard never tripped
        # and every PID named in a /tmp file was tree-killed unconditionally.
        # Do not re-add a socket_dir term here; it carries no information.
        #
        # Fail-safe: _cmdline() returns [] when the process is gone or
        # unreadable (psutil raises NoSuchProcess/AccessDenied), so argv == ""
        # and the guard trips (we skip the kill).
        if "agent-browser" not in argv:
            stale_dirs.append(socket_dir)
            continue
        if not _DRY_RUN:
            _tree_kill(daemon_pid)
        reaped += 1
        if not _DRY_RUN:
            shutil.rmtree(socket_dir, ignore_errors=True)

    for chrom_pid, profile_dir in orphan_chromium:
        if not _DRY_RUN:
            _tree_kill(chrom_pid)
            shutil.rmtree(profile_dir, ignore_errors=True)
        reaped += 1

    for socket_dir in stale_dirs:
        if not _DRY_RUN:
            shutil.rmtree(socket_dir, ignore_errors=True)

    # Final stale-profile-dir pass: after the daemon/chromium kills, recompute
    # the live set from *non-zombie* processes and remove any /tmp profile dir
    # no longer referenced by a live process. Mirrors t_9b49cd19's keep-set.
    _cleanup_orphan_profile_dirs()

    if reaped or stale_dirs:
        print(
            f"browser_owner_watchdog: owner gone -> reaped {reaped} agent-browser "
            f"daemon/chromium process(es), removed {len(stale_dirs)} stale socket dir(s)",
            flush=True,
        )


def _cleanup_orphan_profile_dirs() -> None:
    """Remove /tmp/agent-browser-chrome-* dirs not referenced by any live
    (non-zombie) process. The keep-set is every profile dir still named in a
    live process cmdline; anything else is stale and safe to remove."""
    live_dirs: set[str] = set()
    for _pid in _proc_pids():
        pid = _pid
        if not _alive(pid):
            continue
        for arg in _cmdline(pid):
            if arg.startswith(UDD_PREFIX):
                live_dirs.add(arg.split("=", 1)[1])
    now = time.time()
    # The Chromium profile dirs are created by the external agent-browser CLI,
    # not by browser_tool, so we do not control that path. Search the resolved
    # tmpdir AND /tmp (deduped) rather than assuming either one.
    search_roots = {_socket_safe_tmpdir(), "/tmp"}
    candidates_d = {d for root in search_roots for d in Path(root).glob(TMP_GLOB)}
    for d in sorted(candidates_d):
        if str(d) in live_dirs:
            continue
        # Age gate: never remove a dir that is younger than the grace window, and
        # never remove one whose age we cannot determine. A dir with no live
        # referencing process is USUALLY stale — but it is also exactly what a
        # concurrent agent looks like between mkdir and exec.
        try:
            age_s = now - d.stat().st_mtime
        except OSError:
            continue  # unknown age -> fail safe, leave it
        if age_s < _PROFILE_MIN_AGE_S:
            continue
        if not _DRY_RUN:
            shutil.rmtree(d, ignore_errors=True)


def _run(original_ppid: int) -> int:
    """Watch the owner for its WHOLE lifetime.

    We deliberately do NOT self-terminate when the /tmp socket dirs are empty
    while the owner is still alive. The browser tool spawns (and keeps) one
    watchdog per live agent process, so if it exited on empty dirs it would
    leave later sessions in a long-lived gateway/CLI process with NO watchdog —
    and a SIGKILL during that session would leak orphan Chromium exactly as
    before (t_8a1037d1 review, round 1). The watchdog is stdlib-only and sleeps
    2s per poll, so a single instance guarding the owner for its whole life is
    trivially cheap. Exit only on owner death (after reaping) or the absolute
    ``_MAX_S`` lifetime cap, at which point we RE-EXEC rather than exit: the cap
    exists so a wedged watchdog can never accumulate, and re-exec replaces this
    process instead of adding one while keeping a long-lived owner protected.
    """
    start = time.time()
    while True:
        if _owner_is_gone(original_ppid):
            _reap_owner_browsers()
            return 0

        if time.time() - start >= _MAX_S:
            # Lifetime cap reached. We only get here with the owner STILL ALIVE
            # (owner death returns above, after reaping), so we must NOT reap —
            # that would kill a live session. But simply exiting would leave a
            # long-lived owner unprotected until its next browser session
            # happens to respawn us, and a SIGKILL in that gap leaks exactly the
            # orphan Chromium this watchdog exists to prevent.
            #
            # Re-exec instead: the cap's purpose is that a WEDGED watchdog can
            # never accumulate, and re-exec replaces this process rather than
            # adding one — same pid, same parent, so owner-death detection via
            # getppid() is unaffected — while the fresh image resets any wedged
            # state. If exec fails for any reason, fall back to the old
            # behaviour and exit rather than spin.
            try:
                os.execv(sys.executable,
                         [sys.executable, os.path.abspath(__file__),
                          "--ppid", str(original_ppid)])
            except OSError:
                return 0

        time.sleep(_POLL_S)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detached owner-death watchdog for agent-browser sessions.",
    )
    parser.add_argument("--ppid", type=int, required=True)
    args = parser.parse_args(argv)
    return _run(args.ppid)


if __name__ == "__main__":
    sys.exit(main())
