"""RecursiveIntell agent-graph integration for Hermes.

Provides in-process AgentState backed by the Rust agent-graph crate
via PyO3, avoiding MCP serialization overhead for graph state operations.

Usage::

    from agent.transports.ri_agent_graph import RiAgentState

    state = RiAgentState({"key": "value"})
    state.set("counter", 42)
    print(state.get("counter"))
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_NATIVE_AVAILABLE = False
try:
    from agent_graph._native import AgentState as _NativeState

    _NATIVE_AVAILABLE = True
except ImportError:
    logger.debug("agent-graph native extension not available")


class RiAgentState:
    """In-process agent graph state backed by Rust."""

    def __init__(self, initial: dict[str, Any] | None = None):
        self._native: _NativeState | None = None
        if _NATIVE_AVAILABLE:
            self._native = _NativeState(initial)
        self._fallback: dict[str, Any] = dict(initial) if initial else {}

    @property
    def available(self) -> bool:
        return self._native is not None

    def get(self, key: str) -> Any:
        if self._native is not None:
            return self._native.get(key)
        return self._fallback.get(key)

    def set(self, key: str, value: Any) -> None:
        if self._native is not None:
            self._native.set(key, value)
        else:
            self._fallback[key] = value

    def as_dict(self) -> dict[str, Any]:
        if self._native is not None:
            return self._native.as_dict()
        return dict(self._fallback)

    def __repr__(self) -> str:
        status = "native" if self.available else "fallback"
        size = len(self.as_dict())
        return f"RiAgentState({size} keys, {status})"
