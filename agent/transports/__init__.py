"""Transport layer types and registry for provider response normalization.

Usage:
    from agent.transports import get_transport
    transport = get_transport("anthropic_messages")
    result = transport.normalize_response(raw_response)
"""

from agent.transports.types import (
    NormalizedResponse,
    ToolCall,
    Usage,
    build_tool_call,
    map_finish_reason,
)

# Deliberate re-export: these live in .types but are part of this package's
# public surface, so `from agent.transports import NormalizedResponse` works.
# Declared in __all__ rather than suppressed with `# noqa: F401` — a trailing
# noqa on the closing paren above was INERT, because ruff attributes F401 to
# the individual name lines, not to the line the statement ends on. It read as
# a live suppression for as long as the F group was off. Names listed here are
# "used" as far as F401 is concerned, and the intent is stated rather than
# silenced.
__all__ = [
    "NormalizedResponse",
    "ToolCall",
    "Usage",
    "build_tool_call",
    "map_finish_reason",
    "get_transport",
    "register_transport",
]

_REGISTRY: dict = {}
_discovered: bool = False


def register_transport(api_mode: str, transport_cls: type) -> None:
    """Register a transport class for an api_mode string."""
    _REGISTRY[api_mode] = transport_cls


def get_transport(api_mode: str):
    """Get a transport instance for the given api_mode.

    Returns None if no transport is registered for this api_mode.
    This allows gradual migration — call sites can check for None
    and fall back to the legacy code path.
    """
    global _discovered
    if not _discovered:
        _discover_transports()
    cls = _REGISTRY.get(api_mode)
    if cls is None:
        # The registry can be partially populated when a specific transport
        # module was imported directly (for example chat_completions before
        # codex).  Discover on misses, not only when the registry is empty, so
        # test/order-dependent imports do not make valid api_modes unavailable.
        _discover_transports()
        cls = _REGISTRY.get(api_mode)
    if cls is None:
        return None
    return cls()


def _discover_transports() -> None:
    """Import all transport modules to trigger auto-registration."""
    global _discovered
    _discovered = True
    try:
        import agent.transports.anthropic  # noqa: F401
    except ImportError:
        pass
    try:
        import agent.transports.codex  # noqa: F401
    except ImportError:
        pass
    try:
        import agent.transports.chat_completions  # noqa: F401
    except ImportError:
        pass
    try:
        import agent.transports.bedrock  # noqa: F401
    except ImportError:
        pass
