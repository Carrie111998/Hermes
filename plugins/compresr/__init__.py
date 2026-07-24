"""Compresr Hermes plugin — out-of-tree, SDK-backed.

All compression logic lives in the ``compresr`` PyPI package
(``compresr.integrations.hermes``); this in-repo plugin is a thin shim that

1. registers the Compresr tool-output cache subdir with the generic
   out-of-tree cache surface (:func:`tools.credential_files.register_cache_dir`)
   so the recovery paths it emits are visible on Docker/Modal/SSH backends, not
   just Local, and
2. delegates registration to the SDK's ``register(ctx)``.

Requires ``pip install compresr`` in the same interpreter that runs Hermes.
Fail-open and inert without the package or an API key.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# HERMES_HOME-relative subdir the SDK writes tool-output originals to. Kept in
# sync with ``compresr.integrations.hermes.cache`` (CACHE_SUBPATH).
_COMPRESR_CACHE_SUBPATH = "cache/compresr/tool-output"

_INSTALL_HINT = (
    "compresr: the `compresr` Python package is not installed in Hermes's "
    "environment. Install it with `pip install compresr` (same interpreter "
    "that runs Hermes), then restart Hermes."
)


def register(ctx: Any) -> None:
    """Wire the generic cache surface, then hand off to the SDK integration."""
    # Make the SDK's cache dir participate in container mounts, host->backend
    # path translation, and remote sync. Without this the tool-output recovery
    # path an agent is handed is unreadable on every non-Local backend. Idempotent.
    try:
        from tools.credential_files import register_cache_dir

        register_cache_dir(_COMPRESR_CACHE_SUBPATH)
    except Exception as e:  # never block plugin load on the wiring step
        logger.debug("compresr: could not register cache dir (%s)", e)

    try:
        from compresr.integrations.hermes.plugin import register as _sdk_register
    except ImportError:
        logger.error(_INSTALL_HINT)
        return
    _sdk_register(ctx)


__all__ = ["register"]
