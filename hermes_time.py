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
import math
import os
from datetime import datetime
from hermes_constants import get_config_path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Python 3.8 fallback (shouldn't be needed — Hermes requires 3.9+)
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# Cached state — resolved once, reused on every call.
# Call reset_cache() to force re-resolution (e.g. after config changes).
_cached_tz: Optional[ZoneInfo] = None
_cached_tz_name: Optional[str] = None
_cache_resolved: bool = False


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


def get_timezone() -> Optional[ZoneInfo]:
    """Return the user's configured ZoneInfo, or None (meaning server-local).

    Resolved once and cached. Call ``reset_cache()`` after config changes.
    """
    global _cached_tz, _cached_tz_name, _cache_resolved
    if not _cache_resolved:
        _cached_tz_name = _resolve_timezone_name()
        _cached_tz = _get_zoneinfo(_cached_tz_name)
        _cache_resolved = True
    return _cached_tz


def reset_cache() -> None:
    """Clear the cached timezone so the next call re-resolves it.

    Call this after the configured timezone may have changed (e.g. after a
    config edit or ``HERMES_TIMEZONE`` update) to force ``get_timezone()`` /
    ``now()`` to read the new value instead of the value cached at first use.
    """
    global _cached_tz, _cached_tz_name, _cache_resolved
    _cached_tz = None
    _cached_tz_name = None
    _cache_resolved = False


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


def _expand_portable_strftime_directives(dt: datetime, format_string: str) -> str:
    """Expand directives unavailable on some Python platforms."""
    parts = []
    index = 0

    while index < len(format_string):
        char = format_string[index]
        if char != "%" or index + 1 >= len(format_string):
            parts.append(char)
            index += 1
            continue

        directive = format_string[index + 1]
        if directive == "%":
            # Keep the escape intact for the final strftime pass.
            parts.append("%%")
        elif directive == "k":
            parts.append(f"{dt.hour:2d}")
        elif directive == "l":
            parts.append(f"{dt.hour % 12 or 12:2d}")
        elif directive == "P":
            # A locale-derived replacement can theoretically contain a percent.
            parts.append(dt.strftime("%p").lower().replace("%", "%%"))
        elif directive == "s":
            parts.append(str(math.floor(dt.timestamp())))
        else:
            parts.append(f"%{directive}")
        index += 2

    return "".join(parts)


def _safe_strftime(dt: datetime, format_string: str) -> str:
    """Apply strftime without letting one unsupported directive break UI."""
    try:
        return dt.strftime(format_string)
    except (UnicodeError, ValueError):
        # Windows rejects some unknown or POSIX-only directives that glibc
        # preserves. Expand valid simple directives one at a time and leave the
        # unsupported token literal, matching the shared TypeScript formatter.
        parts = []
        index = 0
        while index < len(format_string):
            char = format_string[index]
            if char != "%" or index + 1 >= len(format_string):
                parts.append(char)
                index += 1
                continue

            directive = format_string[index + 1]
            token = f"%{directive}"
            if directive == "%":
                parts.append("%")
            else:
                try:
                    parts.append(dt.strftime(token))
                except (UnicodeError, ValueError):
                    parts.append(token)
            index += 2

        return "".join(parts)


def format_display_timestamp(
    value: Any = None,
    *,
    enabled: bool,
    format_string: str = "%H:%M",
    tz=None,
) -> str:
    """Format a human-facing timestamp with Hermes' ``strftime`` contract.

    This helper is display-only: it returns an unadorned label and never
    mutates message content or protocol payloads. ``value`` may be a datetime
    or Unix epoch seconds. Omitting it uses the current instant from the Hermes
    clock, rendered in the surface-local timezone unless ``tz`` is supplied.
    Callers own the surrounding UI decoration (brackets, dim styling, separators).
    """
    if not enabled:
        return ""

    if value is None:
        # Display labels follow the human-facing surface's local timezone.
        # now() may honor the agent's configured timezone, so normalize
        # it just as the epoch path below does before formatting.
        dt = now().astimezone()
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=tz)
        if tz is None:
            dt = dt.astimezone()
    else:
        return ""

    if tz is not None:
        if dt.tzinfo is None:
            dt = dt.astimezone()
        dt = dt.astimezone(tz)

    portable_format = _expand_portable_strftime_directives(dt, str(format_string or "%H:%M"))
    return _safe_strftime(dt, portable_format)
