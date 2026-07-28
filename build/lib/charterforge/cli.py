"""Canonical Charterforge CLI entry point."""

from __future__ import annotations

from charterforge.compat import install_legacy_environment_aliases


def main() -> None:
    """Run Charterforge through the proven CLI implementation."""
    install_legacy_environment_aliases()
    from hermes_cli.main import main as legacy_main

    legacy_main()
