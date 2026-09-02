"""Linux cgroup-v2 containment for durable Kanban workers."""

from __future__ import annotations

import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence


_ENV_FLAG = "HERMES_KANBAN_CGROUP_CONTAINMENT"
_ENV_ROOT = "HERMES_KANBAN_CGROUP_ROOT"
_ENV_ROOT_INODE = "HERMES_KANBAN_CGROUP_ROOT_INODE"
_CGROUP_FS = Path("/sys/fs/cgroup")
_NAME_RE = re.compile(r"^hermes-kanban-r[0-9]+-[a-f0-9]{24}$")


class ContainmentError(RuntimeError):
    """Raised when containment policy or kernel state cannot be trusted."""


class ContainmentRetirementPending(ContainmentError):
    """A committed worker identity is reserved for trusted retirement."""

    def __init__(self, message: str, *, certified: bool) -> None:
        super().__init__(message)
        self.certified = bool(certified)


class WorkerSpawn:
    """A worker blocked behind a pipe gate inside its exact cgroup."""

    def __init__(
        self,
        process: subprocess.Popen,
        gate_fd: int,
        task_id: str,
        run_id: int,
        claim_lock: str,
        cgroup_path: str,
        cgroup_inode: int,
    ) -> None:
        self._process = process
        self._gate_fd = gate_fd
        self.task_id = task_id
        self.run_id = int(run_id)
        self.claim_lock = claim_lock
        self.cgroup_path = cgroup_path
        self.cgroup_inode = int(cgroup_inode)
        self.pid = int(process.pid)
        self.released = False
        self.aborted = False
        self._abort_result: dict[str, Any] | None = None

    def _close_gate(self) -> None:
        if self._gate_fd < 0:
            return
        fd, self._gate_fd = self._gate_fd, -1
        os.close(fd)

    def release(self) -> None:
        """Release the helper only after durable ownership is committed."""
        if self.released:
            return
        if self.aborted or self._gate_fd < 0:
            raise ContainmentRetirementPending(
                "worker spawn gate is no longer releasable", certified=False
            )
        try:
            written = os.write(self._gate_fd, b"1")
            if written != 1:
                raise OSError("short write to worker spawn gate")
            self._close_gate()
            self.released = True
        except OSError as exc:
            try:
                self._close_gate()
            except OSError:
                pass
            termination = kill_cgroup(self.cgroup_path, self.cgroup_inode)
            certified = bool(termination.get("containment_certified"))
            raise ContainmentRetirementPending(
                f"worker spawn gate release uncertain: {exc}", certified=certified
            ) from exc

    def abort(self, *, unlink: bool = False) -> dict[str, Any]:
        """Keep the worker blocked, kill the exact cgroup, and certify it."""
        if self.aborted:
            return dict(self._abort_result or {})
        if self.released:
            raise ContainmentRetirementPending(
                "released worker requires durable retirement", certified=False
            )
        try:
            self._close_gate()
        except OSError:
            pass
        termination = kill_cgroup(self.cgroup_path, self.cgroup_inode)
        certified = bool(termination.get("containment_certified"))
        self.aborted = True
        self._abort_result = dict(termination)
        if not certified:
            raise ContainmentRetirementPending(
                "worker spawn abort could not certify cgroup extinction",
                certified=False,
            )
        if unlink:
            cleaned = cleanup_cgroup(self.cgroup_path, self.cgroup_inode)
            termination["cleaned"] = cleaned
            self._abort_result = dict(termination)
            if not cleaned:
                raise ContainmentRetirementPending(
                    "pre-registration worker cgroup cleanup was not certified",
                    certified=True,
                )
        return dict(termination)


def _effective_uid() -> int:
    """Return the Linux effective uid without importing a Windows-only hazard."""
    if sys.platform != "linux":
        raise ContainmentError("cgroup-v2 containment is Linux-only")
    getter = getattr(os, "geteuid", None)
    if getter is None:
        raise ContainmentError("effective uid is unavailable")
    return int(getter())


