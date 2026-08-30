"""Resolve the installed Codex CLI version used by Hermes.

Identity must describe the executable Hermes runs, not the newest package in a
registry. The configured executable is resolved from an explicit argument,
then ``HERMES_CODEX_BIN``, then ``codex`` on ``PATH``. Resolution is best
effort and never raises. If the executable is missing or its output cannot be
parsed, callers receive ``0.0.0`` so the wire identity remains visibly unknown
rather than claiming an uninstalled release.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_UNKNOWN_CODEX_CLI_VERSION = "0.0.0"
_VERSION_QUERY_TIMEOUT_SECONDS = 10.0
_memo: dict[str, str] = {}


def resolve_codex_executable(codex_bin: Optional[str] = None) -> str:
    """Return the Codex executable selected for app-server startup."""
    explicit = codex_bin.strip() if isinstance(codex_bin, str) else ""
    if explicit:
        return explicit
    configured = os.environ.get("HERMES_CODEX_BIN", "").strip()
    return configured or "codex"


def _parse_version(text: str) -> Optional[str]:
    """Extract the semver emitted by ``codex --version``."""
    match = re.search(
        r"(?<!\d)(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)",
        text or "",
    )
    return match.group(1) if match else None


def _query_installed_version(codex_bin: str) -> Optional[str]:
    """Run ``<codex_bin> --version`` and return its semver, or ``None``."""
    try:
        proc = subprocess.run(
            [codex_bin, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_QUERY_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        logger.debug("codex_version: %r not found", codex_bin)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("codex_version: version query failed for %r: %s", codex_bin, exc)
        return None

    if proc.returncode != 0:
        logger.debug(
            "codex_version: %r --version exited %s", codex_bin, proc.returncode
        )
        return None
    return _parse_version(proc.stdout) or _parse_version(proc.stderr)


def get_codex_cli_version(codex_bin: Optional[str] = None) -> str:
    """Return the selected executable's installed semver without raising."""
    executable = resolve_codex_executable(codex_bin)
    if executable in _memo:
        return _memo[executable]
    try:
        version = _query_installed_version(executable)
    except Exception as exc:  # defensive boundary for identity construction
        logger.debug("codex_version: resolution failed for %r: %s", executable, exc)
        version = None
    resolved = version or _UNKNOWN_CODEX_CLI_VERSION
    _memo[executable] = resolved
    return resolved


__all__ = ["get_codex_cli_version", "resolve_codex_executable"]
