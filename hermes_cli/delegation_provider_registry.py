"""Delegation Provider Registry
=================================

Generic registry that lets plugins declare custom **delegation providers** for
``delegate_task``.  A plugin registers a resolver callable under a provider
key; when ``_resolve_delegation_credentials`` encounters an unknown provider
key (not handled by the built-in ``resolve_runtime_provider`` path), it
consults this registry first.

The descriptor returned by a resolver is a plain ``dict`` — **no vendor-specific
fields in the registry itself**.  Recognised keys:

``provider``
    The provider name to pass to ``AIAgent(provider=...)``.
``model``
    Optional model override (falls back to the parent's model when ``None``).
``api_mode``
    Transport mode (``chat_completions``, ``anthropic_messages``,
    ``acp_client``, ``codex_responses``, …).  Required when the child should
    not inherit the parent's transport.
``base_url``
    Optional direct endpoint URL.
``api_key``
    API key string, or ``""`` when auth is handled externally
    (e.g. ACP binary).
``command``
    Optional local command for ACP-style transports.
``args``
    Optional argument list for ACP-style transports.
``request_overrides``
    Optional dict of extra request parameters.
``max_output_tokens``
    Optional token ceiling for the child agent.
``metadata``
    Optional opaque dict forwarded to the child agent (trusted, never
    model-controlled).

Security constraints
--------------------
* The provider key is trusted — it comes from plugin config or the plugin's
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

#: Type alias for the resolver callable.
#: Receives ``(requested_model: Optional[str], cfg: dict)`` and returns a
#: descriptor dict (see module docstring) or raises.
DelegationResolver = Callable[[Optional[str], Dict[str, Any]], Dict[str, Any]]

_providers: Dict[str, DelegationResolver] = {}
_lock = threading.Lock()


def register_delegation_provider(
    key: str, resolver: DelegationResolver,
) -> None:
    """Register a delegation provider resolver under *key*.

    Re-registration with the same callable is idempotent (overwrites and logs
    debug).  This makes hot-reload scenarios (tests, dev loops) predictable.

    Parameters
    ----------
    key
        Provider key (e.g. ``"my-plugin-provider"``).  Must be a non-empty
        string.  Normalised to stripped lowercase before storage so callers
        can use any case variant in config.
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
            f"Delegation provider resolver must be callable, got "
            f"{type(resolver).__name__}"
        )
    normalised = (key or "").strip().lower()
    if not normalised:
        raise ValueError("Delegation provider key must be a non-empty string")

    with _lock:
        existing = _providers.get(normalised)
        _providers[normalised] = resolver

    if existing is not None:
        logger.debug(
            "Delegation provider '%s' re-registered", normalised,
        )
    else:
        logger.debug(
            "Registered delegation provider '%s'", normalised,
        )


def get_delegation_provider(key: str) -> Optional[DelegationResolver]:
    """Return the resolver registered under *key*, or ``None``."""
    if not isinstance(key, str):
        return None
    normalised = key.strip().lower()
    with _lock:
        return _providers.get(normalised)


def list_delegation_providers() -> List[str]:
    """Return all registered provider keys (sorted)."""
    with _lock:
        return sorted(_providers.keys())


def resolve_via_registry(
    key: str,
    requested_model: Optional[str],
    cfg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Try to resolve a provider via the plugin registry.

    Returns the descriptor dict if *key* matches a registered resolver, or
    ``None`` if no resolver is registered for that key (so the caller can fall
    through to the built-in path).

    Exceptions raised by the resolver propagate to the caller.
    """
    resolver = get_delegation_provider(key)
    if resolver is None:
        return None
    descriptor = resolver(requested_model, cfg)
    if not isinstance(descriptor, dict):
        raise TypeError(
            f"Delegation provider '{key}' resolver returned "
            f"{type(descriptor).__name__}, expected dict"
        )
    return descriptor


def _clear_for_testing() -> None:
    """Remove all registered providers.  Test-only utility."""
    with _lock:
        _providers.clear()