def _current_cgroup_dir() -> Path:
    """Return the delegated worker root from unified cgroup-v2 membership."""
    if sys.platform != "linux":
        raise ContainmentError("cgroup-v2 containment is Linux-only")
    root_override = os.environ.get(_ENV_ROOT)
    inode_override = os.environ.get(_ENV_ROOT_INODE)
    if root_override is not None or inode_override is not None:
        if not root_override or not inode_override:
            raise ContainmentError(
                "explicit cgroup root and root inode must be configured together"
            )
        root = Path(root_override)
        if (
            not root.is_absolute()
            or ".." in root.parts
            or os.path.normpath(root_override) != root_override
        ):
            raise ContainmentError("explicit cgroup root is not canonical")
        try:
            expected_inode = int(inode_override)
            observed = os.stat(root, follow_symlinks=False)
        except (OSError, TypeError, ValueError) as exc:
            raise ContainmentError(f"explicit cgroup root is unavailable: {exc}") from exc
        if (
            expected_inode <= 0
            or not stat.S_ISDIR(observed.st_mode)
            or int(observed.st_ino) != expected_inode
            or int(observed.st_uid) != _effective_uid()
        ):
            raise ContainmentError(
                "explicit cgroup root inode or runtime ownership mismatch"
            )
        return root
    try:
        root_st = os.stat(_CGROUP_FS, follow_symlinks=False)
        controllers_st = os.stat(
            _CGROUP_FS / "cgroup.controllers", follow_symlinks=False
        )
    except OSError as exc:
        raise ContainmentError(f"unified cgroup-v2 filesystem unavailable: {exc}") from exc
    if not stat.S_ISDIR(root_st.st_mode) or not stat.S_ISREG(controllers_st.st_mode):
        raise ContainmentError("unified cgroup-v2 filesystem unavailable")
    try:
        lines = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise ContainmentError(f"cannot read /proc/self/cgroup: {exc}") from exc
    relative = next(
        (
            parts[2]
            for line in lines
            if len(parts := line.split(":", 2)) == 3
            and parts[0] == "0"
            and parts[1] == ""
        ),
        None,
    )
    if not relative or not relative.startswith("/") or ".." in Path(relative).parts:
        raise ContainmentError("unusable unified cgroup-v2 membership")
    leaf = _CGROUP_FS / relative.lstrip("/")
    if leaf.name == "control" or _NAME_RE.fullmatch(leaf.name):
        return leaf.parent
    raise ContainmentError(
        "cgroup containment requires a delegated control or worker subtree"
    )


def _validated_path(path_text: str) -> Path:
    """Accept exactly one named worker child below the authorized root."""
    path = Path(path_text)
    root = _current_cgroup_dir()
    if (
        not path.is_absolute()
        or path.parent != root
        or not _NAME_RE.fullmatch(path.name)
    ):
        raise ContainmentError("persisted cgroup path is outside the worker namespace")
    return path


