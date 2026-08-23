"""Windows rollout coordinator isolation and lock-safe re-exec.

Windows cannot replace an in-use virtual environment.  A canary rollout (or
an explicit rollback) therefore cannot run from the project's live
``venv\\Scripts\\python.exe``: that process maps both the launcher and native
extensions which the transaction must be able to swap.

This module copies the dependency tree to a private sibling directory while
the install-wide update lock is held, verifies the copy byte-for-byte, and
starts the same update command from the copied interpreter.  The child takes
the existing marker over atomically, acknowledges its exact PID, and waits on
a Win32 process handle until the live-venv parent has exited.  No Git or venv
mutation is allowed before that wait completes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional


COORDINATOR_SNAPSHOT_ENV = "HERMES_UPDATE_COORDINATOR_SNAPSHOT"
COORDINATOR_PARENT_PID_ENV = "HERMES_UPDATE_COORDINATOR_PARENT_PID"
COORDINATOR_TOKEN_ENV = "HERMES_UPDATE_COORDINATOR_TOKEN"
TAURI_READY_ENV = "HERMES_UPDATE_TAURI_READY_PATH"
UPDATE_CORRELATION_ENV = "HERMES_UPDATE_CORRELATION_ID"
WINDOWS_DETACHED_ENV = "HERMES_UPDATE_WINDOWS_DETACHED"

_READY_FILE_NAME = "ready.json"
_OWNER_FILE_NAME = "owner.json"
_SNAPSHOT_NAME_RE = re.compile(r"^[0-9a-f]{32}$")
_VERIFY_SENTINEL = "hermes-external-rollout-coordinator-ok"
_HANDSHAKE_TIMEOUT_SECONDS = 60.0
_STALE_AFTER_SECONDS = 24 * 60 * 60
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class CoordinatorHandoffError(RuntimeError):
    """The external coordinator could not be proven safe and ready."""


def is_windows_coordinator_child() -> bool:
    """Whether this command claims to be an external coordinator child."""

    return bool(os.environ.get(COORDINATOR_SNAPSHOT_ENV, "").strip())


def _coordinator_base(project_root: Path) -> Path:
    project = Path(project_root).resolve(strict=False)
    return project.parent / f".{project.name}-update-coordinators"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CoordinatorHandoffError(
            f"cannot inspect coordinator path {path}: {exc}"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _ensure_real_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise CoordinatorHandoffError(
                f"cannot create coordinator directory {path}: {exc}"
            ) from exc
    if _is_link_or_reparse(path) or not path.is_dir():
        raise CoordinatorHandoffError(
            f"coordinator path must be a real directory: {path}"
        )
    return path.resolve(strict=True)


def _snapshot_root_from_env(project_root: Path) -> Path:
    raw = os.environ.get(COORDINATOR_SNAPSHOT_ENV, "").strip()
    if not raw:
        raise CoordinatorHandoffError("external coordinator snapshot is not set")
    snapshot = Path(raw).expanduser().resolve(strict=False)
    base = _coordinator_base(project_root).resolve(strict=False)
    if snapshot.parent != base or not _SNAPSHOT_NAME_RE.fullmatch(snapshot.name):
        raise CoordinatorHandoffError(
            f"external coordinator snapshot is outside the install scope: {snapshot}"
        )
    return _ensure_real_directory(snapshot)


def _positive_pid(value: str, label: str) -> int:
    try:
        pid = int(value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise CoordinatorHandoffError(f"invalid {label} pid") from exc
    if pid <= 0:
        raise CoordinatorHandoffError(f"invalid {label} pid")
    return pid


def _venv_python_for_platform(venv: Path, *, windows: bool) -> Path:
    from hermes_constants import venv_bin_dir

    return venv_bin_dir(venv, windows=windows) / ("python.exe" if windows else "python")


def _venv_python(venv: Path) -> Path:
    return _venv_python_for_platform(venv, windows=sys.platform == "win32")


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _process_identity(pid: int) -> dict[str, Any]:
    identity: dict[str, Any] = {"pid": int(pid)}
    try:
        import psutil

        identity["create_time"] = float(psutil.Process(pid).create_time())
    except Exception:
        # Creation time is only an extra stale-cleanup guard.  Failure to read
        # it must never weaken the live update lock or the handoff protocol.
        identity["create_time"] = None
    return identity


def _write_owner(snapshot: Path, pid: int) -> None:
    _atomic_write_json(snapshot / _OWNER_FILE_NAME, _process_identity(pid))


def _owner_is_provably_dead(owner_path: Path) -> bool:
    try:
        payload = json.loads(owner_path.read_text(encoding="utf-8"))
        pid = _positive_pid(str(payload.get("pid", "")), "snapshot owner")
        expected_created = payload.get("create_time")
    except (CoordinatorHandoffError, OSError, ValueError, AttributeError):
        return False
    try:
        import psutil
    except Exception:
        return False
    try:
        process = psutil.Process(pid)
        if not process.is_running():
            return True
        if expected_created is None:
            return False
        return abs(float(process.create_time()) - float(expected_created)) >= 2.0
    except psutil.NoSuchProcess:
        return True
    except Exception:
        return False


def _safe_remove_snapshot(snapshot: Path, project_root: Path) -> bool:
    """Remove only an exact, real coordinator root for this project."""

    try:
        resolved = snapshot.resolve(strict=False)
        base = _coordinator_base(project_root).resolve(strict=False)
        if resolved.parent != base or not _SNAPSHOT_NAME_RE.fullmatch(resolved.name):
            return False
        if not resolved.exists():
            return True
        if _is_link_or_reparse(resolved) or not resolved.is_dir():
            return False
        shutil.rmtree(resolved)
        return True
    except OSError:
        return False


def _prune_stale_snapshots(project_root: Path, base: Path) -> None:
    """Best-effort cleanup of old snapshots whose exact owner is dead."""

    try:
        children = list(base.iterdir())
    except OSError:
        return
    now = time.time()
    for child in children:
        if not _SNAPSHOT_NAME_RE.fullmatch(child.name):
            continue
        try:
            if _is_link_or_reparse(child) or not child.is_dir():
                continue
            age = now - child.stat().st_mtime
        except (CoordinatorHandoffError, OSError):
            continue
        if age < _STALE_AFTER_SECONDS:
            continue
        if _owner_is_provably_dead(child / _OWNER_FILE_NAME):
            _safe_remove_snapshot(child, project_root)


def _create_verified_snapshot(project_root: Path) -> tuple[Path, Path]:
    """Copy the real project venv and prove before/copy/after equality."""

    from hermes_cli.update_rollout import (
        CheckpointError,
        _dependency_state,
        _dependency_states_match,
        _find_venv,
    )

    project = Path(project_root).resolve(strict=True)
    try:
        live_venv, _venv_name, present = _find_venv(project)
    except CheckpointError as exc:
        raise CoordinatorHandoffError(str(exc)) from exc
    if not present:
        raise CoordinatorHandoffError(
            "Windows rollout requires a real project venv to create an "
            "external coordinator"
        )
    live_venv = live_venv.resolve(strict=True)

    base = _coordinator_base(project)
    existed = base.exists()
    base = _ensure_real_directory(base, create=True)
    if not existed:
        try:
            os.chmod(base, 0o700)
        except OSError:
            pass
    _prune_stale_snapshots(project, base)

    snapshot = base / uuid.uuid4().hex
    try:
        snapshot.mkdir(mode=0o700)
        snapshot = _ensure_real_directory(snapshot)
        _write_owner(snapshot, os.getpid())
        destination = snapshot / "venv"

        state_before = _dependency_state(live_venv)
        shutil.copytree(live_venv, destination, symlinks=True)
        copied_state = _dependency_state(destination)
        state_after = _dependency_state(live_venv)
        if not (
            _dependency_states_match(state_before, copied_state)
            and _dependency_states_match(state_before, state_after)
        ):
            raise CoordinatorHandoffError(
                "project venv changed while the external coordinator was copied"
            )
        return snapshot, destination
    except BaseException:
        _safe_remove_snapshot(snapshot, project)
        raise


def _coordinator_env(
    *,
    snapshot: Path,
    copied_venv: Path,
    parent_pid: int,
    owner_pid: int,
    token: str,
) -> dict[str, str]:
    from hermes_cli.update_lock import (
        COORDINATOR_TAKEOVER_PID_ENV,
        HANDOFF_PID_ENV,
    )

    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    # A gateway-origin coordinator is already the independently-launched
    # worker. Bind that proof to this exact request, rather than inheriting a
    # stale slash-command value or setting a boolean. The child still has to
    # prove at runtime that it is outside every Windows job object before the
    # rollout path accepts this value.
    correlation_id = env.get(UPDATE_CORRELATION_ENV, "").strip()
    if correlation_id:
        env[WINDOWS_DETACHED_ENV] = correlation_id
    else:
        env.pop(WINDOWS_DETACHED_ENV, None)
    env.update({
        COORDINATOR_SNAPSHOT_ENV: str(snapshot),
        COORDINATOR_PARENT_PID_ENV: str(parent_pid),
        COORDINATOR_TOKEN_ENV: token,
        COORDINATOR_TAKEOVER_PID_ENV: str(owner_pid),
        HANDOFF_PID_ENV: str(owner_pid),
        "VIRTUAL_ENV": str(copied_venv),
        "PYTHONUNBUFFERED": "1",
    })
    return env


def _verify_copied_interpreter(
    interpreter: Path,
    *,
    project_root: Path,
    env: dict[str, str],
) -> None:
    if not interpreter.is_file():
        raise CoordinatorHandoffError(
            f"copied coordinator interpreter is missing: {interpreter}"
        )
    code = (
        "import sys; from pathlib import Path; import hermes_cli.main; "
        "from hermes_cli.update_rollout import validate_rollout_coordinator; "
        "validate_rollout_coordinator(Path(sys.argv[1])); "
        f"print({_VERIFY_SENTINEL!r})"
    )
    try:
        completed = subprocess.run(
            [str(interpreter), "-I", "-B", "-c", code, str(project_root)],
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoordinatorHandoffError(
            f"copied coordinator interpreter could not run: {exc}"
        ) from exc
    if completed.returncode != 0 or _VERIFY_SENTINEL not in completed.stdout:
        detail = (completed.stderr or completed.stdout or "no output").strip()
        raise CoordinatorHandoffError(
            f"copied coordinator failed import/isolation verification: {detail[:500]}"
        )


def _authorized_marker_owner(update_lock: Any) -> int:
    """Return the marker owner this successfully-acquired command may transfer."""

    from hermes_cli.update_lock import (
        _handoff_pid,
        _is_ancestor_pid,
        read_live_update,
    )

    holder = read_live_update(path=update_lock.path)
    if holder is None:
        raise CoordinatorHandoffError(
            "update marker disappeared before coordinator handoff"
        )
    holder_pid = holder.pid
    if holder_pid is None:
        detail = getattr(holder, "unavailable_reason", None) or "unresolved owner"
        raise CoordinatorHandoffError(
            f"update marker owner is unavailable before coordinator handoff: {detail}"
        )
    if bool(getattr(update_lock, "acquired", False)):
        if holder_pid != os.getpid():
            raise CoordinatorHandoffError(
                "update marker ownership changed before coordinator handoff"
            )
        return holder_pid
    if holder_pid == _handoff_pid() or _is_ancestor_pid(holder_pid):
        return holder_pid
    raise CoordinatorHandoffError(
        "cannot prove the coordinator handoff owns the live update marker"
    )


def _read_ready(path: Path) -> Optional[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _publish_tauri_coordinator_ready(child_pid: int) -> None:
    """Publish the exact handoff PID to a UUID-bound Tauri path, if requested."""

    ready_raw = os.environ.get(TAURI_READY_ENV, "").strip()
    if not ready_raw:
        return
    correlation_id = os.environ.get(
        "HERMES_UPDATE_CORRELATION_ID", ""
    ).strip().lower()
    try:
        parsed = str(uuid.UUID(correlation_id))
    except ValueError as exc:
        raise CoordinatorHandoffError(
            "Tauri coordinator correlation is invalid"
        ) from exc
    if parsed != correlation_id:
        raise CoordinatorHandoffError("Tauri coordinator correlation is invalid")
    from hermes_constants import get_hermes_home

    home = get_hermes_home().resolve(strict=False)
    expected = home / f".update_coordinator_ready.{correlation_id}"
    supplied = Path(ready_raw).expanduser().resolve(strict=False)
    if supplied != expected:
        raise CoordinatorHandoffError(
            "Tauri coordinator readiness path is outside the Hermes home"
        )
    _atomic_write_json(
        expected,
        {
            "correlation_id": correlation_id,
            "pid": int(child_pid),
        },
    )


def _wait_for_coordinator_ready(
    child: subprocess.Popen,
    *,
    ready_path: Path,
    token: str,
    marker_path: Path,
    timeout_seconds: float = _HANDSHAKE_TIMEOUT_SECONDS,
) -> None:
    from hermes_cli.update_lock import read_live_update

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        return_code = child.poll()
        if return_code is not None:
            raise CoordinatorHandoffError(
                f"external coordinator exited before takeover (code {return_code})"
            )
        payload = _read_ready(ready_path)
        if payload is not None:
            try:
                ready_pid = int(payload.get("pid", 0))
            except (TypeError, ValueError):
                ready_pid = 0
            holder = read_live_update(path=marker_path)
            if (
                ready_pid == child.pid
                and payload.get("token") == token
                and holder is not None
                and holder.pid == child.pid
            ):
                return
        time.sleep(0.05)
    raise CoordinatorHandoffError(
        "timed out waiting for the external coordinator to take the update lock"
    )


def _stop_failed_child(child: subprocess.Popen) -> bool:
    try:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        return child.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


def _spawn_coordinator_process(
    child_argv: list[str],
    *,
    project: Path,
    env: dict[str, str],
) -> subprocess.Popen:
    """Spawn the copied coordinator with Tauri-safe durable output.

    The Tauri runner captures the live-venv parent's stdout/stderr in anonymous
    pipes and intentionally abandons descendant-held writers after a bounded
    drain.  A copied coordinator outlives that parent, so inheriting those
    writers would turn its next normal ``print`` into ``BrokenPipeError`` once
    Rust closes the read ends.  When the UUID-bound Tauri readiness channel is
    present, detach both output streams onto the standard update log instead.

    Non-Tauri terminal and bot launchers keep their existing inherited output
    contract (including bot-mode progress capture).
    """

    popen_kwargs: dict[str, Any] = {
        "cwd": str(project),
        "env": env,
        "close_fds": True,
    }
    popen_kwargs.update(_coordinator_detach_popen_kwargs(sys.platform))
    if not os.environ.get(TAURI_READY_ENV, "").strip():
        return subprocess.Popen(child_argv, **popen_kwargs)

    from hermes_constants import get_hermes_home

    log_path = get_hermes_home().resolve(strict=False) / "logs" / "update.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Binary, unbuffered append keeps the parent from owning a text buffer.
        # Popen duplicates the explicit handle for the child before this scope
        # closes it, on both POSIX and Windows.
        log_handle = log_path.open("ab", buffering=0)
    except OSError as exc:
        raise CoordinatorHandoffError(
            f"cannot open durable coordinator output {log_path}: {exc}"
        ) from exc
    try:
        return subprocess.Popen(
            child_argv,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    finally:
        log_handle.close()


def _coordinator_detach_popen_kwargs(
    platform: str,
    *,
    windows_flags: Optional[int] = None,
) -> dict[str, Any]:
    """Return the copied coordinator's mandatory process-lifetime policy.

    The child is useful only if it survives the live-venv parent and its
    Electron/Tauri job. A Windows spawn without ``CREATE_BREAKAWAY_FROM_JOB``
    would acknowledge takeover and then be killed with the parent, leaving no
    process able to publish the UUID-bound terminal outcome. Refuse before
    spawning if the platform helper ever loses that bit; callers deliberately
    do not retry without breakaway.

    ``windows_flags`` is injectable so the policy is exercised on Linux CI
    without pretending that the host OS is Windows.
    """

    if platform != "win32":
        return {}
    if windows_flags is None:
        from hermes_cli._subprocess_compat import windows_detach_flags

        windows_flags = windows_detach_flags()
    if not windows_flags & _CREATE_BREAKAWAY_FROM_JOB:
        raise CoordinatorHandoffError(
            "external coordinator launch cannot prove Windows job breakaway"
        )
    return {"creationflags": windows_flags}


def _coordinator_policy_applies(
    *,
    platform: str,
    coordinator_child: bool,
    explicit_rollback: bool,
    rollout_enabled: bool,
) -> bool:
    """Pure host policy for operations that may replace a live Windows venv."""

    return bool(
        platform == "win32"
        and not coordinator_child
        and (explicit_rollback or rollout_enabled)
    )


def _rollout_needs_external_coordinator(args: Any, project_root: Path) -> bool:
    """True only when an applicable operation fails live-runtime validation."""

    explicit_rollback = getattr(args, "rollback", None) is not None
    coordinator_child = is_windows_coordinator_child()
    if sys.platform != "win32" or coordinator_child:
        return False
    rollout_enabled = False
    if not explicit_rollback:
        from hermes_cli.update_rollout import load_rollout_config

        rollout_enabled = bool(load_rollout_config().enabled)
    if not _coordinator_policy_applies(
        platform=sys.platform,
        coordinator_child=coordinator_child,
        explicit_rollback=explicit_rollback,
        rollout_enabled=rollout_enabled,
    ):
        return False
    from hermes_cli.update_rollout import RolloutError, validate_rollout_coordinator

    try:
        validate_rollout_coordinator(Path(project_root))
    except RolloutError:
        return True
    return False


def handoff_windows_rollout_coordinator(
    args: Any,
    *,
    update_lock: Any,
    gateway_mode: bool,
    project_root: Path,
    argv: Optional[list[str]] = None,
) -> Optional[int]:
    """Start and prove an external coordinator; return the parent exit code.

    The caller must already hold (or be admitted non-owningly under) the
    install-global update lock.  ``None`` means no handoff is needed.  A
    successful terminal parent exits 0; gateway parents use the existing
    nonterminal handoff code 75 so the independent child owns the final status.
    """

    project = Path(project_root).resolve(strict=True)
    if not _rollout_needs_external_coordinator(args, project):
        return None

    snapshot: Optional[Path] = None
    child: Optional[subprocess.Popen] = None
    try:
        owner_pid = _authorized_marker_owner(update_lock)
        snapshot, copied_venv = _create_verified_snapshot(project)
        token = uuid.uuid4().hex
        env = _coordinator_env(
            snapshot=snapshot,
            copied_venv=copied_venv,
            parent_pid=os.getpid(),
            owner_pid=owner_pid,
            token=token,
        )
        interpreter = _venv_python(copied_venv)
        _verify_copied_interpreter(
            interpreter,
            project_root=project,
            env=env,
        )

        # Copying and verification can be slow.  Prove that the same live
        # marker is still ours immediately before creating the child.
        if _authorized_marker_owner(update_lock) != owner_pid:
            raise CoordinatorHandoffError(
                "update marker owner changed while preparing the coordinator"
            )

        original_argv = list(sys.argv[1:] if argv is None else argv)
        child_argv = [
            str(interpreter),
            "-I",
            "-m",
            "hermes_cli.main",
            *original_argv,
        ]
        child = _spawn_coordinator_process(
            child_argv,
            project=project,
            env=env,
        )
        _wait_for_coordinator_ready(
            child,
            ready_path=snapshot / _READY_FILE_NAME,
            token=token,
            marker_path=update_lock.path,
        )
        _publish_tauri_coordinator_ready(child.pid)
        return 75 if gateway_mode else 0
    except BaseException:
        child_stopped = child is None or _stop_failed_child(child)
        if snapshot is not None and child_stopped:
            _safe_remove_snapshot(snapshot, project)
        raise


def acquire_windows_coordinator_takeover(
    update_lock: Any,
    *,
    project_root: Path,
) -> Optional[bool]:
    """Take the parent marker, acknowledge, and wait for its process exit.

    ``None`` means this is not a coordinator child.  ``False`` is an atomic
    takeover refusal.  ``True`` is returned only after the old live-venv
    process is gone, making Git/venv mutation safe on Windows.
    """

    if not is_windows_coordinator_child():
        return None
    if sys.platform != "win32":
        raise CoordinatorHandoffError(
            "Windows coordinator handoff metadata is invalid on this platform"
        )
    project = Path(project_root).resolve(strict=True)
    snapshot = _snapshot_root_from_env(project)
    copied_venv = (snapshot / "venv").resolve(strict=True)
    if Path(sys.prefix).resolve(strict=False) != copied_venv:
        raise CoordinatorHandoffError(
            "external coordinator is not running from its copied venv"
        )
    if not _under(Path(sys.executable), copied_venv):
        raise CoordinatorHandoffError(
            "external coordinator executable is outside its copied venv"
        )

    token = os.environ.get(COORDINATOR_TOKEN_ENV, "").strip()
    if not _SNAPSHOT_NAME_RE.fullmatch(token):
        raise CoordinatorHandoffError("external coordinator token is invalid")
    parent_pid = _positive_pid(
        os.environ.get(COORDINATOR_PARENT_PID_ENV, ""),
        "coordinator parent",
    )
    if parent_pid == os.getpid():
        raise CoordinatorHandoffError("external coordinator parent pid is self")

    from hermes_cli.update_rollout import validate_rollout_coordinator

    validate_rollout_coordinator(project)
    parent_handle = _open_parent_wait_handle(parent_pid)
    try:
        if not update_lock.take_over_handoff():
            return False
        _write_owner(snapshot, os.getpid())
        _atomic_write_json(
            snapshot / _READY_FILE_NAME,
            {
                "pid": os.getpid(),
                "parent_pid": parent_pid,
                "token": token,
            },
        )
        _wait_parent_handle(parent_handle)
        return True
    finally:
        _close_process_handle(parent_handle)


def _open_parent_wait_handle(pid: int) -> int:
    if os.name != "nt":
        raise CoordinatorHandoffError(
            "Win32 parent-process wait is unavailable on this platform"
        )
    try:
        import ctypes
        from ctypes import wintypes

        win_dll = getattr(ctypes, "WinDLL")
        last_error = getattr(ctypes, "get_last_error", lambda: 0)
        kernel32 = win_dll("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        synchronize = 0x00100000
        handle = open_process(synchronize, False, pid)
        if not handle:
            raise OSError(last_error(), "OpenProcess failed")
        return int(handle)
    except Exception as exc:
        raise CoordinatorHandoffError(
            f"cannot open the coordinator parent process {pid}: {exc}"
        ) from exc


def _wait_parent_handle(handle: int) -> None:
    try:
        import ctypes
        from ctypes import wintypes

        win_dll = getattr(ctypes, "WinDLL")
        last_error = getattr(ctypes, "get_last_error", lambda: 0)
        kernel32 = win_dll("kernel32", use_last_error=True)
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        wait_object_0 = 0x00000000
        infinite = 0xFFFFFFFF
        result = wait(handle, infinite)
        if result != wait_object_0:
            raise OSError(
                last_error(),
                f"WaitForSingleObject returned {result:#x}",
            )
    except Exception as exc:
        raise CoordinatorHandoffError(
            f"could not wait for the live-venv parent to exit: {exc}"
        ) from exc


def _close_process_handle(handle: int) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        win_dll = getattr(ctypes, "WinDLL")
        kernel32 = win_dll("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)
    except Exception:
        pass


_CLEANUP_CODE = r"""
import ctypes
import shutil
import sys
from ctypes import wintypes

