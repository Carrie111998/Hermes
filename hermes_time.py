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


def get_timezone_name() -> str:
    """Return the configured IANA timezone name (e.g. ``Asia/Shanghai``).

    Returns an empty string when no timezone is configured.
    """
    timezone = get_timezone()  # ensure the cache is resolved and validated
    return (_cached_tz_name or "") if timezone is not None else ""


def get_utc_offset_display(now_dt: Optional[datetime] = None) -> str:
    """Return the UTC offset for the configured timezone (e.g. ``+08:00``).

    ``now_dt`` defaults to ``now()``. With no configured timezone, the
    server-local offset is used. Returns ``+00:00`` for UTC.
    """
    current = now_dt if now_dt is not None else now()
    offset = current.utcoffset()
    if offset is None:  # defensive: now() is always tz-aware
        return "+00:00"
    total_seconds = int(offset.total_seconds())
    sign = "+" if total_seconds >= 0 else "-"
    hours, remainder = divmod(abs(total_seconds), 3600)
    minutes = remainder // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def format_current_time_context(now_dt: Optional[datetime] = None) -> str:
    """Format a ``[Runtime Context]`` time block for per-turn injection.

    Injected into the API copy of the current turn's user message (never the
    cached system prompt), so the volatile timestamp re-renders every turn
    while the cached prompt prefix stays byte-stable. Generates ``now()`` when
    ``now_dt`` is omitted; tests pass a fixed value for determinism.

    Example::

        [Runtime Context]
        Current datetime: 2026-08-29T13:25:42+08:00
        Timezone: Asia/Shanghai
        UTC offset: +08:00
    """
    current = now_dt if now_dt is not None else now()
    lines = [
        "[Runtime Context]",
        f"Current datetime: {current.isoformat(timespec='seconds')}",
    ]
    tz_name = get_timezone_name()
    if tz_name:
        lines.append(f"Timezone: {tz_name}")
    lines.append(f"UTC offset: {get_utc_offset_display(current)}")
    return "\n".join(lines)


def prepend_current_time_context(
    text: str,
    now_dt: Optional[datetime] = None,
) -> str:
    """Prepend the per-turn current-time block to *text*.

    Shared by the turn prologue and the max-iterations summary path so both
    API copies carry the same volatile time block, formatted from the same
    instant. Fail-open: if time formatting fails for any reason, *text* is
    returned unchanged so a formatting problem can never block a turn.
    """
    try:
        block = format_current_time_context(now_dt=now_dt)
        if block:
            return block + "\n\n" + text
    except Exception:
        pass
    return text

