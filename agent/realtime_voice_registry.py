"""
Realtime Voice Provider Registry
================================

Central registry for :class:`agent.realtime_voice_provider.RealtimeVoiceProvider`.
Built-in providers and plugin providers use the same contract. A built-in
registration always wins its name; plugins may replace other plugin providers
to keep development reloads predictable.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Set

from agent.realtime_voice_provider import (
    MAX_IDENTIFIER_LENGTH,
    REALTIME_VOICE_PROVIDER_API_VERSION,
    RealtimeVoiceProvider,
)
from hermes_constants import hermes_home_key

logger = logging.getLogger(__name__)

_providers: Dict[str, RealtimeVoiceProvider] = {}
_scoped_providers: Dict[str, Dict[str, RealtimeVoiceProvider]] = {}
_built_in_names: Set[str] = set()
_lock = threading.Lock()


def register_provider(
    provider: RealtimeVoiceProvider, *, scope: Optional[str] = None
) -> bool:
    """Register a plugin provider without replacing a reserved built-in."""
    return _register_provider(provider, built_in=False, scope=scope)


def _register_builtin_provider(provider: RealtimeVoiceProvider) -> bool:
    """Register a core built-in; the first built-in for a name wins."""
    return _register_provider(provider, built_in=True, scope=None)


def _register_provider(
    provider: RealtimeVoiceProvider,
    *,
    built_in: bool,
    scope: Optional[str],
) -> bool:
    if not isinstance(provider, RealtimeVoiceProvider):
        raise TypeError(
            "register_provider() expects a RealtimeVoiceProvider instance, "
            f"got {type(provider).__name__}"
        )

    name = provider.name
    if (
        not isinstance(name, str)
        or not name
        or name != name.strip()
        or len(name) > MAX_IDENTIFIER_LENGTH
    ):
        raise ValueError(
            "Realtime voice provider name must be a nonblank, trimmed identifier "
            f"no longer than {MAX_IDENTIFIER_LENGTH} characters"
        )
    key = name.lower()

    api_version = getattr(provider, "api_version", None)
    if api_version != REALTIME_VOICE_PROVIDER_API_VERSION:
        logger.warning(
            "Realtime voice provider '%s' targets API v%s; Hermes supports v%s. "
            "Registration ignored.",
            key,
            api_version,
            REALTIME_VOICE_PROVIDER_API_VERSION,
        )
        return False

    with _lock:
        if key in _built_in_names:
            logger.warning(
                "Realtime voice provider '%s' collides with a reserved built-in "
                "name; registration ignored. Built-in providers always win.",
                key,
            )
            return False

        target = _providers if scope is None else _scoped_providers.setdefault(scope, {})
        existing = target.get(key)
        if built_in:
            _built_in_names.add(key)
        target[key] = provider

    if existing is not None:
        logger.debug(
            "Realtime voice provider '%s' re-registered (was %r)",
            key,
            type(existing).__name__,
        )
    else:
        logger.debug(
            "Registered realtime voice provider '%s' (%s)",
            key,
            type(provider).__name__,
        )
    return True


def list_providers(*, scope: Optional[str] = None) -> List[RealtimeVoiceProvider]:
    """Return registered providers sorted by normalized name."""
    with _lock:
        merged = dict(_providers)
        merged.update(_scoped_providers.get(scope or hermes_home_key(), {}))
        for name in _built_in_names:
            merged[name] = _providers[name]
        items = list(merged.items())
    return [provider for _, provider in sorted(items)]


def get_provider(
    name: str, *, scope: Optional[str] = None
) -> Optional[RealtimeVoiceProvider]:
    """Return a provider by case-insensitive, whitespace-tolerant name."""
    if not isinstance(name, str):
        return None
    key = name.strip().lower()
    with _lock:
        if key in _built_in_names:
            return _providers.get(key)
        scoped_provider = _scoped_providers.get(
            scope or hermes_home_key(), {}
        ).get(key)
        if scoped_provider is not None:
            return scoped_provider
        return _providers.get(key)


def snapshot_registration(
    name: str, *, scope: Optional[str] = None
) -> Optional[RealtimeVoiceProvider]:
    """Return the exact provider installed in one registry scope."""
    key = name.strip().lower()
    with _lock:
        target = _providers if scope is None else _scoped_providers.get(scope, {})
        return target.get(key)


def restore_registration(
    name: str,
    current: RealtimeVoiceProvider,
    previous: Optional[RealtimeVoiceProvider],
    *,
    scope: Optional[str] = None,
) -> bool:
    """Restore a registration only while *current* still owns its slot."""
    key = name.strip().lower()
    with _lock:
        if scope is None:
            if key in _built_in_names:
                return False
            target = _providers
        else:
            target = _scoped_providers.get(scope)
            if target is None:
                return False
        if target.get(key) is not current:
            return False
        if previous is None:
            target.pop(key, None)
        else:
            target[key] = previous
        if scope is not None and not target:
            _scoped_providers.pop(scope, None)
    return True


def is_builtin_provider(name: str) -> bool:
    """Return whether *name* is reserved by an active built-in provider."""
    if not isinstance(name, str):
        return False
    with _lock:
        return name.strip().lower() in _built_in_names


def _reset_for_tests() -> None:
    """Clear all providers and built-in reservations. Test-only."""
    with _lock:
        _providers.clear()
        _scoped_providers.clear()
        _built_in_names.clear()
