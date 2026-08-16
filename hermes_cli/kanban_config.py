"""Small, fail-closed config coercions shared by Kanban entry points."""

from __future__ import annotations

from typing import Any


def enabled(value: Any) -> bool:
    """Return a strict enable flag without treating arbitrary strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        # Strings are legacy/configuration mistakes, not affirmative flags.
        # In particular, the familiar false spellings stay explicitly false;
        # every other string fails closed too.
        return False
    return False
