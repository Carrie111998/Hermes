"""Canonical Agent Client Protocol entry point."""

from __future__ import annotations

from charterforge.compat import install_legacy_environment_aliases


def main() -> None:
    install_legacy_environment_aliases()
    from acp_adapter.entry import main as legacy_main

    legacy_main()
