"""Resolve ORION_HOME for standalone skill scripts.

Skill scripts may run outside the Orion process (system Python, nix env,
CI) where ``orion_constants`` is not importable.  This module provides the
same ``get_orion_home()`` contract without requiring it on ``sys.path``.

When ``orion_constants`` IS available it is used directly so profile
resolution and any future enhancements are picked up automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from orion_constants import get_orion_home as get_orion_home
except (ModuleNotFoundError, ImportError):

    def get_orion_home() -> Path:
        """Return the Orion home directory (default: ``~/.orion``)."""
        val = os.environ.get("ORION_HOME", "").strip()
        return Path(val) if val else Path.home() / ".orion"
