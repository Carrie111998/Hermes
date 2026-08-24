"""Adapter registry. Each external benchmark (OSWorld 2.0, Tau²-Bench, etc.)
lives in its own module here and exposes an adapter class implementing the
BenchmarkAdapter protocol from `evals.harness.harness`.

Currently registered: NONE (placeholders only).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..harness import BenchmarkAdapter


_REGISTRY: dict[str, type] = {}


def register(name: str, adapter_cls: type) -> None:
    """Register an adapter class. Called by adapter modules on import."""
    _REGISTRY[name] = adapter_cls


def get_adapter(name: str) -> "BenchmarkAdapter | None":
    """Look up an adapter by name. Returns None if not registered."""
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    return cls()


def known_adapters() -> list[str]:
    """List all registered adapter names."""
    return sorted(_REGISTRY.keys())


__all__ = ["register", "get_adapter", "known_adapters"]
