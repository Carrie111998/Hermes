"""Windows subprocess compatibility helpers.

Hermes is developed on Linux / macOS and tested natively on Windows too.
Several common subprocess patterns break silently-or-loudly on Windows:

* ``["npm", "install", ...]`` — on Windows ``npm`` is ``npm.cmd``, a batch
  shim.  ``subprocess.Popen(["npm", ...])`` fails with WinError 193
  ("not a valid Win32 application") because CreateProcessW can't run a
  ``.cmd`` file without ``shell=True`` or PATHEXT resolution.

* ``start_new_session=True`` — on POSIX, this maps to ``os.setsid()`` and
  actually detaches the child.  On Windows it's silently ignored; the
  Windows equivalent is ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS``
  creationflags, which Python only applies when you pass them explicitly.

* Console-window flashes — every ``subprocess.Popen`` of a ``.exe`` on
  Windows spawns a cmd window briefly unless ``CREATE_NO_WINDOW`` is
  passed.  Cosmetic but jarring for background daemons.

This module centralizes the platform-branching logic so the rest of the
codebase doesn't sprinkle ``if sys.platform == "win32":`` everywhere.

**All helpers are no-ops on non-Windows** — calling them in Linux/macOS
code paths is safe by design.  That's the "do no damage on POSIX"
guarantee.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Sequence

__all__ = [
    "IS_WINDOWS",
    "resolve_node_command",
    "resolve_windows_git_bash",
    "windows_detach_flags",
    "windows_detach_flags_without_breakaway",
    "windows_hide_flags",
    "windows_detach_popen_kwargs",
    "windows_pipe_readable_bytes",
    "run_text_capture",
]


IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _PeekNamedPipe = _kernel32.PeekNamedPipe
    _PeekNamedPipe.argtypes = [
        wintypes.HANDLE,                 # hNamedPipe
        ctypes.c_void_p,                 # lpBuffer (NULL — we only want the count)
        wintypes.DWORD,                  # nBufferSize
        ctypes.POINTER(wintypes.DWORD),  # lpBytesRead
        ctypes.POINTER(wintypes.DWORD),  # lpTotalBytesAvail
        ctypes.POINTER(wintypes.DWORD),  # lpBytesLeftThisMessage
    ]
    _PeekNamedPipe.restype = wintypes.BOOL


# -----------------------------------------------------------------------------
# Node ecosystem launcher resolution
# -----------------------------------------------------------------------------


def resolve_node_command(name: str, argv: Sequence[str]) -> list[str]:
    """Resolve a Node-ecosystem command name to an absolute-path argv.

    On Windows, commands like ``npm``, ``npx``, ``yarn``, ``pnpm``,
    ``playwright``, ``prettier`` ship as ``.cmd`` files (batch shims).
    ``subprocess.Popen(["npm", "install"])`` fails with WinError 193
    because CreateProcessW doesn't execute batch files directly.

    ``shutil.which(name)`` *does* resolve ``.cmd`` via PATHEXT and returns
    the fully-qualified path — which CreateProcessW accepts because the
    extension tells Windows to route through ``cmd.exe /c``.

    On POSIX ``shutil.which`` also returns a fully-qualified path when
    found.  That's a small change from bare-name resolution (the OS does
    its own PATH search) but functionally identical and has the side
    benefit of making the argv reproducible in logs.

    Behavior when the command is not on PATH:
    - On Windows: return the bare name — caller can still try with
      ``shell=True`` as a last resort, OR the subsequent Popen will
      raise FileNotFoundError with a readable error we want to surface.
    - On POSIX: same.  Bare ``npm`` on a Linux box without npm installed
      fails the same way it did before this function existed.

    Args:
        name: The command name to resolve (``npm``, ``npx``, ``node`` …).
        argv: The remaining arguments.  Must NOT include ``name`` itself —
            this function builds the full argv list.

    Returns:
        A list suitable for passing to subprocess.Popen/run/call.
    """
    resolved = shutil.which(name)
    if resolved:
        return [resolved, *argv]
    return [name, *argv]


# -----------------------------------------------------------------------------
# Git Bash resolution (Windows)
# -----------------------------------------------------------------------------


def resolve_windows_git_bash() -> Optional[str]:
    """Locate a bash.exe on Windows that can execute ``C:\\`` paths.

    On a box with WSL installed, PATH order usually puts the WSL launcher
    (``System32\\bash.exe``) or its Microsoft Store stub
    (``WindowsApps\\bash.exe``) ahead of Git Bash — when Git Bash is on
    PATH at all (a default Git for Windows install only adds ``Git\\cmd``,
    which ships no bash).  WSL bash runs commands inside the Linux VM,
    where ``C:\\...``-style paths do not resolve: scripts and snapshot
    files "vanish" and bash exits 126/127 with a mangled-path "No such
    file or directory".

    Preference order:

    1. ``HERMES_GIT_BASH_PATH`` — explicit user override.
    2. Hermes' own portable Git (``%LOCALAPPDATA%\\hermes\\git``) —
       dropped by install.ps1 when the user had no working system Git;
       checked before system installs so a broken or partially
       uninstalled system Git can't hijack the lookup.  Both PortableGit
       (``bin\\bash.exe``) and MinGit (``usr\\bin\\bash.exe``) layouts
       are probed.
    3. Known Git for Windows install locations (machine-wide and
       per-user).
    4. ``shutil.which("bash")`` — last resort, REFUSING System32 /
       WindowsApps results (the WSL launcher and its Store stub).

    Returns ``None`` when nothing usable exists.  Windows-only by
    intent: callers branch on platform before calling (the POSIX answer
    is just ``shutil.which("bash")``).
    """
    custom = os.environ.get("HERMES_GIT_BASH_PATH")
    if custom and os.path.isfile(custom):
        return custom

    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        hermes_git = os.path.join(local_appdata, "hermes", "git")
        for candidate in (
            os.path.join(hermes_git, "bin", "bash.exe"),         # PortableGit
            os.path.join(hermes_git, "usr", "bin", "bash.exe"),  # MinGit
        ):
            if os.path.isfile(candidate):
                return candidate

    for base_var, rel in (
        ("ProgramFiles", os.path.join("Git", "bin", "bash.exe")),
        ("ProgramFiles", os.path.join("Git", "usr", "bin", "bash.exe")),
        ("ProgramFiles(x86)", os.path.join("Git", "bin", "bash.exe")),
        ("LOCALAPPDATA", os.path.join("Programs", "Git", "bin", "bash.exe")),
    ):
        base = os.environ.get(base_var)
        if base:
            candidate = os.path.join(base, rel)
            if os.path.isfile(candidate):
                return candidate

    found = shutil.which("bash")
    if found and (
        "\\system32\\" in found.lower() or "\\windowsapps\\" in found.lower()
    ):
        return None  # WSL launcher / Store stub — cannot see C:\ paths
    return found


# -----------------------------------------------------------------------------
# Pipe introspection (Windows)
# -----------------------------------------------------------------------------


def windows_pipe_readable_bytes(fd: int) -> Optional[int]:
    """Return the bytes readable *right now* on a pipe fd, or ``None`` when the
    pipe is broken / the fd is unusable (the EOF equivalent).

    ``select.select()`` only works on sockets on Windows, so non-blocking pipe
    drains poll this instead: ``os.read(fd, n)`` is guaranteed not to block
    whenever this reports > 0 available bytes.

    Built on ``PeekNamedPipe``, which works on anonymous pipes too — both
    ``subprocess.PIPE`` and ``os.pipe()`` create them.  Pipe fds only; on a
    non-pipe fd the underlying call fails and this reports ``None``.

    Failure mapping: once every write handle is closed AND the buffer is
    drained, ``PeekNamedPipe`` fails with ``ERROR_BROKEN_PIPE`` — exactly the
    moment a POSIX ``read()`` would return ``b""``.  While data is still
    buffered the call keeps succeeding, so no tail output is lost.  Any other
    failure (handle closed under us, not a pipe) also returns ``None``: the
    caller should stop reading in every such case.

    Returns ``None`` on non-Windows hosts, keeping this module's "no-op on
    POSIX" guarantee — callers there use ``select.select([fd], [], [], t)``.
    """
    if not IS_WINDOWS:
        return None
    try:
        handle = msvcrt.get_osfhandle(fd)
    except (OSError, ValueError):
        return None  # fd already closed / not a CRT fd
    avail = wintypes.DWORD(0)
    ok = _PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
    if not ok:
        return None  # broken pipe (all writers gone) or invalid handle
    return avail.value


# -----------------------------------------------------------------------------
# Detached / hidden process creation
# -----------------------------------------------------------------------------


# Win32 CreationFlags — defined here rather than imported from subprocess
# because CREATE_NO_WINDOW and DETACHED_PROCESS aren't guaranteed to be
# present on stdlib subprocess on older Pythons or non-Windows builds.
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
# Escape any Win32 job object the parent process belongs to. Without this,
# a detached child still inherits its parent's job object membership, and
# when that parent (Electron, Tauri, Windows Terminal, the Desktop GUI's
# bootstrap-installer) dies, the OS tears down the whole job — taking the
# "detached" child with it. Critical for the post-update gateway watcher:
# Electron spawns the Tauri updater inside its own job, the updater spawns
# the watcher subprocess; without BREAKAWAY the watcher dies the instant
# Electron exits, so the gateway never gets respawned after a `hermes
# update` triggered from the GUI. See fix/windows-gateway-reliability.
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def windows_detach_flags() -> int:
    """Return Win32 creationflags that detach a child from the parent
    console and process group.  0 on non-Windows.

    Pair with ``start_new_session=False`` (default) when calling
    subprocess.Popen — on POSIX use ``start_new_session=True`` instead,
    which maps to ``os.setsid()`` in the child.

    Rationale:
    - ``CREATE_NEW_PROCESS_GROUP`` — child has its own process group so
      Ctrl+C in the parent console doesn't propagate.
    - ``DETACHED_PROCESS`` — child has no console at all.  Necessary for
      background daemons (gateway watchers, update respawners) because
      without it, closing the console kills the child.
    - ``CREATE_NO_WINDOW`` — suppress the brief cmd flash that would
      otherwise appear when launching a console app.  Redundant with
      DETACHED_PROCESS but explicit for clarity.
    - ``CREATE_BREAKAWAY_FROM_JOB`` — escape any job object the parent is
      in.  Electron (Desktop app) and Tauri (bootstrap installer) wrap
      their children in job objects; without breakaway, those children
      die when the parent process exits even if they were spawned with
      DETACHED_PROCESS.  This was the missing flag that made the
      post-update gateway respawn watcher silently die alongside the
      Tauri updater after the Electron Desktop's update flow finished.

    If a process is in a job that disallows breakaway (rare —
    JOB_OBJECT_LIMIT_BREAKAWAY_OK isn't set), CreateProcess returns
    ERROR_ACCESS_DENIED.  Python surfaces that as ``PermissionError``
    on the ``subprocess.Popen`` call.  Callers in this codebase already
    wrap detached spawns in ``try/except OSError`` and fall back to a
    cmd.exe wrapper, so the breakaway-denied case degrades gracefully
    rather than crashing.
    """
    if not IS_WINDOWS:
        return 0
    return (
        _CREATE_NEW_PROCESS_GROUP
        | _DETACHED_PROCESS
        | _CREATE_NO_WINDOW
        | _CREATE_BREAKAWAY_FROM_JOB
    )


def windows_detach_flags_without_breakaway() -> int:
    """Same as :func:`windows_detach_flags` minus ``CREATE_BREAKAWAY_FROM_JOB``.

    The docstring on :func:`windows_detach_flags` notes that a process in
    a job which disallows breakaway (no ``JOB_OBJECT_LIMIT_BREAKAWAY_OK``)
    will see ``ERROR_ACCESS_DENIED`` from CreateProcess, surfacing as
    ``OSError`` (``PermissionError``) on the ``subprocess.Popen`` call.
    Callers that want to recover — by retrying without the breakaway
    bit — can pair the two helpers symbolically rather than coding the
    ``& ~0x01000000`` magic at every site:

    .. code-block:: python

        try:
            subprocess.Popen(argv, creationflags=windows_detach_flags(), …)
        except OSError:
            subprocess.Popen(
                argv,
                creationflags=windows_detach_flags_without_breakaway(),
                …,
            )

    See ``gateway_windows.py::_spawn_detached`` for the canonical
    implementation of this pattern.  Returns 0 on non-Windows.
    """
    if not IS_WINDOWS:
        return 0
    return _CREATE_NEW_PROCESS_GROUP | _DETACHED_PROCESS | _CREATE_NO_WINDOW


def windows_hide_flags() -> int:
    """Return Win32 creationflags that merely hide the child's console
    window without detaching the child.  0 on non-Windows.

    Use for short-lived console apps spawned as part of a larger
    operation (``taskkill``, ``where``, version probes) where we want no
    flash but also want to collect stdout/exit code synchronously.

    The key difference from :func:`windows_detach_flags`: NO
    ``DETACHED_PROCESS`` — the child still inherits stdio handles so
    ``capture_output=True`` works.  ``DETACHED_PROCESS`` would sever
    stdio and break stdout capture.
    """
    if not IS_WINDOWS:
        return 0
    return _CREATE_NO_WINDOW


def run_text_capture(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | os.PathLike | None = None,
) -> subprocess.CompletedProcess:
    """``subprocess.run(argv, capture_output=True, text=True, timeout=timeout)``
    that reliably honours ``timeout`` on Windows.

    The stdlib hang this works around: when the spawned child itself spawns a
    grandchild that inherits the capture (stdout/stderr) pipe handles, the
    grandchild keeps the pipe's write end open. ``subprocess.run`` kills only
    the *direct* child on timeout, then (on Windows) calls ``communicate()`` a
    second time to drain the pipes — which blocks forever on the reader-thread
    join because the pipe never reaches EOF. A wedged CLI therefore hangs the
    caller indefinitely instead of timing out at ``timeout`` seconds.

    The fix: **capture into temporary files rather than pipes.** A tree-kill
    alone is not enough — measured against a real wedged `npm audit` on
    Windows, a nominal 30s budget still cost 75s: `taskkill /T /F` itself ran
    11.6s and was abandoned at its own cap, the post-kill drain burned its full
    10s because the surviving grandchild still held the pipe, and
    ``Popen.__exit__`` then blocked a further 22.8s merely *closing* a pipe
    whose reader thread was still parked in a blocking read. Every one of
    those costs is a property of the capture pipes and its reader threads, so
    the pipes have to go. With file-backed stdio there are no reader threads,
    nothing to drain, and closing a file handle cannot block — a grandchild
    that outlives its parent just writes into a temp file nobody reads.

    On timeout we still tree-kill (``taskkill /T /F`` on Windows, ``killpg``
    on POSIX) as a best effort not to leak the process, but correctness of the
    bound no longer depends on that kill succeeding: we raise as soon as it
    returns and never wait on the child again.

    Returns a :class:`subprocess.CompletedProcess`; raises
    :class:`subprocess.TimeoutExpired` on timeout (same as ``subprocess.run``)
    so existing ``except (OSError, subprocess.TimeoutExpired)`` handlers keep
    working. May raise ``OSError`` / ``FileNotFoundError`` at spawn, also like
    ``subprocess.run``.

    ``cwd`` is passed straight through to :class:`subprocess.Popen` for callers
    that must run the command from a particular directory (``npm audit`` in
    ``hermes doctor``, for one).
    """
    if IS_WINDOWS:
        popen_kwargs: dict = {"creationflags": _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}
    else:
        # Own session/process group so killpg() on timeout reaches grandchildren.
        popen_kwargs = {"start_new_session": True}

    # Binary temp files + an explicit decode rather than text=True: we own the
    # handles, so the decoding is ours to make deterministic (utf-8 with
    # replacement, \r\n normalized) instead of locale-dependent.
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(
            list(argv),
            stdout=out_f,
            stderr=err_f,
            cwd=cwd,
            **popen_kwargs,
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Best effort — we do NOT wait on the child again afterwards, so a
            # kill that fails costs us nothing but a lingering process.
            _tree_kill(proc)
            raise subprocess.TimeoutExpired(proc.args, timeout)
        return subprocess.CompletedProcess(
            proc.args, proc.returncode, _read_text(out_f), _read_text(err_f),
        )


def _read_text(handle) -> str:
    """Rewind a captured temp file and decode it the way ``text=True`` would."""
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace").replace("\r\n", "\n")


def _tree_kill(proc: subprocess.Popen) -> None:
    """Kill ``proc`` and its entire descendant tree; never raises.

    Windows: ``taskkill /PID <pid> /T /F`` — the documented primitive for a
    tree-kill, mirroring ``tools.process_registry._terminate_host_pid``. We
    can't use a softer signal: there is no Windows SIGTERM that cascades
    through a process group, and ``/T`` without ``/F`` won't reach a windowless
    child. POSIX: ``killpg(SIGKILL)`` reaches the grandchildren because the
    child was started in its own session (``start_new_session=True``). Either
    way this is best effort: ``run_text_capture`` captures into files, not
    pipes, so its budget holds whether or not the kill lands — the point here
    is only to avoid leaving an abandoned process tree behind. Measured at
    11.6s against a real ``npm audit`` tree on a loaded Windows host, which is
    why the ``taskkill`` call carries its own timeout.
    """
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()  # at least reap the direct child
            except OSError:
                pass
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def windows_detach_popen_kwargs() -> dict:
    """Return a dict of Popen kwargs that detach a child on Windows and
    fall back to the POSIX equivalent (``start_new_session=True``) on
    Linux/macOS.

    Usage pattern:

    .. code-block:: python

        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            **windows_detach_popen_kwargs(),
        )

    This replaces the unsafe-on-Windows pattern:

    .. code-block:: python

        subprocess.Popen(..., start_new_session=True)

    which silently fails to detach on Windows (the flag is accepted but
    has no effect — the child stays attached to the parent's console
    and dies when the console closes).
    """
    if IS_WINDOWS:
        return {"creationflags": windows_detach_flags()}
    return {"start_new_session": True}
