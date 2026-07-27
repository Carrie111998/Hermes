"""Canonical direct-agent entry point."""

from __future__ import annotations

from charterforge.compat import install_legacy_environment_aliases


def main() -> None:
    install_legacy_environment_aliases()
    from run_agent import main as legacy_main

    legacy_main()
