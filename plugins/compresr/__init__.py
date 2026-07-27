"""Compresr Hermes plugin — a thin, out-of-tree shim over the ``compresr`` SDK.

All compression logic ships in the ``compresr`` PyPI package
(``compresr.integrations.hermes``). This shim only registers Compresr's
tool-output cache subdir so recovery paths resolve on Docker/Modal/SSH backends,
then delegates to the SDK's ``register(ctx)``. Requires ``pip install compresr``
in the interpreter that runs Hermes; fail-open and inert without it or an API key.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Literal fallback used only when the SDK isn't importable yet (so cache wiring
# still happens before the delegate below reports the missing package). When the
# SDK is present its cache.CACHE_SUBPATH is the single source of truth.
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
        logger.error(_INSTALL_HINT)
        return
    _sdk_register(ctx)


__all__ = ["register"]
