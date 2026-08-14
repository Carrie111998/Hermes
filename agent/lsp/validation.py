"""Explicit, bounded full-project Pyright validation.

The write/patch LSP hook intentionally validates only the edited document.
This module is a separate opt-in path for a final repository-wide check:
it derives a temporary Pyright config from the repository config, adds explicit
include/exclude boundaries, and always tears down its subprocess and temporary
config.
"""
from __future__ import annotations

import copy
import json
import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

import psutil

DEFAULT_EXCLUDES = (".venv", "venv", "node_modules", "generated", "build", "cache")
DEFAULT_TIMEOUT = 300.0
DEFAULT_TERM_GRACE = 2.0
POST_TIMEOUT_DRAIN = 0.5
POST_TIMEOUT_WAIT = 0.2


@dataclass(frozen=True)
class ValidationResult:
    """Result of one bounded full-project validation process."""

    command: tuple[str, ...]
    returncode: int
    timed_out: bool
    stdout: str
    stderr: str


def _resolve_scoped_path(
    root: Path,
    value: str,
    *,
    base_dir: Optional[Path] = None,
    label: str = "path",
) -> Path:
    """Resolve a config path and require it to stay inside ``root``."""
    anchor = (base_dir or root).resolve()
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else anchor / raw
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"{label} outside workspace: {value}") from exc
    return resolved


def _as_config_relative(
    config_dir: Path,
    root: Path,
    values: Iterable[str],
    *,
    base_dir: Optional[Path] = None,
    label: str = "path",
) -> list[str]:
    result = []
    for value in values:
        resolved = _resolve_scoped_path(
            root,
            value,
            base_dir=base_dir,
            label=label,
        )
        result.append(os.path.relpath(resolved, config_dir))
    return result


