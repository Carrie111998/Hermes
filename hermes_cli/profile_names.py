"""Canonical, stdlib-only profile identifier validation."""

from __future__ import annotations

import re

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RESERVED_PROFILE_NAMES = frozenset(
    {"hermes", "default", "test", "tmp", "root", "sudo"}
)


def is_valid_profile_name(name: str) -> bool:
    """Return whether *name* is a canonical on-disk profile identifier."""
    if name == "default":
        return True
    return bool(PROFILE_ID_RE.fullmatch(name)) and name not in RESERVED_PROFILE_NAMES


def validate_profile_name(name: str) -> None:
    """Raise ``ValueError`` unless *name* is a canonical profile identifier."""
    if name == "default":
        return
    if not PROFILE_ID_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. Must match "
            f"[a-z0-9][a-z0-9_-]{{0,63}}"
        )
    if name in RESERVED_PROFILE_NAMES:
        raise ValueError(
            f"Profile name {name!r} is reserved — it collides with either "
            "the Hermes installation itself or a common system binary.  "
            "Pick a different name."
        )
