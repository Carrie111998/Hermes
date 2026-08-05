"""Detached subprocess spawning with durable, restart-independent exit-code capture.

Wraps a command so its exit code is written to disk by the child's own
wrapper (a shell command on POSIX, a small Python helper on Windows)
rather than relying on the parent's ``Popen`` handle surviving long enough
to ``poll()`` it. That independence is the whole point: it's what lets
status survive a restart the child itself may cause (e.g. a self-updating
``hermes update`` restarting the gateway process that spawned it).

Extracted from ``gateway/slash_commands.py``'s original update-watcher
spawn logic (the update slash-command handler) so the gateway's
chat-triggered update path and the dashboard's HTTP-triggered path share
one implementation instead of two copies of the same Windows/POSIX
branching drifting apart over time.

The exit-code file is written with a genuine tmp-then-replace atomic swap
(``mv``/``os.replace()``), not a bare redirect — a reader can never observe
a truncated-but-not-yet-written file.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional


def spawn_with_exit_capture(
    cmd: List[str],
    *,
    output_path: Path,
    exit_code_path: Path,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    append_output: bool = True,
) -> subprocess.Popen:
    """Spawn ``cmd`` detached; ``exit_code_path`` receives the real exit code.

    The returned ``Popen`` is the wrapper process (shell on POSIX, a small
    Python helper on Windows), not ``cmd`` directly — but its own exit
    status is made to mirror ``cmd``'s (see ``exit $rc`` / ``sys.exit(rc)``
    below), so polling it via ``.poll()`` for a fast, same-process status
    read agrees with whatever ``exit_code_path`` eventually says. Callers
    that outlive the wrapper (e.g. after a restart) should read
    ``exit_code_path`` instead of trusting this handle.

    Clears any stale ``exit_code_path`` (and its ``.tmp``) before spawning,
    so its mere existence afterward always means *this* run finished.
    """
    exit_code_path.unlink(missing_ok=True)
    Path(str(exit_code_path) + ".tmp").unlink(missing_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    exit_code_path.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        from hermes_cli._subprocess_compat import windows_detach_popen_kwargs

        helper = textwrap.dedent(
            """
            import os, subprocess, sys
            output_path = sys.argv[1]
            exit_code_path = sys.argv[2]
            mode = sys.argv[3]
            cmd = sys.argv[4:]
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            with open(output_path, mode) as f:
                proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env)
                rc = proc.wait(timeout=3600)
            tmp = exit_code_path + ".tmp"
            with open(tmp, "w") as f:
                f.write(str(rc))
            os.replace(tmp, exit_code_path)
            sys.exit(rc)
            """
        ).strip()
        popen_kwargs: Dict = dict(windows_detach_popen_kwargs())
        if cwd is not None:
            popen_kwargs["cwd"] = cwd
        if env is not None:
            popen_kwargs["env"] = env
        return subprocess.Popen(
            [
                sys.executable, "-c", helper,
                str(output_path), str(exit_code_path),
                "ab" if append_output else "wb",
                *cmd,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **popen_kwargs,
        )

    cmd_str = " ".join(shlex.quote(part) for part in cmd)
    exit_code_tmp = f"{exit_code_path}.tmp"
    redirect_op = ">>" if append_output else ">"
    # `exit $rc` at the end makes the wrapper's OWN exit status mirror
    # cmd's, not the trailing `mv`'s — so a fast in-process .poll() on this
    # Popen agrees with what exit_code_path will say once it lands. Avoid
    # `status=$?`: `status` is a read-only special parameter in zsh, and
    # this template gets copied into other zsh-adjacent contexts.
    wrapper_cmd = (
        f"{cmd_str} {redirect_op} {shlex.quote(str(output_path))} 2>&1; "
        f"rc=$?; "
        f"printf '%s' \"$rc\" > {shlex.quote(exit_code_tmp)}; "
        f"mv {shlex.quote(exit_code_tmp)} {shlex.quote(str(exit_code_path))}; "
        f"exit $rc"
    )

    popen_kwargs: Dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }
    if cwd is not None:
        popen_kwargs["cwd"] = cwd
    if env is not None:
        popen_kwargs["env"] = env

    setsid_bin = shutil.which("setsid")
    if setsid_bin:
        return subprocess.Popen([setsid_bin, "bash", "-c", wrapper_cmd], **popen_kwargs)
    return subprocess.Popen(["bash", "-c", wrapper_cmd], **popen_kwargs)


def read_exit_code(exit_code_path: Path) -> Optional[int]:
    """Read a completed run's exit code, or ``None`` if it hasn't finished.

    Mirrors gateway's own read-side tolerance: an empty/unparseable read
    (the file can only be observed post-``os.replace()`` here, but callers
    reading concurrently with an interrupted process are still handled
    gracefully) falls back to ``1`` rather than raising.
    """
    if not exit_code_path.exists():
        return None
    try:
        raw = exit_code_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return 1
    try:
        return int(raw)
    except ValueError:
        return 1
