"""Profile-aware configuration helpers for the SimpleX platform plugin."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


def profile_scoped() -> bool:
    """Return whether a multiplexed secondary profile owns this call."""
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active

        return bool(is_multiplex_active() and current_secret_scope() is not None)
    except Exception:
        # This helper is an isolation boundary. If the multiplex state cannot
        # be established, treating the call as unscoped would fall through to
        # the process environment and could expose another profile's values.
        return True


def scoped_platform_setting(
    env_name: str,
    extra: Mapping[str, Any] | None,
    key: str,
) -> Any:
    """Read a setting without leaking the default profile's bridged env.

    The process environment belongs to the default profile. A multiplexed
    secondary profile must use its own ``PlatformConfig.extra`` mapping and
    fail closed when a key is absent. Single-profile and default-profile
    execution retain the established environment-over-config precedence.
    """
    if profile_scoped():
        return (extra or {}).get(key)
    return os.getenv(env_name)


def profile_simplex_extra() -> dict[str, Any]:
    """Load ``simplex.extra`` from the active secondary profile's config."""
    if not profile_scoped():
        return {}
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(Path(get_hermes_home()) / "config.yaml")
    except Exception:
        return {}
    if not isinstance(config, dict):
        return {}
    simplex = ((config.get("gateway") or {}).get("platforms") or {}).get("simplex")
    if not isinstance(simplex, dict):
        return {}
    extra = simplex.get("extra", simplex)
    return extra if isinstance(extra, dict) else {}
