"""
Timezone-aware clock for Hermes.

Provides a single ``now()`` helper that returns a timezone-aware datetime
based on the user's configured IANA timezone (e.g. ``Asia/Kolkata``).

Resolution order:
  1. ``HERMES_TIMEZONE`` environment variable
  2. ``timezone`` key in ``~/.hermes/config.yaml``
  3. Falls back to the server's local time (``datetime.now().astimezone()``)

Invalid timezone values log a warning and fall back safely — Hermes never
crashes due to a bad timezone string.
"""

import logging
import os
from datetime import datetime
from hermes_constants import get_config_path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python 3.8 fallback (shouldn't be needed — Hermes requires 3.9+)
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# Cached ZoneInfo (or None for server-local), keyed by the resolution context.
#
# The timezone is resolved from two profile-sensitive inputs: the
# ``HERMES_TIMEZONE`` env var and ``config.yaml`` at ``get_config_path()`` —
# and the latter is ``get_hermes_home() / "config.yaml"``, which the
# multiplexed gateway scopes per profile via ``set_hermes_home_override()``
# (a ContextVar). A single process-global cache resolved once at first use
# would freeze whichever zone the *first* ``now()`` saw — typically the root
# home at unscoped startup — and hand it to every profile forever, ignoring
# each profile's own ``timezone:`` key. Keying by (env, resolved config path)
# lets each profile resolve and cache its own zone independently.
# Call reset_cache() to force re-resolution (e.g. after a config edit).
_tz_cache: dict = {}


def _resolve_timezone_name() -> str:
    """Read the configured IANA timezone string (or empty string).

    This does file I/O when falling through to config.yaml, so callers
    should cache the result rather than calling on every ``now()``.
    """
    # 1. Environment variable (highest priority — set by Supervisor, etc.)
    tz_env = os.getenv("HERMES_TIMEZONE", "").strip()
    if tz_env:
        return tz_env

    # 2. config.yaml ``timezone`` key
    try:
        # Prefer the shared cached raw-config reader (mtime/size-keyed cache +
        # libyaml C loader) — a direct yaml.safe_load of a large config.yaml
        # costs ~100ms+ and this used to run inside the FIRST system prompt
        # build, on the time-to-first-token critical path.
        try:
            from hermes_cli.config import read_raw_config
            cfg = read_raw_config() or {}
        except Exception:
            import yaml
            config_path = get_config_path()
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            else:
                cfg = {}
        if cfg:
            # Managed scope: an administrator can pin ``timezone`` too. Overlay
            # via the shared helper (fail-open) since this reads config.yaml directly.
            try:
                from hermes_cli import managed_scope
                cfg = managed_scope.apply_managed_overlay(cfg)
            except Exception:
                pass
            tz_cfg = cfg.get("timezone", "")
            if isinstance(tz_cfg, str) and tz_cfg.strip():
                return tz_cfg.strip()
    except Exception:
        pass

    return ""


def _get_zoneinfo(name: str) -> Optional[ZoneInfo]:
    """Validate and return a ZoneInfo, or None if invalid."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, Exception) as exc:
        logger.warning(
            "Invalid timezone '%s': %s. Falling back to server local time.",
            name, exc,
        )
        return None


def _tz_cache_key() -> str:
    """Identity of the current timezone-resolution context.

    Both inputs to :func:`_resolve_timezone_name` are folded in: the
    ``HERMES_TIMEZONE`` env var and the resolved ``config.yaml`` path (which is
    profile-scoped via ``get_hermes_home()``). Two profiles with distinct homes
    get distinct keys, so one profile's zone can never be served to another.
    """
    try:
        config_path = str(get_config_path())
    except Exception:
        config_path = ""
    return f"{os.getenv('HERMES_TIMEZONE', '').strip()}\x00{config_path}"


def get_timezone() -> Optional[ZoneInfo]:
    """Return the user's configured ZoneInfo, or None (meaning server-local).

    Cached per resolution context (env + profile-scoped config path). Call
    ``reset_cache()`` after config changes.
    """
    key = _tz_cache_key()
    if key not in _tz_cache:
        _tz_cache[key] = _get_zoneinfo(_resolve_timezone_name())
    return _tz_cache[key]


def reset_cache() -> None:
    """Clear the cached timezone so the next call re-resolves it.

    Call this after the configured timezone may have changed (e.g. after a
    config edit or ``HERMES_TIMEZONE`` update) to force ``get_timezone()`` /
    ``now()`` to read the new value instead of the value cached at first use.
    """
    _tz_cache.clear()


def now() -> datetime:
    """
    Return the current time as a timezone-aware datetime.

    If a valid timezone is configured, returns wall-clock time in that zone.
    Otherwise returns the server's local time (via ``astimezone()``).
    """
    tz = get_timezone()
    if tz is not None:
        return datetime.now(tz)
    # No timezone configured — use server-local (still tz-aware)
    return datetime.now().astimezone()


