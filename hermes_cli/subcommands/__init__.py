"""Subcommand parser builders for the hermes CLI.

Extracted from ``hermes_cli/main.py`` so each subcommand's argparse
construction is introspectable and unit-testable without running ``main``.
Handlers are injected as callables to avoid importing ``main``.
"""
