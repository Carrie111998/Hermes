"""Regression tests for optional secret-source import boundaries."""

from __future__ import annotations

import importlib
import importlib.abc
import sys


class _BlockingFinder(importlib.abc.MetaPathFinder):
    def __init__(self, blocked_prefix: str):
        self.blocked_prefix = blocked_prefix

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.blocked_prefix or fullname.startswith(
            f"{self.blocked_prefix}."
        ):
            raise ImportError(f"blocked import: {fullname}")
        return None


def _drop_modules(monkeypatch, *prefixes: str) -> None:
    for name in list(sys.modules):
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes):
            monkeypatch.delitem(sys.modules, name, raising=False)


def test_bitwarden_import_does_not_require_cryptography(monkeypatch):
    """A broken optional cryptography wheel must not break source registration."""
    _drop_modules(monkeypatch, "agent.secret_sources.bitwarden", "cryptography")

    import agent.secret_sources as package

    monkeypatch.delattr(package, "bitwarden", raising=False)
    monkeypatch.setattr(
        sys,
        "meta_path",
        [_BlockingFinder("cryptography"), *sys.meta_path],
    )

    module = importlib.import_module("agent.secret_sources.bitwarden")

    assert module.BitwardenSource().name == "bitwarden"


def test_command_source_registers_when_bitwarden_import_fails(monkeypatch):
    """The command source is independent of the Bitwarden implementation."""
    _drop_modules(
        monkeypatch,
        "agent.secret_sources.bitwarden",
        "agent.secret_sources.command",
    )

    from agent.secret_sources import registry

    registry._reset_registry_for_tests()
    monkeypatch.setattr(
        sys,
        "meta_path",
        [_BlockingFinder("agent.secret_sources.bitwarden"), *sys.meta_path],
    )

    names = {source.name for source in registry.list_sources()}

    assert "command" in names
    assert "bitwarden" not in names