def _merge_excludes(exclude: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Keep mandatory dependency/generated-tree exclusions in every overlay."""
    values: list[str] = list(DEFAULT_EXCLUDES)
    if exclude:
        values.extend(exclude)
    return tuple(dict.fromkeys(values))


def _repository_config(root: Path, project_config: Optional[str]) -> Optional[Path]:
    root = root.resolve()
    if project_config:
        path = Path(project_config).expanduser()
        if not path.is_absolute():
            path = root / path
        path = _resolve_scoped_path(root, str(path), label="project config")
        if not path.is_file():
            raise FileNotFoundError(f"Pyright project config not found: {path}")
        return path
    for name in ("pyrightconfig.json", "pyproject.toml"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _rebase_config_path(
    value: str,
    *,
    source_dir: Path,
    root: Path,
    target_dir: Path,
    label: str,
) -> str:
    return _as_config_relative(
        target_dir,
        root,
        [value],
        base_dir=source_dir,
        label=label,
    )[0]


def _rebase_pyright_options(
    options: dict,
    *,
    source_dir: Path,
    root: Path,
    target_dir: Path,
) -> dict:
    """Keep path-valued TOML options anchored to their source config."""
    options = copy.deepcopy(options)
    for key in ("extraPaths", "include", "exclude"):
        values = options.get(key)
        if isinstance(values, list):
            options[key] = [
                _rebase_config_path(
                    value,
                    source_dir=source_dir,
                    root=root,
                    target_dir=target_dir,
                    label=f"pyright {key}",
                )
                for value in values
                if isinstance(value, str)
            ]
    for key in ("stubPath", "typeshedPath", "venvPath"):
        value = options.get(key)
        if isinstance(value, str):
            options[key] = _rebase_config_path(
                value,
                source_dir=source_dir,
                root=root,
                target_dir=target_dir,
                label=f"pyright {key}",
            )
    environments = options.get("executionEnvironments")
    if isinstance(environments, list):
        for environment in environments:
            if not isinstance(environment, dict):
                continue
            if isinstance(environment.get("root"), str):
                environment["root"] = _rebase_config_path(
                    environment["root"],
                    source_dir=source_dir,
                    root=root,
                    target_dir=target_dir,
                    label="pyright execution environment root",
                )
            values = environment.get("extraPaths")
            if isinstance(values, list):
                environment["extraPaths"] = [
                    _rebase_config_path(
                        value,
                        source_dir=source_dir,
                        root=root,
                        target_dir=target_dir,
                        label="pyright execution environment extraPaths",
                    )
                    for value in values
                    if isinstance(value, str)
                ]
    if isinstance(options.get("extends"), str):
        options["extends"] = _rebase_config_path(
            options["extends"],
            source_dir=source_dir,
            root=root,
            target_dir=target_dir,
            label="pyright extends",
        )
    return options


def _config_options(
    config: Optional[Path],
    *,
    root: Optional[Path] = None,
    target_dir: Optional[Path] = None,
) -> dict:
    if config is None:
        return {}
    config = config.resolve()
    root = (root or config.parent).resolve()
    target_dir = (target_dir or config.parent).resolve()
    if config.suffix.lower() == ".json":
        payload = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Pyright config must contain an object: {config}")
        # Keep path resolution and inherited options anchored to the repository
        # config instead of copying a relative `extends` into /tmp.  Validate
        # every path-valued option before inheriting the original JSON file.
        _rebase_pyright_options(
            dict(payload),
            source_dir=config.parent,
            root=root,
            target_dir=config.parent,
        )
        return {"extends": str(config)}
    if config.name == "pyproject.toml":
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
        options = payload.get("tool", {}).get("pyright", {})
        if not isinstance(options, dict):
            raise ValueError(f"[tool.pyright] must be a table: {config}")
        return _rebase_pyright_options(
            dict(options),
            source_dir=config.parent,
            root=root,
            target_dir=target_dir,
        )
    raise ValueError(f"Unsupported Pyright config format: {config}")


@contextmanager
def _temporary_project_config(
    root: Path,
    config: Optional[Path],
    include: Sequence[str],
    exclude: Sequence[str],
):
    """Create an explicit overlay config and remove it on every exit path."""
    root = root.resolve()
    config = config.resolve() if config is not None else None
    with tempfile.TemporaryDirectory(prefix=".hermes-pyright-", dir=root) as temp_dir:
        path = Path(temp_dir) / "pyrightconfig.json"
        config_dir = Path(temp_dir).resolve()
        options = _config_options(config, root=root, target_dir=config_dir)
        options["include"] = _as_config_relative(
            config_dir,
            root,
            include or (".",),
            label="include",
        )
        options["exclude"] = _as_config_relative(
            config_dir,
            root,
            _merge_excludes(exclude),
            label="exclude",
        )
        path.write_text(json.dumps(options, indent=2) + "\n", encoding="utf-8")
        yield path


def _signal_group(pgid: Optional[int], sig: signal.Signals) -> bool:
    if pgid is None or os.name == "nt":
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, OSError):
        return False


def _group_alive(pgid: Optional[int]) -> bool:
    if pgid is None or os.name == "nt":
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def _process_identity(pid: int) -> Optional[psutil.Process]:
    try:
        return psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _snapshot_descendants(
    pid: int,
    owner: Optional[psutil.Process] = None,
) -> list[psutil.Process]:
    owner = owner or _process_identity(pid)
    if owner is None:
        return []
    try:
        return owner.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def _refresh_descendants(
    owner: Optional[psutil.Process],
    known: Sequence[psutil.Process],
) -> list[psutil.Process]:
    """Re-collect the owned tree, including descendants of known children."""
    seeds = [owner, *known] if owner is not None else list(known)
    refreshed: list[psutil.Process] = []
    seen: set[int] = set()
    for seed in seeds:
        try:
            children = seed.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        for child in children:
            child_pid = getattr(child, "pid", id(child))
            if child_pid in seen:
                continue
            seen.add(child_pid)
            refreshed.append(child)
    for child in known:
        child_pid = getattr(child, "pid", id(child))
        if child_pid not in seen:
            seen.add(child_pid)
            refreshed.append(child)
    return refreshed


def _any_process_alive(processes: Sequence[psutil.Process]) -> bool:
    for process in processes:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, AttributeError):
            continue
    return False


def _signal_descendants(descendants: Sequence[psutil.Process], *, force: bool) -> None:
    for child in descendants:
        try:
            if not child.is_running():
                continue
            (child.kill if force else child.terminate)()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass


def _cleanup_process(
    process: subprocess.Popen,
    pgid: Optional[int],
    descendants: Sequence[psutil.Process],
    term_grace: float,
    owner: Optional[psutil.Process] = None,
) -> None:
    """TERM the owned tree/group, then KILL after a bounded grace period."""
    owner = owner or _process_identity(process.pid)
    current = _refresh_descendants(owner, descendants)
    # Detached descendants are outside the process group, so signal the exact
    # process-tree snapshot before terminating the owned group.  Re-collect
    # during the grace window to catch a child that creates a grandchild after
    # the first snapshot.
    _signal_descendants(current, force=False)
    _signal_group(pgid, signal.SIGTERM)
    if pgid is None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass

    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        current = _refresh_descendants(owner, current)
        _signal_descendants(current, force=False)
        if process.poll() is not None and not _group_alive(pgid) and not _any_process_alive(current):
            break
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    # Always take one latest snapshot immediately before KILL.  A detached
    # child may have created a new session and a grandchild during TERM/grace.
    current = _refresh_descendants(owner, current)
    _signal_group(pgid, signal.SIGKILL)
    _signal_descendants(current, force=True)
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=max(1.0, term_grace))
    except (subprocess.TimeoutExpired, ChildProcessError, OSError):
        # The final group/process signals above are best effort; communicate
        # below still drains pipes and reaps the root where possible.
        pass


def _decode_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _close_output_pipes(process: subprocess.Popen) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            pass


def _drain_after_timeout(
    process: subprocess.Popen,
    stdout: object,
    stderr: object,
) -> tuple[str, str]:
    """Drain briefly, then close inherited pipes instead of waiting forever."""
    fallback_stdout = _decode_output(stdout)
    fallback_stderr = _decode_output(stderr)
    try:
        drained_stdout, drained_stderr = process.communicate(timeout=POST_TIMEOUT_DRAIN)
        return (
            _decode_output(drained_stdout) or fallback_stdout,
            _decode_output(drained_stderr) or fallback_stderr,
        )
    except subprocess.TimeoutExpired as exc:
        drained_stdout = _decode_output(exc.output) or fallback_stdout
        drained_stderr = _decode_output(exc.stderr) or fallback_stderr
        _close_output_pipes(process)
        try:
            process.wait(timeout=POST_TIMEOUT_WAIT)
        except (subprocess.TimeoutExpired, ChildProcessError, OSError):
            pass
        return drained_stdout, drained_stderr


def _resolve_executable(executable: Optional[str]) -> str:
    if executable:
        return executable
    resolved = shutil.which("pyright")
    if resolved:
        return resolved
    raise FileNotFoundError("pyright CLI is not installed or not on PATH")


def run_full_project_check(
    workspace_root: str,
    *,
    project_config: Optional[str] = None,
    executable: Optional[str] = None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = DEFAULT_EXCLUDES,
    timeout: float = DEFAULT_TIMEOUT,
    term_grace: float = DEFAULT_TERM_GRACE,
    env: Optional[dict[str, str]] = None,
) -> ValidationResult:
    """Run a bounded repository-wide Pyright check.

    This function is never used by the post-write document loop.  It is an
    explicit final-validation operation and therefore scans the repository
    root (or the supplied include paths) using a temporary config overlay.
    The repository's ``pyrightconfig.json`` or ``[tool.pyright]`` settings are
    retained, while the include/exclude boundary is made explicit.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if not math.isfinite(term_grace) or term_grace < 0:
        raise ValueError("term_grace must be a finite non-negative number")
    root = Path(workspace_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    config = _repository_config(root, project_config)
    command: tuple[str, ...]
    with _temporary_project_config(root, config, include, exclude) as overlay:
        command = (_resolve_executable(executable), "--project", str(overlay))
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        process = subprocess.Popen(
            list(command),
            cwd=str(root),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0
            ),
        )
        pgid = None
        if os.name != "nt":
            try:
                candidate = os.getpgid(process.pid)
                if candidate == process.pid and candidate != os.getpgrp():
                    pgid = candidate
            except (ProcessLookupError, OSError):
                pass
        owner = _process_identity(process.pid)
        descendants = _snapshot_descendants(process.pid, owner)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            _cleanup_process(process, pgid, descendants, term_grace, owner)
            stdout, stderr = _drain_after_timeout(process, exc.output, exc.stderr)
        finally:
            if process.poll() is None or _group_alive(pgid):
                _cleanup_process(process, pgid, descendants, term_grace, owner)
        return ValidationResult(
            command=command,
            returncode=process.returncode if process.returncode is not None else 124,
            timed_out=timed_out,
            stdout=stdout or "",
            stderr=stderr or "",
        )


__all__ = [
    "DEFAULT_EXCLUDES",
    "DEFAULT_TERM_GRACE",
    "DEFAULT_TIMEOUT",
    "ValidationResult",
    "run_full_project_check",
]
