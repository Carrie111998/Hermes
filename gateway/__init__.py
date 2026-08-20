"""
Hermes Gateway - Multi-platform messaging integration.

This module provides a unified gateway for connecting the Hermes agent
to various messaging platforms (Telegram, Discord, WhatsApp, Weixin, and more) with:
- Session management (persistent conversations with reset policies)
- Dynamic context injection (agent knows where messages come from)
- Delivery routing (cron job outputs to appropriate channels)
- Platform-specific toolsets (different capabilities per platform)

Import cost
-----------
These re-exports used to be plain ``from .config import ...`` /
``from .session import ...`` / ``from .delivery import ...`` at module scope.
Because a package's ``__init__`` runs before *any* of its submodules, that made
``import gateway.channel_directory`` -- all ``hermes send --list`` needs -- cost
**295 modules** over the bare interpreter floor, of which ``channel_directory``
itself was exactly one. The other 294 came from the eager chain:
``gateway.config`` -> ``agent.secret_scope``, ``gateway.session`` ->
``agent.turn_context``, ``gateway.delivery`` -> ``gateway.dead_targets``, and
through them ``requests`` + ``urllib3`` + ``agent.context_engine``.

PEP 562 ``__getattr__`` defers all of it to first attribute access, so the
package namespace is unchanged for every caller while a submodule import pays
only for the submodule. ``gateway run`` reaches the same modules a moment later
and pays the same total.

Two access shapes must keep working, so both are handled below:

* ``from gateway import GatewayConfig`` -- a re-exported *name*, resolved via
  ``_LAZY_NAMES``. (No caller in the tree does this today; the shim exists so
  the package's public API is not narrowed by an import-cost change.)
* ``import gateway`` followed by ``gateway.session.SessionStore`` -- a
  *submodule* attribute, which the old eager imports happened to bind as a side
  effect. ``importlib.import_module`` restores that.

Regression test: ``tests/hermes_cli/test_send_import_cost.py``.
"""

from typing import TYPE_CHECKING

# name -> submodule that defines it
_LAZY_NAMES = {
    "GatewayConfig": ".config",
    "PlatformConfig": ".config",
    "HomeChannel": ".config",
    "load_gateway_config": ".config",
    "SessionContext": ".session",
    "SessionStore": ".session",
    "SessionResetPolicy": ".session",
    "build_session_context_prompt": ".session",
    "DeliveryRouter": ".delivery",
    "DeliveryTarget": ".delivery",
}

if TYPE_CHECKING:  # pragma: no cover - type checkers only, never at runtime
    from .config import GatewayConfig, HomeChannel, PlatformConfig, load_gateway_config
    from .delivery import DeliveryRouter, DeliveryTarget
    from .session import (
        SessionContext,
        SessionResetPolicy,
        SessionStore,
        build_session_context_prompt,
    )


def __getattr__(name: str):
    """Resolve a re-exported name, or a submodule, on first access (PEP 562)."""
    import importlib

    submodule = _LAZY_NAMES.get(name)
    if submodule is not None:
        value = getattr(importlib.import_module(submodule, __name__), name)
        globals()[name] = value  # cache: __getattr__ only fires while unbound
        return value

    # Submodule attribute access after a bare ``import gateway``. The old eager
    # re-exports bound ``gateway.config`` / ``.session`` / ``.delivery`` as a
    # side effect; keep every submodule reachable the same way.
    if not name.startswith("_"):
        try:
            return importlib.import_module("." + name, __name__)
        except ImportError:
            pass

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(_LAZY_NAMES))


__all__ = [
    # Config
    "GatewayConfig",
    "PlatformConfig",
    "HomeChannel",
    "load_gateway_config",
    # Session
    "SessionContext",
    "SessionStore",
    "SessionResetPolicy",
    "build_session_context_prompt",
    # Delivery
    "DeliveryRouter",
    "DeliveryTarget",
]
