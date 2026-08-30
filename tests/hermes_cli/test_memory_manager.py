"""Tests for hermes_cli/memory_manager.py — memory manager helpers."""


def test_resolve_memory_provider_returns_string():
    from hermes_cli.memory_manager import _resolve_memory_provider
    result = _resolve_memory_provider()
    assert isinstance(result, str) or result is None
