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

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

VERIFIED = "verified"
MISMATCH = "mismatch"
UNOBSERVABLE = "unobservable"
# A spawner said where it launched, but nothing corroborated it. Telemetry —
# never evidence. See verify_launch.
REPORTED_ONLY = "reported-only"

# How long a sandbox worker waits to be released before giving up. A worker
# that is never released must exit rather than sit forever holding a claim.
START_BARRIER_TIMEOUT_SECONDS = 120
START_BARRIER_ACK_TIMEOUT_SECONDS = 30
START_BARRIER_ENV = "HERMES_KANBAN_START_BARRIER"
START_BARRIER_TOKEN_ENV = "HERMES_KANBAN_START_BARRIER_TOKEN"
_BARRIER_GO = "GO"
_BARRIER_WAITING = "WAITING"


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

    **Only an independent observation of the process can produce VERIFIED.**

    A previous revision fell back to the spawner's own ``observed_cwd`` when the
    OS could not be read, and marked the launch verified if that *string*
    resolved to the authorized inode. A spawner that ignored its workspace and
    launched elsewhere could therefore report the authorized path and be
    believed — the exact failure the observation was added to catch. A report is
    now telemetry: it is recorded, it can still produce a MISMATCH (a spawner
    naming somewhere unauthorized is taken at its word for refusal), but it can
    never produce VERIFIED.

    Four outcomes:

    * ``verified``      — the OS places the child in the authorized directory.
    * ``mismatch``      — observed, or self-reported, somewhere else. Fails closed.
    * ``reported-only`` — a report with no corroboration. Not confined.
    * ``unobservable``  — nothing could be read at all. Not confined.

    The last two differ from ``verified`` in exactly the way that matters: in
    sandbox mode the caller must terminate the worker. Only an authenticated
    adapter that attests the process independently could raise a report to
    evidence, and no such adapter exists.
    """
    observer = _observer or observe_process_cwd
    observed = observer(pid)

    if observed is not None:
        if authorized.matches(observed):
            return LaunchVerification(
                pid=pid, observed_cwd=os.path.realpath(observed), status=VERIFIED,
                detail=f"task {task_id}: launch directory verified by OS observation",
            )
        return LaunchVerification(
            pid=pid, observed_cwd=observed, status=MISMATCH,
            detail=(
                f"task {task_id}: worker launched in {observed!r} but was "
                f"authorized for {authorized.path!r} (OS-observed)"
            ),
        )

    if reported_cwd:
        if not authorized.matches(reported_cwd):
            # Taking a self-report at its word to REFUSE is safe; the reverse
            # is not.
            return LaunchVerification(
                pid=pid, observed_cwd=str(reported_cwd), status=MISMATCH,
                detail=(
                    f"task {task_id}: spawner reported launching in "
                    f"{reported_cwd!r}, which is not the authorized "
                    f"{authorized.path!r}"
                ),
            )
        return LaunchVerification(
            pid=pid, observed_cwd=str(reported_cwd), status=REPORTED_ONLY,
            detail=(
                f"task {task_id}: the spawner reported the authorized directory "
                f"but nothing corroborated it (pid={pid}); this launch is NOT "
                f"verified as confined"
            ),
        )

    return LaunchVerification(
        pid=pid, observed_cwd=None, status=UNOBSERVABLE,
        detail=(
            f"task {task_id}: the launch directory could not be observed "
            f"(pid={pid}); this launch is NOT verified as confined"
        ),
    )


@dataclass(frozen=True)
class OwnedProcess:
    """A worker Hermes can prove it owns, and can clean up completely.

    A bare pid is not ownership. It can be reused by an unrelated process
    between the spawn and the cleanup, and signalling it kills only the leader
    while descendants keep running — a benign reproduction left a sleeping
    grandchild alive after the leader was terminated.

    ``create_time`` is the PID-reuse guard: the kernel's start timestamp for
    this exact process. ``pgid`` is the cleanup handle: the default spawner uses
    ``start_new_session=True``, so the worker leads its own process group and the
    whole tree can be signalled at once.
    """

    pid: int
    create_time: Optional[float] = None
    pgid: Optional[int] = None

    @property
    def has_tree_handle(self) -> bool:
        return self.pgid is not None and self.pgid > 0

    def is_alive(self) -> bool:
        return _process_matches(self.pid, self.create_time)


@dataclass(frozen=True)
class TerminationResult:
    ok: bool
    detail: str
    survivors: tuple = ()


def _process_matches(pid: Optional[int], create_time: Optional[float]) -> bool:
    """True when *pid* is still the process we owned — not a recycled number."""
    if not pid:
        return False
    try:
        import psutil

        proc = psutil.Process(int(pid))
        # A zombie has already exited; its entry lingers only until the parent
        # reaps it. Counting one as alive makes every successful termination
        # look like a containment failure.
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
        except Exception:
            pass
        if create_time is None:
            return True
        return abs(proc.create_time() - float(create_time)) < 0.001
    except Exception:
        return False


def own_process(pid: Optional[int]) -> Optional[OwnedProcess]:
    """Bind a spawned worker to an identity Hermes can verify later.

    Returns None when the process cannot be inspected at all — which a sandbox
    dispatch must treat as a failure, because a worker it cannot identify is a
    worker it cannot clean up.
    """
    if not pid:
        return None
    create_time = None
    pgid = None
    try:
        import psutil

        create_time = psutil.Process(int(pid)).create_time()
    except Exception:
        return None
    try:
        pgid = os.getpgid(int(pid))
    except Exception:
        pgid = None
    return OwnedProcess(pid=int(pid), create_time=create_time, pgid=pgid)


def _descendants(pid: int) -> list:
    try:
        import psutil

        return psutil.Process(int(pid)).children(recursive=True)
    except Exception:
        return []


def terminate_worker_tree(owned: Optional[OwnedProcess], *,
                          grace_seconds: float = 2.0) -> TerminationResult:
    """Stop a worker and everything it started. Verified, not assumed.

    Signals the process GROUP when one is available, so descendants go too, then
    confirms nothing survived. The caller must treat ``ok=False`` as a failure to
    contain — reporting a dispatch safely closed while descendants keep running
    is precisely the outcome this exists to prevent.
    """
    import signal
    import time

    if owned is None:
        return TerminationResult(False, "no owned process handle to terminate")
    if not _process_matches(owned.pid, owned.create_time):
        return TerminationResult(True, "process already gone (or pid recycled)")

    own_group = None
    try:
        own_group = os.getpgid(0)
    except Exception:
        pass

    # Never signal our OWN process group: that would take the dispatcher with it.
    use_group = owned.has_tree_handle and owned.pgid != own_group

    for sig_name in ("SIGTERM", "SIGKILL"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        if use_group:
            try:
                os.killpg(int(owned.pgid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        else:
            # No usable group handle. Enumerate descendants FIRST — killing the
            # leader can reparent them and lose the relationship — then signal
            # children before the leader.
            for child in _descendants(owned.pid):
                try:
                    os.kill(child.pid, sig)
                except Exception:
                    pass
            try:
                os.kill(int(owned.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if not _process_matches(owned.pid, owned.create_time):
                break
            time.sleep(0.05)
        if not _process_matches(owned.pid, owned.create_time):
            break

    survivors = []
    if _process_matches(owned.pid, owned.create_time):
        survivors.append(owned.pid)
    survivors.extend(c.pid for c in _descendants(owned.pid))
    if survivors:
        return TerminationResult(
            False,
            f"worker tree not fully terminated; survivors: {survivors[:8]}",
            tuple(survivors),
        )
    return TerminationResult(True, "worker tree terminated")


# Kept for callers that only have a pid. Prefer terminate_worker_tree.
def terminate_escaped_worker(pid: Optional[int]) -> bool:
    return terminate_worker_tree(own_process(pid)).ok


# ---------------------------------------------------------------------------
# The cooperative start barrier
# ---------------------------------------------------------------------------


class StartBarrier:
    """Hold a worker before it does anything, until confinement is proven.

    Verification after the spawn bounds how long a worker can run in the wrong
    place; it does not prevent the work. A benign reproduction wrote a marker
    file before mismatch verification terminated it. So in sandbox mode the
    worker is launched already waiting: it blocks at CLI entry, before any
    agent, model call, or tool execution, until Hermes has

      * revalidated the final workspace and allowed-root identities,
      * independently observed the process's actual working directory,
      * bound an owned process handle, and
      * durably stored the audit record.

    **Cooperative by construction.** The worker chooses to wait because Hermes
    asks it to. A process that ignores the barrier is not stopped by it — which
    is the same same-user limit that applies to everything else here, and is why
    this is a workflow-safety mechanism rather than a sandbox.
    """

    def __init__(self, path: str, token: str) -> None:
        self.path = path
        self.token = token
        self._released = False

    @classmethod
    def create(cls, directory: str, task_id: str) -> "StartBarrier":
        os.makedirs(directory, exist_ok=True)
        safe_task = "".join(
            ch if ch.isalnum() or ch in "-_" else "_" for ch in str(task_id)
        )[:80] or "task"
        # A task can be retried. The barrier identity therefore belongs to this
        # dispatch attempt, never to the durable task id. Exclusive creation
        # makes a stale successful GO file unusable by a later attempt.
        for _ in range(10):
            token = secrets.token_hex(32)
            path = os.path.join(directory, f"{safe_task}-{token[:20]}.barrier")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # pragma: no cover - cryptographic collision
                continue
            try:
                payload = json.dumps({"version": 1, "token": token})
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return cls(path, token)
        raise PreflightRefusal("could not allocate a unique worker start barrier")

    @property
    def waiting_path(self) -> str:
        return f"{self.path}.waiting"

    @property
    def release_path(self) -> str:
        return f"{self.path}.go"

    def env(self) -> dict:
        return {
            START_BARRIER_ENV: self.path,
            START_BARRIER_TOKEN_ENV: self.token,
        }

    def wait_until_waiting(
        self, pid: int, *, timeout_seconds: int = START_BARRIER_ACK_TIMEOUT_SECONDS,
        _sleep=None,
    ) -> bool:
        """Require a worker acknowledgement bound to this attempt and pid."""
        sleep = _sleep or time.sleep
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while time.monotonic() < deadline:
            if self.is_waiting(pid):
                return True
            sleep(0.05)
        return False

    def is_waiting(self, pid: int) -> bool:
        """True only while this attempt's worker still advertises WAITING."""
        try:
            with open(self.waiting_path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return (
                payload.get("state") == _BARRIER_WAITING
                and payload.get("token") == self.token
                and int(payload.get("pid")) == int(pid)
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def release(self) -> None:
        """Let the worker start. Called only after every check has passed."""
        tmp = f"{self.release_path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"state": _BARRIER_GO, "token": self.token}, fh)
        os.replace(tmp, self.release_path)
        self._released = True

    def abort(self) -> None:
        """Remove the barrier without releasing it: the worker will time out."""
        for path in (self.release_path, self.waiting_path, self.path):
            try:
                os.unlink(path)
            except OSError:
                pass

    @property
    def released(self) -> bool:
        return self._released


def wait_for_start_barrier(
    *, timeout_seconds: int = START_BARRIER_TIMEOUT_SECONDS,
    _env: Optional[dict] = None, _sleep=None,
) -> bool:
    """Worker side: block until released, or exit.

    Returns True when the worker may proceed. Returns False when it must not —
    the caller exits non-zero rather than starting work Hermes never authorized.
    No barrier configured means no barrier: open-mode boards are unaffected.
    """
    env = _env if _env is not None else os.environ
    path = (env.get(START_BARRIER_ENV) or "").strip()
    token = (env.get(START_BARRIER_TOKEN_ENV) or "").strip()
    if not path and not token:
        return True
    if not path or not token:
        return False
    waiting_path = f"{path}.waiting"
    release_path = f"{path}.go"

    try:
        with open(path, "r", encoding="utf-8") as fh:
            request = json.load(fh)
        if request.get("version") != 1 or request.get("token") != token:
            return False
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False

    acknowledgement = json.dumps({
        "state": _BARRIER_WAITING,
        "token": token,
        "pid": os.getpid(),
    }).encode("utf-8")
    try:
        fd = os.open(waiting_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, acknowledgement)
        finally:
            os.close(fd)
    except OSError:
        return False

    sleep = _sleep or time.sleep
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    while time.monotonic() < deadline:
        if not os.path.exists(path):
            try:
                os.unlink(waiting_path)
            except OSError:
                pass
            return False
        try:
            with open(release_path, "r", encoding="utf-8") as fh:
                release = json.load(fh)
                if (release.get("state") == _BARRIER_GO
                        and release.get("token") == token):
                    for cleanup in (release_path, waiting_path, path):
                        try:
                            os.unlink(cleanup)
                        except OSError:
                            pass
                    return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        sleep(0.1)
    try:
        os.unlink(waiting_path)
    except OSError:
        pass
    return False
