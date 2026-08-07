"""Shared environment reload behavior for CLI and TUI surfaces."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SSH_ENV_KEYS = frozenset(
    {
        "TERMINAL_SSH_HOST",
        "TERMINAL_SSH_PORT",
        "TERMINAL_SSH_USER",
        "TERMINAL_SSH_KEY",
    }
)


def reload_env_with_ssh_invalidation() -> tuple[int, int]:
    """Reload ``.env`` and invalidate cached SSH backends if SSH settings changed.

    Returns ``(updated_variable_count, cleared_ssh_environment_count)``.
    Cleanup is best-effort: a successful environment reload is not turned into
    a user-facing failure merely because stale-backend cleanup raises.
    """
    from hermes_cli.config import reload_env

    before = {key: os.environ.get(key) for key in SSH_ENV_KEYS}
    updated = reload_env()
    after = {key: os.environ.get(key) for key in SSH_ENV_KEYS}

    if before == after:
        return updated, 0

    try:
        from tools.terminal_tool import cleanup_ssh_environments

        return updated, cleanup_ssh_environments()
    except Exception as exc:
        logger.debug("Failed to clear SSH environments after reload: %s", exc)
        return updated, 0
