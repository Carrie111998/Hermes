"""Shared warmup work for Dashboard and Desktop backend startup."""

from __future__ import annotations

import threading


_WARM_MODULES = (
    "hermes_cli.gateway",
    "hermes_cli.auth",
    "hermes_cli.copilot_auth",
    "hermes_cli.runtime_provider",
    "hermes_cli.skin_engine",
    "hermes_cli.inventory",
    "hermes_cli.model_switch",
)
_warm_lock = threading.Lock()
_warm_complete = False


def warm_gateway_modules() -> None:
    """Import request-hot modules exactly once per backend process.

    Import failures remain non-fatal, matching the historical web-server
    warmup. Their owning request paths still surface a real failure if the
    missing module is later required.
    """
    global _warm_complete
    if _warm_complete:
        return
    with _warm_lock:
        if _warm_complete:
            return
        for module_name in _WARM_MODULES:
            try:
                __import__(module_name)
            except Exception:
                pass
        _warm_complete = True
