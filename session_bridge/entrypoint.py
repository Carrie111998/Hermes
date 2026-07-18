"""Thin console bootstrap for installer isolation and lazy CLI composition."""

from __future__ import annotations

from collections.abc import Sequence
import json
import sys


_INSTALL_COMMANDS = frozenset({"install-sidebar-skill", "install-claude-skill"})


def main(argv: Sequence[str] | None = None) -> int:
    """Install directly for the exact installer command; lazily load all else."""

    selected = list(sys.argv[1:] if argv is None else argv)
    if len(selected) == 1 and selected[0] in _INSTALL_COMMANDS:
        if selected[0] == "install-sidebar-skill":
            from .sidebar_skill import install_sidebar_skill as installer
        else:
            from .claude_skill import install_claude_skill as installer

        try:
            installed = installer()
        except Exception:
            _emit({"error": "configuration_error"})
            return 2
        _emit({"status": "installed", "path": str(installed)})
        return 0

    from .cli import main as cli_main

    return cli_main(selected)


def _emit(payload: dict[str, str]) -> None:
    print(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
