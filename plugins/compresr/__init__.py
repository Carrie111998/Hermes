"""Compresr Hermes plugin — a thin shim over the ``compresr`` PyPI SDK.

Registers Compresr's tool-output cache subdir (so cached files resolve on
Docker/Modal/SSH backends), then delegates to the SDK's ``register(ctx)``.
Fail-open: inert without the SDK or an API key.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Used only when the SDK isn't importable, so cache wiring still happens;
# otherwise the SDK's cache.CACHE_SUBPATH is the source of truth.
_FALLBACK_CACHE_SUBPATH = "cache/compresr/tool-output"

_INSTALL_HINT = (
    "compresr: the `compresr` Python package is not installed in Hermes's "
    "environment. Install it with `pip install compresr` (same interpreter "
    "that runs Hermes), then restart Hermes."
)


def register(ctx: Any) -> None:
    """Wire the SDK's cache dir into the backend surface, then delegate to it."""
    try:
        from compresr.integrations.hermes.cache import CACHE_SUBPATH
    except Exception:
        CACHE_SUBPATH = _FALLBACK_CACHE_SUBPATH

    try:
        from tools.credential_files import register_cache_dir

        register_cache_dir(CACHE_SUBPATH)
    except Exception as e:  # cache wiring must never block plugin load
        logger.debug("compresr: could not register cache dir (%s)", e)

    try:
        from compresr.integrations.hermes.plugin import register as _sdk_register
    except ImportError:
        # Warn, not error: the fail-open state shouldn't trip error alerting.
        logger.warning(_INSTALL_HINT)
        return
    _sdk_register(ctx)


__all__ = ["register"]
