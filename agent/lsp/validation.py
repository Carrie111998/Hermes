"""Explicit, bounded full-project Pyright validation.

The write/patch LSP hook intentionally validates only the edited document.
This module is a separate opt-in path for a final repository-wide check:
it derives a temporary Pyright config from the repository config, adds explicit
include/exclude boundaries, and always tears down its subprocess and temporary
config.
"""
from __future__ import annotations

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


@dataclass(frozen=True)
class ValidationResult:
    """Result of one bounded full-project validation process."""

    command: tuple[str, ...]
    returncode: int
    timed_out: bool
    stdout: str
    stderr: str


def _as_config_relative(config_dir: Path, root: Path, values: Iterable[str]) -> list[str]:
    result = []
    for value in values:
        path = Path(value)
        absolute = path if path.is_absolute() else root / path
        result.append(os.path.relpath(absolute, config_dir))
    return result


def _repository_config(root: Path, project_config: Optional[str]) -> Optional[Path]:
    if project_config:
        path = Path(project_config).expanduser()
        if not path.is_absolute():
            path = root / path
        path = path.absolute()
        if not path.is_file():
            raise FileNotFoundError(f"Pyright project config not found: {path}")
        return path
    for name in ("pyrightconfig.json", "pyproject.toml"):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _config_options(config: Optional[Path]) -> dict:
    if config is None:
        return {}
    if config.suffix.lower() == ".json":
        payload = json.loads(config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Pyright config must contain an object: {config}")
        # Keep path resolution and inherited options anchored to the repository
        # config instead of copying a relative `extends` into /tmp.
        return {"extends": str(config)}
    if config.name == "pyproject.toml":
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
        options = payload.get("tool", {}).get("pyright", {})
        if not isinstance(options, dict):
            raise ValueError(f"[tool.pyright] must be a table: {config}")
        return dict(options)
    raise ValueError(f"Unsupported Pyright config format: {config}")


@contextmanager
def _temporary_project_config(
    root: Path,
    config: Optional[Path],
    include: Sequence[str],
    exclude: Sequence[str],
):
    """Create an explicit overlay config and remove it on every exit path."""
    options = _config_options(config)
    with tempfile.TemporaryDirectory(prefix=".hermes-pyright-", dir=root) as temp_dir:
        path = Path(temp_dir) / "pyrightconfig.json"
        config_dir = Path(temp_dir)
        options["include"] = _as_config_relative(config_dir, root, include or (".",))
        options["exclude"] = _as_config_relative(config_dir, root, exclude)
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


def _snapshot_descendants(pid: int) -> list[psutil.Process]:
    try:
        return psutil.Process(pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


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
) -> None:
    """TERM the owned group, then KILL after a bounded grace period."""
    used_group = _signal_group(pgid, signal.SIGTERM)
    if not used_group:
        _signal_descendants(descendants, force=False)
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + term_grace
    while time.monotonic() < deadline:
        if process.poll() is not None and not _group_alive(pgid):
            break
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    if _group_alive(pgid):
        _signal_group(pgid, signal.SIGKILL)
    _signal_descendants(descendants, force=True)
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
    root = Path(workspace_root).expanduser().absolute()
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
        descendants = _snapshot_descendants(process.pid)
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            _cleanup_process(process, pgid, descendants, term_grace)
            stdout, stderr = process.communicate()
            extra_stdout = exc.output or ""
            extra_stderr = exc.stderr or ""
            if isinstance(extra_stdout, bytes):
                extra_stdout = extra_stdout.decode("utf-8", errors="replace")
            if isinstance(extra_stderr, bytes):
                extra_stderr = extra_stderr.decode("utf-8", errors="replace")
            stdout = stdout or extra_stdout
            stderr = stderr or extra_stderr
        finally:
            if process.poll() is None or _group_alive(pgid):
                _cleanup_process(process, pgid, descendants, term_grace)
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
