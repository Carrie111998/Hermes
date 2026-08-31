"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory
(design #19214/#19242: `terminal.cwd` is bridged once to `TERMINAL_CWD` at
gateway/cron startup). The local-CLI backend deliberately leaves it unset and
relies on the launch dir. Reading it in one place keeps the system prompt, the
tool surfaces, and context-file discovery agreeing on where the agent lives.

Multi-session gateways can pin a logical cwd via the `_SESSION_CWD`
contextvar; CLI/cron fall through to `TERMINAL_CWD`/launch cwd.
"""

import logging
import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)

# The Python package/source root (this file lives at <root>/agent/runtime_cwd.py).
# When a backend is launched from, or self-spawns into, this tree (the desktop
# app default), an os.getcwd() fallback would inject this repo's contributor
# AGENTS.md as authoritative project context. Context discovery must never
# resolve here.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_install_tree(p: Path) -> bool:
    # True only when p IS the package root or sits inside it. Ancestors of the
    # package root (a user home that happens to contain the checkout, a --user
    # site-packages parent) are legitimate workspaces and must not be blocked.
    try:
        p = p.resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context."""
    return _SESSION_CWD.set((cwd or "").strip())


def clear_session_cwd() -> None:
    _SESSION_CWD.set("")


def _session_cwd_override() -> str:
    value = _SESSION_CWD.get()
    if value is _UNSET:
        return ""
    return str(value).strip()


def resolve_agent_cwd() -> Path:
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
        logger.warning("configured working directory does not exist: %s", override)
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            return p
        logger.warning("TERMINAL_CWD does not exist: %s", raw)
    return Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    # None means "no configured cwd": build_context_files_prompt then falls back
    # to the launch dir (os.getcwd()), correct for a local CLI launched inside a
    # real project. A configured path is validated here (previously it was passed
    # through unchecked, diverging from resolve_agent_cwd). An explicitly
    # configured path is otherwise honored verbatim — including the Hermes
    # source tree itself, which is a legitimate workspace when the user is
    # developing Hermes (per-surface policy for fallback-picked directories
    # lives in build_context_files_prompt; see #64590).
    override = _session_cwd_override()
    if override:
        p = Path(override).expanduser()
        if not p.is_dir():
            logger.warning("configured working directory does not exist: %s", override)
        else:
            return p
        return None
    raw = os.environ.get("TERMINAL_CWD", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_dir():
            logger.warning("TERMINAL_CWD does not exist: %s", raw)
        else:
            return p
    return None


@dataclass(frozen=True)
class ProjectContextResolution:
    """Session-static repository-context policy, separate from tool cwd."""

    mode: str
    root: Optional[Path]
    repository_context_active: bool
    skip_context_files: bool
    load_soul_identity: bool


def _normalize_project_context_mode(value: object) -> str:
    mode = str(value or "auto").strip().lower()
    if mode not in {"off", "assigned", "auto"}:
        logger.warning("unknown agent.project_context mode %r; using auto", value)
        return "auto"
    return mode


def _validated_context_root(value: object) -> Optional[Path]:
    if not value:
        return None
    try:
        root = Path(str(value)).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not root.is_dir():
        logger.warning("assigned project-context directory does not exist: %s", value)
        return None
    return root


def _auto_context_root(platform: str | None) -> Optional[Path]:
    root = resolve_context_cwd()
    if root is None:
        root = _validated_context_root(os.getcwd())
        if root is None:
            return None
        if _is_install_tree(root) and (platform or "").lower() not in {"cli", "tui"}:
            return None
    root = root.resolve()
    try:
        from agent.coding_context import _git_root, _marker_root

        home = Path.home().resolve()
        git_root = _git_root(root)
        if (git_root is not None and git_root != home) or _marker_root(root) is not None:
            return root
    except (OSError, RuntimeError):
        return None

    # Context-only files outside a code workspace are loaded only from cwd by
    # the existing builder. Match that boundary instead of scanning arbitrary
    # parents and granting them prompt authority.
    local_context_markers = {
        ".hermes.md", "HERMES.md", "AGENTS.md", "AGENTS.override.md",
        "agents.md", "CLAUDE.md", "claude.md", ".cursorrules", ".cursor/rules",
    }
    try:
        if any((root / marker).exists() for marker in local_context_markers):
            return root
    except OSError:
        return None
    return None


def resolve_project_context(
    mode: object,
    *,
    assigned_workdir: object = None,
    entrypoint_skip: bool = False,
    entrypoint_load_soul: bool = False,
    platform: str | None = None,
) -> ProjectContextResolution:
    """Resolve context authority without changing terminal/file-tool cwd.

    ``assigned`` accepts only an owner-supplied workdir. Dispatcher workers
    carry that value in ``HERMES_KANBAN_WORKSPACE``; ambient cwd and
    ``TERMINAL_CWD`` never widen the mode. ``entrypoint_skip`` may narrow any
    mode. A profile policy of ``off`` still loads SOUL; an explicit legacy
    entry-point skip keeps its historical authority to suppress all context
    unless that caller separately requests SOUL identity.
    """
    normalized = _normalize_project_context_mode(mode)
    root: Optional[Path] = None
    if not entrypoint_skip:
        if normalized == "auto":
            root = _auto_context_root(platform)
        elif normalized == "assigned":
            explicit = assigned_workdir
            if explicit is None:
                try:
                    from agent.delegation_context import has_dispatcher_owned_worker_task

                    if has_dispatcher_owned_worker_task():
                        explicit = os.environ.get("HERMES_KANBAN_WORKSPACE")
                except Exception:
                    explicit = None
            root = _validated_context_root(explicit)

    active = root is not None and normalized != "off" and not entrypoint_skip
    return ProjectContextResolution(
        mode=normalized,
        root=root if active else None,
        repository_context_active=active,
        skip_context_files=not active,
        load_soul_identity=bool(entrypoint_load_soul or not entrypoint_skip),
    )
