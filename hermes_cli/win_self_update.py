"""Windows self-update trampoline for ``hermes update``.

Why this module exists
----------------------

On Windows, ``hermes`` is launched through a distlib/setuptools console-script
launcher: ``venv\\Scripts\\hermes.exe``. That launcher is **not** a thin
redirect — it is a real process that reads the shebang embedded in its own
trailer, spawns ``venv\\Scripts\\python.exe`` as a *child*, hands it the
launcher's own path as the script (the ``.exe`` doubles as a zip archive
holding ``__main__.py``), and then blocks until the child exits.

That produces a two-process chain for every ``hermes`` invocation::

    hermes.exe  (pid A, holds Scripts\\hermes.exe mapped as its own image)
      └── python.exe  (pid B, runs hermes_cli.main)

``hermes update`` ends in ``uv pip install -e .``, whose final step rewrites
the console-script shims — including ``Scripts\\hermes.exe``. Windows refuses
to delete or replace a file that is currently mapped as a running image, so uv
fails with::

    error: failed to remove file `...\\Scripts/hermes.exe`:
           The process cannot access the file because it is being used by
           another process. (os error 32)

The lock holder is pid A. Nothing pid B can do from inside Python releases it,
because pid B is not the process holding the image — its parent is. The
pre-existing mitigation (:func:`main._quarantine_running_hermes_exe`) tries to
rename the shim out of uv's way, which Windows normally permits even for a
running image; when that rename loses to a transient handle it falls back to
``MoveFileExW(MOVEFILE_DELAY_UNTIL_REBOOT)``, which does **not** move the file
now — it only queues the rename for next boot, leaving the shim exactly where
it was and the install still doomed.

That residual failure mode, and the cleanup of the rename entries it queues,
are handled separately and deliberately not duplicated here — see #68821
(hard-stop when the shim stays locked) and #85942 (clean orphaned shim entries
from ``PendingFileRenameOperations``). This module removes the cause those two
are left cleaning up after: with the update no longer running from the shim it
is replacing, the quarantine rename has nothing to lose against in the common
case.

The fix here removes the hazard instead of racing it: before the update touches
anything, re-launch the update as a detached grandchild driven directly by
``python.exe -m hermes_cli.main``, then let pids B and A exit. The grandchild
waits for the launcher to disappear, at which point ``Scripts\\hermes.exe`` is
an ordinary unlocked file and uv can replace it like any other.

    hermes.exe (A) ──> python.exe (B) ──> python.exe (C, detached)
         exit             exit              waits for A, then updates

This is the same pattern used by every self-updating Windows tool that has to
replace its own launcher.

Non-Windows platforms never reach any of this: POSIX replaces a running
executable's inode atomically, so the update runs in-process as before.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Set on the detached grandchild to (a) mark it as already-trampolined so it
# never re-trampolines, and (b) name the launcher pid it must outlive before
# touching the venv.
TRAMPOLINE_PARENT_ENV = "HERMES_UPDATE_TRAMPOLINE_PARENT"

# How long the grandchild waits for the launcher chain to exit before giving
# up and refusing. The launcher exits within milliseconds of its child; a
# multi-second ceiling only matters when something has genuinely wedged.
LAUNCHER_EXIT_TIMEOUT_SECONDS = 30.0
_LAUNCHER_POLL_SECONDS = 0.05

# Windows process-creation flags. We deliberately do NOT pass DETACHED_PROCESS
# or CREATE_NO_WINDOW: the grandchild should keep writing to the same console
# the user is watching. CREATE_NEW_PROCESS_GROUP stops a Ctrl+C aimed at the
# (already exiting) launcher from also killing the updater mid-install.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _is_windows() -> bool:
    return sys.platform == "win32"


def _shim_names(scripts_dir: Path) -> set[str]:
    """Lower-cased filenames of the console-script shims uv will rewrite."""
    try:
        from hermes_cli.main import _hermes_exe_shims

        return {p.name.lower() for p in _hermes_exe_shims(scripts_dir)}
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not enumerate console-script shims: %s", exc)
        return {"hermes.exe", "hermes-agent.exe", "hermes-acp.exe"}


def find_locking_launcher(scripts_dir: Path) -> tuple[int, str] | None:
    """Return ``(pid, exe_path)`` of an ancestor launcher holding a shim open.

    Walks our own parent chain looking for a process whose executable is one of
    the console-script shims in ``scripts_dir``. That process has the shim
    mapped as its running image, which is precisely what blocks uv's rewrite.

    Returns ``None`` when no ancestor is a shim — the normal case for
    ``python -m hermes_cli.main update``, for a gateway-spawned update, and for
    every non-Windows platform. Any probe failure also returns ``None``: an
    unprovable lock must fall through to the existing quarantine path rather
    than refuse a legitimate update.
    """
    if not _is_windows():
        return None

    names = _shim_names(scripts_dir)
    try:
        import psutil

        for parent in psutil.Process().parents():
            try:
                exe = parent.exe()
            except Exception:
                continue
            if not exe:
                continue
            exe_path = Path(exe)
            if exe_path.name.lower() not in names:
                continue
            # Same-directory check: a `hermes.exe` from a *different* install
            # is someone else's problem, and the concurrent-instance guard in
            # _cmd_update_impl already covers it.
            try:
                same_dir = exe_path.parent.resolve() == scripts_dir.resolve()
            except OSError:
                same_dir = False
            if same_dir:
                return parent.pid, str(exe_path)
    except Exception as exc:
        logger.debug("Could not walk ancestry for launcher detection: %s", exc)
    return None


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` currently exists. Never raises, never signals.

    Reuses :func:`hermes_cli.update_lock._pid_alive`, which routes through the
    project's no-kill probe. Do not hand-roll ``os.kill(pid, 0)`` here: on
    Windows CPython maps signal 0 onto ``GenerateConsoleCtrlEvent``, which
    Ctrl+C's the target's entire console process group (bpo-14484).
    """
    from hermes_cli.update_lock import _pid_alive as probe

    return probe(pid)