path = sys.argv[1]
pid = int(sys.argv[2])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
open_process = kernel32.OpenProcess
open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
open_process.restype = wintypes.HANDLE
wait = kernel32.WaitForSingleObject
wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
wait.restype = wintypes.DWORD
close_handle = kernel32.CloseHandle
close_handle.argtypes = [wintypes.HANDLE]
close_handle.restype = wintypes.BOOL
handle = open_process(0x00100000, False, pid)
if not handle:
    raise SystemExit(1)
try:
    if wait(handle, 0xFFFFFFFF) != 0:
        raise SystemExit(1)
finally:
    close_handle(handle)
shutil.rmtree(path, ignore_errors=True)
""".strip()


def schedule_windows_coordinator_cleanup(project_root: Path) -> bool:
    """Launch an external base-Python helper that deletes us after exit."""

    if sys.platform != "win32" or not is_windows_coordinator_child():
        return False
    try:
        project = Path(project_root).resolve(strict=True)
        snapshot = _snapshot_root_from_env(project)
        base_python = Path(getattr(sys, "_base_executable", "")).resolve(strict=True)
        forbidden = [
            snapshot,
            project / "venv",
            project / ".venv",
        ]
        if any(_under(base_python, root) for root in forbidden):
            return False
        flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        env = dict(os.environ)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        for name in (
            COORDINATOR_SNAPSHOT_ENV,
            COORDINATOR_PARENT_PID_ENV,
            COORDINATOR_TOKEN_ENV,
            TAURI_READY_ENV,
            "HERMES_UPDATE_TAURI_OUTCOME_PATH",
            "HERMES_UPDATE_CORRELATION_ID",
            "HERMES_UPDATE_COORDINATOR_TAKEOVER_PID",
            "HERMES_UPDATE_HANDOFF_PID",
        ):
            env.pop(name, None)
        subprocess.Popen(
            [
                str(base_python),
                "-I",
                "-c",
                _CLEANUP_CODE,
                str(snapshot),
                str(os.getpid()),
            ],
            cwd=str(project.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
        return True
    except (CoordinatorHandoffError, OSError, ValueError):
        return False
