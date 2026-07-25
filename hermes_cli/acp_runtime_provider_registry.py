"""ACP Runtime Provider Registry
=================================

Generic registry that lets plugins declare custom **ACP runtime providers**
for the ``/acp-client-runtime`` slash command.  A plugin registers a resolver
callable under a runtime key; when the switch encounters the key as the
command argument, it resolves the descriptor and writes config generically.

This registry resolves descriptors for the main agent's ACP runtime —
resolvers receive ``(requested_model, operator_cfg)`` and return a
descriptor consumed by ``acp_runtime_switch.apply()`` and
``agent_init.py``.

The descriptor returned by a resolver is a plain ``dict`` — **no
vendor-specific fields in the registry itself**.  Recognised keys:

``provider``
    The canonical provider name to pass to ``AIAgent(provider=...)``
    (``acp_client`` or ``acp-client``).
``api_mode``
    Transport mode — always ``acp_client`` for runtime providers.
``display_provider``
    Human-readable provider name for banner/session metadata
    (e.g. ``claude-code-acp``).
``model``
    Model to use (e.g. ``opus[1m]``).  Operator-configurable, never
    model-controlled.
``command``
    Local command for the ACP transport (e.g. ``npx``).
``args``
    Argument list for the command.
``base_url``
    Optional direct endpoint URL (usually empty for ACP).
``api_key``
    API key string, or ``""`` when auth is handled externally.
``metadata``
    Optional opaque dict forwarded to the agent.

Security constraints
--------------------
* The runtime key is trusted — it comes from plugin config or the plugin's
  ``register(ctx)`` call, never from model output.
* Resolver callables must validate their own inputs.  The registry does not
  inspect descriptor contents; it only dispatches by key.
* ``acp_command`` / ``acp_args`` remain in ``_MODEL_HIDDEN_TASK_FIELDS`` — the
  model can never inject them via task fields.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Type alias for the runtime resolver callable.
#: Receives ``(requested_model: Optional[str], cfg: dict)`` and returns a
#: descriptor dict (see module docstring) or raises.
ACPRuntimeResolver = Callable[[Optional[str], Dict[str, Any]], Dict[str, Any]]

_providers: Dict[str, ACPRuntimeResolver] = {}
_lock = threading.Lock()


def register_acp_runtime_provider(
    key: str, resolver: ACPRuntimeResolver,
) -> None:
    """Register an ACP runtime provider resolver under *key*.

    Re-registration with the same callable is idempotent (overwrites and logs
    debug).  This makes hot-reload scenarios (tests, dev loops) predictable.

    Parameters
    ----------
    key
        Runtime provider key (e.g. ``"claude-agent-acp"``).  Must be a
        non-empty string.  Normalised to stripped lowercase before storage.
    resolver
        Callable ``(requested_model, cfg) -> descriptor_dict``.

    Raises
    ------
    TypeError
        If *resolver* is not callable.
    ValueError
        If *key* is empty after stripping.
    """
    if not callable(resolver):
        raise TypeError(
            f"ACP runtime provider resolver must be callable, got "
            f"{type(resolver).__name__}"
        )
    normalised = (key or "").strip().lower()
    if not normalised:
        raise ValueError("ACP runtime provider key must be a non-empty string")

    with _lock:
        existing = _providers.get(normalised)
        _providers[normalised] = resolver

    if existing is not None:
        logger.debug(
            "ACP runtime provider '%s' re-registered", normalised,
        )
    else:
        logger.debug(
            "Registered ACP runtime provider '%s'", normalised,
        )


def get_acp_runtime_provider(key: str) -> Optional[ACPRuntimeResolver]:
    """Return the resolver registered under *key*, or ``None``."""
    if not isinstance(key, str):
        return None
    normalised = key.strip().lower()
    with _lock:
        return _providers.get(normalised)


def list_acp_runtime_providers() -> List[str]:
    """Return all registered runtime provider keys (sorted)."""
    with _lock:
        return sorted(_providers.keys())


def resolve_acp_runtime_provider(
    key: str,
    requested_model: Optional[str],
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Try to resolve a runtime provider via the plugin registry.

    Returns the descriptor dict if *key* matches a registered resolver, or
    ``None`` if no resolver is registered for that key (so the caller can fall
    through to the built-in PATH-based path).

    Exceptions raised by the resolver propagate to the caller.
    """
    resolver = get_acp_runtime_provider(key)
    if resolver is None:
        return None
    descriptor = resolver(requested_model, cfg)
    if not isinstance(descriptor, dict):
        raise TypeError(
            f"ACP runtime provider '{key}' resolver returned "
            f"{type(descriptor).__name__}, expected dict"
        )
    return descriptor


def _clear_for_testing() -> None:
    """Remove all registered providers.  Test-only utility."""
    with _lock:
        _providers.clear()
