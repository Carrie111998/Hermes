"""Resolve ORION_HOME for standalone skill scripts.

Skill scripts may run outside the Orion process (e.g. system Python,
nix env, CI) where ``orion_constants`` is not importable.  This module
provides the same ``get_orion_home()`` and ``display_orion_home()``
contracts as ``orion_constants`` without requiring it on ``sys.path``.

When ``orion_constants`` IS available it is used directly so that any
future enhancements (profile resolution, Docker detection, etc.) are
picked up automatically.  The fallback path replicates the core logic
from ``orion_constants.py`` using only the stdlib.

All scripts under ``google-workspace/scripts/`` should import from here
instead of duplicating the ``ORION_HOME = Path(os.getenv(...))`` pattern.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from orion_constants import display_orion_home as display_orion_home
    from orion_constants import get_orion_home as get_orion_home
except (ModuleNotFoundError, ImportError):

    def get_orion_home() -> Path:
        """Return the Orion home directory (default: ~/.orion).

        Mirrors ``orion_constants.get_orion_home()``."""
        val = os.environ.get("ORION_HOME", "").strip()
        return Path(val) if val else Path.home() / ".orion"

    def display_orion_home() -> str:
        """Return a user-friendly ``~/``-shortened display string.

        Mirrors ``orion_constants.display_orion_home()``."""
        home = get_orion_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)
