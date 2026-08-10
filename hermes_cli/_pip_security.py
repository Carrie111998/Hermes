"""Stdlib-only guards for venv-managed pip installs.

This module is intentionally dependency-light: ``hermes_cli._early_recovery``
imports it before the normal CLI dependency graph is available.  Keep the
version parser conservative and fail closed when ``pip --version`` does not
prove a stable release at or above the security floor.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any


MIN_PIP_VERSION = (26, 1, 2)
MIN_PIP_SPEC = "pip>=26.1.2"

# ``pip --version`` emits one authoritative record such as
# ``pip 26.1.2 from /.../site-packages/pip (python 3.13)``.  Other output
# (notably update warnings) can contain a misleading ``pip <version>`` token,
# so only a canonical record is evidence for the floor.
_PIP_CANONICAL_VERSION_LINE = re.compile(
    r"^\s*pip\s+(?P<version>[^\s]+)\s+from\s+"
    r"(?P<path>(?:/|[A-Za-z]:[\\/]|\\\\)[^\r\n\"]*[\\/]pip)"
    r"(?:\s+\(python\s+\d+(?:\.\d+){1,3}\))?\s*$",
    re.IGNORECASE,
)
_STABLE_VERSION = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)(?:\.post\d+)?$",
    re.IGNORECASE,
)


def stable_version_tuple(value: str) -> tuple[int, ...] | None:
    """Return release components only for a stable version string.

    This deliberately accepts a PEP 440 post-release (``1.2.3.post1``),
    which is still a stable release for floor checks, but rejects every
    pre/dev/rc/local or unknown suffix.  It is stdlib-only because callers in
    early-recovery paths cannot assume ``packaging`` is importable.
    """

    if not isinstance(value, str):
        return None
    match = _STABLE_VERSION.fullmatch(value.strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group("release").split("."))
    except ValueError:
        return None


def pip_version_meets_floor(output: str) -> bool:
    """Return whether ``pip --version`` output proves a stable floor.

    Numeric-prefix parsing alone would accept ``26.1.2.dev0`` and
    ``26.1.2.rc1`` as the final release, and warning text can mention a newer
    pip than the installed interpreter.  Require exactly one canonical
    ``pip <version> from <path>`` record. Only an exact release token or a
    PEP 440 post-release suffix is accepted; pre/dev/rc/local, unknown,
    malformed, missing, and conflicting records fail closed.
    """

    versions: list[tuple[int, ...]] = []
    for line in (output or "").splitlines():
        match = _PIP_CANONICAL_VERSION_LINE.fullmatch(line)
        if not match:
            continue
        token = match.group("version")
        version = stable_version_tuple(token)
        if version is None:
            return False
        versions.append(version)
    return len(versions) == 1 and versions[0] >= MIN_PIP_VERSION


def ensure_pip_floor(
    pip_cmd: Sequence[str],
    *,
    timeout: int = 120,
    runner: Callable[..., Any] = subprocess.run,
    creationflags: int = 0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Upgrade and verify ``pip_cmd`` before a managed package transaction.

    ``runner`` is injectable so callers can preserve their local subprocess
    wrappers and tests can exercise the real command shape without spawning a
    package manager.  The helper never raises for probe/upgrade failures.
    """

    def _run(args: Sequence[str], *, command_timeout: int):
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": command_timeout,
            "stdin": subprocess.DEVNULL,
            "creationflags": creationflags,
        }
        if cwd is not None:
            kwargs["cwd"] = cwd
        if env is not None:
            kwargs["env"] = env
        return runner(list(args), **kwargs)

    def _probe():
        return _run([*pip_cmd, "--version"], command_timeout=15)

    try:
        probe = _probe()
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pip floor probe failed: {exc}"
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip()
        return False, f"pip floor probe failed: {detail or 'pip --version failed'}"
    if pip_version_meets_floor(probe.stdout or ""):
        return True, ""

    try:
        upgrade = _run(
            [*pip_cmd, "install", "--upgrade", MIN_PIP_SPEC],
            command_timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pip floor upgrade failed: {exc}"
    if upgrade.returncode != 0:
        detail = (upgrade.stderr or upgrade.stdout or "").strip()
        return False, f"pip floor upgrade failed: {detail or 'pip install failed'}"

    try:
        verified = _probe()
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        return False, f"pip floor verification failed: {exc}"
    if verified.returncode != 0 or not pip_version_meets_floor(verified.stdout or ""):
        detail = (verified.stderr or verified.stdout or "").strip()
        return False, (
            f"pip remains below {MIN_PIP_SPEC} after upgrade: "
            f"{detail or 'version was not reported'}"
        )
    return True, ""


class PipFloorError(RuntimeError):
    """Raised when a managed direct-pip transaction cannot prove the floor."""
