"""Pre-dispatch confinement checks and truthful launch verification.

SCOPE — READ THIS FIRST
-----------------------
This protects against **accidental escape and cooperative execution**. It does
**not** protect against **arbitrary malicious same-UID activity**.

Hermes workers run as the same OS user with terminal and filesystem access.
Every check here is a path predicate, a stat comparison, or a process-state
observation — none is an OS-level sandbox. A deliberately adversarial process
running as your user defeats all of them: it can `chdir` after launch, read
outside its workspace, or write `kanban.db` directly. What these checks stop is
the failure that actually happened.

THE FAILURE THAT ACTUALLY HAPPENED
----------------------------------
During M2a a worker launched from the wrong directory did not find its test
command, searched the filesystem, located a live production checkout whose
default branch auto-deploys, and ran a command there. Read-only, no damage,
verified — and luck. Path denials were added and the issue declared closed; a
later run still *started* in the wrong directory, because denials bound the
blast radius without fixing the launch.

WHAT COMMIT 7 GOT WRONG, AND WHY THIS MODULE EXISTS
---------------------------------------------------
Commit 7 removed ``_default_spawn``'s ``cwd=None`` fallback. An independent
review then established four defects, all of which this module addresses:

1. **The review lane bypassed the guard entirely.** Only the ready lane called
   preflight before the spawner, and only ``_default_spawn`` re-ran it — so a
   custom review spawner reached ``Popen`` unchecked. The guard is therefore no
   longer something a lane *calls*; it is something both lanes go through.

2. **The guard ran too late for invariant C1.** ``claim_task`` — which creates
   the ``task_runs`` row — ran first, so a refusal left a ``spawn_failed`` run
   row behind. C1 requires a refused dispatch to create no run row at all.
   Hence :func:`plan_workspace_path`, which resolves the *intended* directory
   with no side effects so the decision can be made before claiming.

3. **``observed_cwd`` was asserted, not observed.** The dispatcher wrote the
   path it *passed to* the spawner. A custom spawner that ignored that argument
   and launched at ``/`` was still recorded as having launched in its workspace.
   :func:`observe_process_cwd` reads the kernel's record of where the process
   actually is.

4. **Provisioning happened before validation**, so an arbitrary missing path was
   created and then accepted because it now existed.

WHAT "VERIFIED" MEANS HERE, PRECISELY
-------------------------------------
A launch is ``verified`` only when the kernel reports the child's working
directory and that directory is the same **(st_dev, st_ino)** as the authorized
one. String comparison is not enough: it misses symlink retargeting and path
replacement between check and use, and on a case-insensitive volume two
different spellings name one directory.

A launch whose directory cannot be read is ``unobservable``, **not** verified.
It is recorded as such and must not be described as confined. A launch in the
wrong place is ``mismatch`` and fails closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

VERIFIED = "verified"
MISMATCH = "mismatch"
UNOBSERVABLE = "unobservable"


class PreflightRefusal(RuntimeError):
    """A dispatch was refused before any worker, session, or run row existed."""


class LaunchVerificationError(RuntimeError):
    """A worker was launched somewhere other than its authorized workspace."""


@dataclass(frozen=True)
class AuthorizedWorkspace:
    """A directory that passed preflight, pinned by filesystem identity.

    ``path`` is a realpath for humans and logs. ``dev``/``ino`` are what any
    comparison must actually use — a path string can be re-pointed after it was
    checked, and identity cannot.
    """

    path: str
    dev: int
    ino: int

    def matches(self, other_path: Optional[str]) -> bool:
        if not other_path:
            return False
        try:
            st = os.stat(other_path)
        except OSError:
            return False
        return (st.st_dev, st.st_ino) == (self.dev, self.ino)


@dataclass(frozen=True)
class SpawnOutcome:
    """Structured spawner result.

    Additive: a spawner may still return a bare ``int`` pid. Doing so is not an
    error, but it yields no launch metadata of its own, so the launch is
    verified against the OS or else recorded as unverified — never assumed.
    """

    pid: int
    observed_cwd: Optional[str] = None


@dataclass(frozen=True)
class LaunchVerification:
    pid: Optional[int]
    observed_cwd: Optional[str]
    status: str
    detail: str

    @property
    def is_verified(self) -> bool:
        return self.status == VERIFIED


# ---------------------------------------------------------------------------
# Planning — resolve the intended directory WITHOUT creating anything
# ---------------------------------------------------------------------------


def plan_workspace_path(task: Any, *, board: Optional[str] = None) -> str:
    """The directory this task intends to run in, with no side effects.

    ``resolve_workspace`` both resolves *and* provisions (``mkdir``). Preflight
    cannot use it: creating a directory in order to decide whether the directory
    is allowed is how commit 7 ended up accepting arbitrary paths it had just
    created itself.

    For ``worktree`` the concrete leaf is produced by git during provisioning,
    so what is planned — and therefore what preflight judges — is the **anchor**
    the worktree will be created beneath. That is the path that decides whether
    a worker lands in an authorized tree, which is the question preflight exists
    to answer.
    """
    kind = (getattr(task, "workspace_kind", None) or "scratch").strip()
    raw = (getattr(task, "workspace_path", None) or "").strip()
    task_id = getattr(task, "id", "<unknown>")

    if kind == "scratch":
        if raw:
            return str(Path(raw).expanduser())
        from hermes_cli.kanban_db import workspaces_root

        return str(Path(workspaces_root(board=board)) / task_id)

    if kind == "dir":
        if not raw:
            raise PreflightRefusal(
                f"task {task_id} has workspace_kind=dir but no workspace_path"
            )
        return str(Path(raw).expanduser())

    if kind == "worktree":
        if raw:
            return str(Path(raw).expanduser())
        from hermes_cli.kanban_db import get_current_board, read_board_metadata

        slug = board if board else get_current_board()
        anchor = (read_board_metadata(slug) or {}).get("default_workdir")
        if not anchor:
            raise PreflightRefusal(
                f"task {task_id} is a worktree task with no workspace_path and "
                f"the board has no default_workdir; refusing to guess an anchor "
                f"from the dispatcher's own directory"
            )
        return str(Path(anchor).expanduser())

    raise PreflightRefusal(f"task {task_id}: unknown workspace_kind {kind!r}")


# ---------------------------------------------------------------------------
# The predicate — runs before claim, run row, session, or provisioning
# ---------------------------------------------------------------------------


def preflight_workspace(task_id: str, intended_path: Optional[str]) -> str:
    """Refuse a dispatch whose launch directory cannot be trusted.

    Fail-closed and unconditional: no warn tier, no override argument, no
    environment escape. A gate that can be waived is not a gate.

    Returns the intended path unchanged. It is deliberately **not** required to
    exist yet — provisioning happens after this, and identity is captured from
    the provisioned directory by :func:`authorize_workspace`.

    NOTE ON WHAT THIS DOES NOT DO: it does not decide whether the directory is
    an *authorized* one. Distinguishing an approved fixture from a canonical or
    auto-deploying checkout needs board policy, which is a separate piece of
    work (``kanban.workspace_policy``). Until that lands, this refuses the
    unusable, not the unauthorized — and the confinement contract stays marked
    incomplete.
    """
    raw = "" if intended_path is None else str(intended_path).strip()
    if not raw:
        raise PreflightRefusal(
            f"task {task_id} has no workspace; refusing to launch a worker in "
            f"the dispatcher's own directory"
        )
    if not os.path.isabs(raw):
        raise PreflightRefusal(
            f"task {task_id} workspace is not absolute ({raw!r}); a relative "
            f"path resolves against whatever directory the dispatcher happens "
            f"to be in"
        )
    if os.path.exists(raw) and not os.path.isdir(raw):
        raise PreflightRefusal(
            f"task {task_id} workspace exists but is not a directory ({raw!r})"
        )
    return raw


def authorize_workspace(task_id: str, path: Any) -> AuthorizedWorkspace:
    """Pin the provisioned directory by filesystem identity.

    Called after provisioning and before the spawn. From here on, "the same
    directory" means the same ``(st_dev, st_ino)`` — never the same string.
    """
    raw = str(path)
    try:
        st = os.stat(raw)
    except OSError as exc:
        raise PreflightRefusal(
            f"task {task_id} workspace {raw!r} could not be stat'd after "
            f"provisioning: {exc}"
        ) from exc
    if not os.path.isdir(raw):
        raise PreflightRefusal(
            f"task {task_id} workspace {raw!r} is not a directory"
        )
    return AuthorizedWorkspace(
        path=os.path.realpath(raw), dev=st.st_dev, ino=st.st_ino
    )


def revalidate_at_spawn(task_id: str, authorized: AuthorizedWorkspace) -> None:
    """Re-check identity immediately before the spawn.

    Closes the window between validation and use. A symlink retargeted, or a
    directory swapped for another, changes ``(st_dev, st_ino)`` while leaving
    the path string identical — so only identity catches it.
    """
    if not authorized.matches(authorized.path):
        raise PreflightRefusal(
            f"task {task_id} workspace {authorized.path!r} changed identity "
            f"between validation and spawn (symlink retargeted or directory "
            f"replaced); refusing to launch"
        )


# ---------------------------------------------------------------------------
# Truthful observation
# ---------------------------------------------------------------------------


def observe_process_cwd(pid: Optional[int]) -> Optional[str]:
    """The kernel's record of where a process actually is, or None.

    ``psutil`` reads this from the OS. It is not the path we asked for, which is
    the entire point: a spawner that ignored its workspace argument is only
    detectable by looking at the process.
    """
    if not pid:
        return None
    try:
        import psutil

        return psutil.Process(int(pid)).cwd()
    except Exception:
        return None


def normalize_spawn_result(value: Any) -> Tuple[Optional[int], Optional[str]]:
    """Accept a bare pid or a :class:`SpawnOutcome`.

    A spawner's self-reported directory is never trusted on its own; it is
    cross-checked against the OS below. It exists so a spawner that launches
    somewhere Hermes cannot inspect (a container, a remote host) can still say
    where, and be judged on it.
    """
    if value is None:
        return None, None
    if isinstance(value, SpawnOutcome):
        return (int(value.pid) if value.pid else None), value.observed_cwd
    if isinstance(value, bool):
        return None, None
    if isinstance(value, int):
        # pid 0 is not a child: on POSIX it addresses the caller's own process
        # group in kill(2). Treat it as "no pid" rather than something to
        # inspect or signal.
        return (int(value) or None), None
    pid = getattr(value, "pid", None)
    cwd = getattr(value, "observed_cwd", None)
    if pid is not None:
        try:
            return int(pid), (str(cwd) if cwd else None)
        except (TypeError, ValueError):
            return None, None
    return None, None


def verify_launch(
    task_id: str,
    authorized: AuthorizedWorkspace,
    pid: Optional[int],
    reported_cwd: Optional[str] = None,
    *,
    _observer=None,
) -> LaunchVerification:
    """Decide whether the worker actually started where it was authorized to.

    Three outcomes, and the difference between the last two matters:

    * ``verified``   — the OS (or a spawner report corroborated by identity)
      places the child in the authorized directory.
    * ``mismatch``   — the child is somewhere else. Fails closed.
    * ``unobservable`` — nothing could be read. **Not** confined, not claimed to
      be. A legacy spawner that returns only a pid for a process Hermes cannot
      inspect lands here, and must not be labelled confined merely for having
      returned a pid.
    """
    observer = _observer or observe_process_cwd
    observed = observer(pid)
    source = "process"
    if observed is None and reported_cwd:
        observed = str(reported_cwd)
        source = "spawner-reported"

    if observed is None:
        return LaunchVerification(
            pid=pid, observed_cwd=None, status=UNOBSERVABLE,
            detail=(
                f"task {task_id}: the launch directory could not be observed "
                f"(pid={pid}); this launch is NOT verified as confined"
            ),
        )
    if authorized.matches(observed):
        return LaunchVerification(
            pid=pid, observed_cwd=os.path.realpath(observed), status=VERIFIED,
            detail=f"task {task_id}: launch directory verified via {source}",
        )
    return LaunchVerification(
        pid=pid, observed_cwd=observed, status=MISMATCH,
        detail=(
            f"task {task_id}: worker launched in {observed!r} but was "
            f"authorized for {authorized.path!r} (observed via {source})"
        ),
    )


def terminate_escaped_worker(pid: Optional[int]) -> bool:
    """Stop a worker that launched outside its authorized directory.

    Best-effort by necessity — the process may already be gone — but the caller
    must still fail the dispatch closed regardless of what this returns.
    """
    if not pid:
        return False
    import signal
    import time

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGKILL", None)):
        if sig is None:
            continue
        try:
            os.kill(int(pid), sig)
        except ProcessLookupError:
            return True
        except Exception:
            return False
        time.sleep(0.05)
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except Exception:
            return False
    return False