def _open_verified_dir(path_text: str, expected_inode: int) -> tuple[Path, int]:
    path = _validated_path(path_text)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        parent_fd = os.open(path.parent, flags)
    except OSError as exc:
        raise ContainmentError(f"authorized cgroup root is unavailable: {exc}") from exc
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            held = os.fstat(fd)
            named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(held.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or int(held.st_ino) != int(expected_inode)
                or (held.st_dev, held.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ContainmentError("worker cgroup inode mismatch")
            return path, fd
        except Exception:
            os.close(fd)
            raise
    except FileNotFoundError as exc:
        raise ContainmentError("registered worker cgroup is missing") from exc
    except OSError as exc:
        raise ContainmentError(f"cannot open registered worker cgroup: {exc}") from exc
    finally:
        os.close(parent_fd)


def _read_populated_fd(dir_fd: int) -> bool:
    try:
        fd = os.open(
            "cgroup.events",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=dir_fd,
        )
        try:
            raw = os.read(fd, 4096).decode("ascii", "strict")
        finally:
            os.close(fd)
    except (OSError, UnicodeError) as exc:
        raise ContainmentError(f"cannot read cgroup.events: {exc}") from exc
    populated = [
        fields[1]
        for line in raw.splitlines()
        if len(fields := line.split()) == 2 and fields[0] == "populated"
    ]
    if populated not in (["0"], ["1"]):
        raise ContainmentError("cgroup.events lacks one valid populated field")
    return populated[0] == "1"


def _runtime_alias_needs_mount(path: str) -> bool:
    """Return whether bubblewrap shadows the lexical alias from ``/``."""
    normalized = os.path.normpath(path)
    return any(
        normalized == root or normalized.startswith(root + os.sep)
        for root in ("/root", "/tmp")
    )


def _create_worker_cgroup(run_id: int) -> tuple[Path, int]:
    """Create one unpredictable worker child below the authorized root."""
    if isinstance(run_id, bool) or int(run_id) <= 0:
        raise ContainmentError("worker run id must be a positive integer")
    root = _current_cgroup_dir()
    name = f"hermes-kanban-r{int(run_id)}-{secrets.token_hex(12)}"
    path = root / name
    try:
        path.mkdir(mode=0o700)
        observed = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ContainmentError(f"cannot create worker cgroup: {exc}") from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or int(observed.st_uid) != _effective_uid()
        or path.parent != root
        or not _NAME_RE.fullmatch(path.name)
    ):
        try:
            path.rmdir()
        except OSError:
            pass
        raise ContainmentError("created worker cgroup identity is untrusted")
    return path, int(observed.st_ino)


def _move_pid_to_cgroup(cgroup_path: str, cgroup_inode: int, pid: int) -> None:
    """Move a blocked helper into an exact worker cgroup and verify readback."""
    if isinstance(pid, bool) or int(pid) <= 0:
        raise ContainmentError("worker pid must be a positive integer")
    _path, dir_fd = _open_verified_dir(cgroup_path, cgroup_inode)
    try:
        try:
            procs_fd = os.open(
                "cgroup.procs",
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=dir_fd,
            )
            try:
                os.write(procs_fd, f"{int(pid)}\n".encode("ascii"))
                os.lseek(procs_fd, 0, os.SEEK_SET)
                members = os.read(procs_fd, 1 << 20).decode("ascii", "strict")
            finally:
                os.close(procs_fd)
        except (OSError, UnicodeError) as exc:
            raise ContainmentError(f"cannot place worker in cgroup: {exc}") from exc
        if str(int(pid)) not in members.splitlines():
            raise ContainmentError("worker cgroup membership readback failed")
    finally:
        os.close(dir_fd)


def spawn_gated(
    command: Sequence[str],
    *,
    task_id: str,
    run_id: int,
    claim_lock: str,
    popen_kwargs: dict[str, Any] | None = None,
) -> WorkerSpawn:
    """Spawn a blocked helper, contain it, then return its unopened gate."""
    argv = [str(part) for part in command]
    if not argv or any("\x00" in part for part in argv):
        raise ContainmentError("worker command is empty or malformed")
    if not task_id or not claim_lock or "\x00" in task_id or "\x00" in claim_lock:
        raise ContainmentError("durable task and claim identity are required")
    cgroup_path, cgroup_inode = _create_worker_cgroup(run_id)
    read_fd = write_fd = helper_fd = -1
    process = None
    try:
        read_fd, write_fd = os.pipe()
        helper_path = Path(__file__).with_name("kanban_worker_gate.py")
        helper_fd = os.open(
            helper_path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        helper_stat = os.fstat(helper_fd)
        if not stat.S_ISREG(helper_stat.st_mode):
            raise ContainmentError("worker gate helper is not a regular file")
        kwargs = dict(popen_kwargs or {})
        if "pass_fds" in kwargs:
            raise ContainmentError("spawn gate owns the pass_fds contract")
        kwargs["pass_fds"] = (read_fd, helper_fd)
        helper_argv = [
            sys.executable,
            "-I",
            "-S",
            f"/proc/self/fd/{helper_fd}",
            str(read_fd),
            "--",
            *argv,
        ]
        process = subprocess.Popen(helper_argv, **kwargs)  # noqa: S603
        for fd in (read_fd, helper_fd):
            os.close(fd)
        read_fd = helper_fd = -1
        _move_pid_to_cgroup(str(cgroup_path), cgroup_inode, int(process.pid))
        return WorkerSpawn(
            process,
            write_fd,
            task_id,
            int(run_id),
            claim_lock,
            str(cgroup_path),
            cgroup_inode,
        )
    except Exception:
        for fd in (write_fd, read_fd, helper_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if process is not None:
            try:
                process.wait(timeout=0.5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        cleanup_cgroup(str(cgroup_path), cgroup_inode)
        raise


def kill_cgroup(
    cgroup_path: str,
    cgroup_inode: int,
    *,
    wait_seconds: float = 2.0,
) -> dict:
    """Kill every process in an exact cgroup and certify ``populated 0``."""
    info = {
        "backend": "cgroup_v2",
        "containment_certified": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    try:
        _path, fd = _open_verified_dir(cgroup_path, cgroup_inode)
        try:
            if not _read_populated_fd(fd):
                info["containment_certified"] = True
                info["terminated"] = True
                return info
            kill_fd = os.open(
                "cgroup.kill",
                os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=fd,
            )
            try:
                info["termination_attempted"] = True
                os.write(kill_fd, b"1\n")
                info["sigkill"] = True
            finally:
                os.close(kill_fd)
            deadline = time.monotonic() + max(0.0, float(wait_seconds))
            while True:
                if not _read_populated_fd(fd):
                    info["containment_certified"] = True
                    info["terminated"] = True
                    return info
                if time.monotonic() >= deadline:
                    info["uncertainty"] = "cgroup_remained_populated"
                    return info
                time.sleep(0.05)
        finally:
            os.close(fd)
    except (ContainmentError, OSError, ValueError) as exc:
        info["uncertainty"] = f"{type(exc).__name__}: {exc}"
        return info


def cleanup_cgroup(cgroup_path: str, cgroup_inode: int) -> bool:
    """Remove and certify one exact empty cgroup; preserve all uncertainty."""
    parent_fd = target_fd = -1
    try:
        path = _validated_path(cgroup_path)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        parent_fd = os.open(path.parent, flags)
        target_fd = os.open(path.name, flags, dir_fd=parent_fd)
        held = os.fstat(target_fd)
        if not stat.S_ISDIR(held.st_mode) or int(held.st_ino) != int(cgroup_inode):
            return False
        if _read_populated_fd(target_fd):
            return False

        # This is the final identity check before the name-based unlinkat syscall.
        held = os.fstat(target_fd)
        named = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(named.st_mode)
            or int(held.st_ino) != int(cgroup_inode)
            or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)
        ):
            return False
        os.rmdir(path.name, dir_fd=parent_fd)

        try:
            kernel_cgroup = (
                os.stat(_CGROUP_FS, follow_symlinks=False).st_dev == held.st_dev
            )
        except OSError:
            return False
        if not kernel_cgroup and os.fstat(target_fd).st_nlink != 0:
            return False
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except (ContainmentError, OSError):
        return False
    finally:
        for fd in (target_fd, parent_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def cgroup_absent(cgroup_path: str) -> bool:
    """Certify only child-name absence below an available authorized root."""
    parent_fd = -1
    try:
        path = _validated_path(cgroup_path)
        parent_fd = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except (ContainmentError, OSError):
        return False
    finally:
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def cgroup_populated(cgroup_path: str, cgroup_inode: int) -> bool:
    """Read population through an exact retained cgroup directory descriptor."""
    _path, fd = _open_verified_dir(cgroup_path, cgroup_inode)
    try:
        return _read_populated_fd(fd)
    finally:
        os.close(fd)


def enabled() -> bool:
    """Return whether the Linux Kanban containment policy is enabled."""
    if sys.platform != "linux":
        return False

    override = os.environ.get(_ENV_FLAG)
    if override is not None:
        return override.strip() == "1"

    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception as exc:
        raise ContainmentError(f"cannot load containment config: {exc}") from exc

    kanban = config.get("kanban") if isinstance(config, dict) else None
    return isinstance(kanban, dict) and kanban.get("cgroup_containment") is True