def trampoline_parent_pid() -> int | None:
    """Launcher pid this process was told to outlive, or ``None``.

    Presence of this value is also what marks us as the already-trampolined
    grandchild, so :func:`maybe_trampoline` never recurses.
    """
    raw = os.environ.get(TRAMPOLINE_PARENT_ENV, "").strip()
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def wait_for_launcher_exit(
    pid: int, *, timeout: float = LAUNCHER_EXIT_TIMEOUT_SECONDS
) -> bool:
    """Block until ``pid`` is gone. Returns False on timeout.

    Called at the top of the detached updater. Until the launcher exits, its
    shim is still a mapped image and uv's rewrite would fail exactly as before
    — so a timeout here is a hard stop, not a warning.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            # The image section is torn down as part of process teardown, but
            # the handle can outlive the pid's disappearance by a hair on
            # loaded machines. One short settle beat costs nothing next to a
            # multi-minute install.
            time.sleep(0.2)
            return True
        time.sleep(_LAUNCHER_POLL_SECONDS)
    return not _pid_alive(pid)


def _spawn_detached_updater(argv: list[str], launcher_pid: int) -> int:
    """Start the real updater as a console-sharing, detached grandchild.

    stdout/stderr are inherited so the user keeps watching one stream of
    output. stdin is deliberately **not**: once we exit, the shell reclaims the
    console and starts reading keystrokes for its own prompt. An updater still
    holding the same stdin would race it for every character, and the update's
    interactive questions (notably "Restore local changes now?") would appear
    with no reliable way to answer them.

    Handing the child DEVNULL makes ``sys.stdin.isatty()`` False, which is
    exactly the signal ``_cmd_update_impl`` already uses to select its
    non-interactive path: no prompts, and stashed local changes handled per
    the ``updates.non_interactive_local_changes`` config setting (default
    ``stash``, which preserves them). That is the correct behavior for a
    detached updater — the alternative is a question nobody can answer.
    """
    env = dict(os.environ)
    env[TRAMPOLINE_PARENT_ENV] = str(launcher_pid)

    kwargs = {
        "env": env,
        "cwd": os.getcwd(),
        "stdin": subprocess.DEVNULL,
    }

    # If the launcher chain sits inside a job object configured to kill on
    # close (some terminal hosts and CI runners do this), break out so our own
    # exit doesn't take the updater down with us. Harmless when no such job
    # exists, but not universally permitted — fall back without it.
    try:
        proc = subprocess.Popen(
            argv,
            creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_BREAKAWAY_FROM_JOB,
            **kwargs,
        )
    except OSError:
        proc = subprocess.Popen(argv, creationflags=_CREATE_NEW_PROCESS_GROUP, **kwargs)
    return proc.pid


def maybe_trampoline(argv: list[str]) -> bool:
    """Hand the update off to a detached process when we'd lock our own shim.

    ``argv`` is the full original command line to re-run (``sys.argv[1:]``).

    Returns ``True`` when a handoff happened, in which case the caller must
    return/exit immediately without doing any update work — the launcher has
    to die for its shim to become replaceable.

    Returns ``False`` when no handoff is needed or possible, and the caller
    should proceed with the update in-process:

    * non-Windows (no self-replacement hazard at all),
    * already the trampolined grandchild,
    * no venv Scripts dir resolved,
    * no ancestor launcher holds a shim (e.g. ``python -m hermes_cli.main``),
    * spawn failed — the existing quarantine path is still a better shot than
      refusing outright.
    """
    if not _is_windows():
        return False
    if trampoline_parent_pid() is not None:
        return False
    if os.environ.get("HERMES_NO_UPDATE_TRAMPOLINE", "").strip():
        logger.debug("Update trampoline disabled by HERMES_NO_UPDATE_TRAMPOLINE")
        return False

    from hermes_cli.main import _venv_scripts_dir

    scripts_dir = _venv_scripts_dir()
    if scripts_dir is None:
        return False

    found = find_locking_launcher(scripts_dir)
    if found is None:
        return False
    launcher_pid, launcher_exe = found

    python_exe = Path(sys.executable)
    if not python_exe.is_file():
        logger.debug("No usable sys.executable for trampoline: %s", python_exe)
        return False

    new_argv = [str(python_exe), "-m", "hermes_cli.main", *argv]

    try:
        child_pid = _spawn_detached_updater(new_argv, launcher_pid)
    except Exception as exc:
        print(
            f"  ⚠ Could not hand the update off to a detached process ({exc}).\n"
            "    Continuing in-process; if the install fails with "
            "'used by another process', re-run as:\n"
            f"      {python_exe} -m hermes_cli.main update"
        )
        return False

    print(
        f"→ Handing off to a detached updater (PID {child_pid}) so "
        f"{Path(launcher_exe).name} can be replaced."
    )
    print(
        "  This shell returns to a prompt immediately; update output continues "
        "below until it finishes."
    )
    print(
        "  The detached updater runs non-interactively — local source changes "
        "follow the\n"
        "  updates.non_interactive_local_changes setting (default: stash, "
        "which keeps them)."
    )
    print()
    return True
