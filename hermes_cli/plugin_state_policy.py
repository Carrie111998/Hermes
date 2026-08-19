"""Filesystem protocol for profile-scoped plugin state clone policy."""

from __future__ import annotations

import os
import stat
from pathlib import Path


IDENTITY_BOUND_STATE_MARKER = ".hermes-identity-bound-state"


def namespace_declares_identity_bound_state(data_dir: Path) -> bool:
    """Return whether a real plugin-data directory has the durable marker.

    Any filesystem uncertainty fails open: clone callers must preserve the
    namespace unless the declaration can be proven without following links.
    """
    try:
        namespace_stat = os.stat(data_dir, follow_symlinks=False)
        if not stat.S_ISDIR(namespace_stat.st_mode):
            return False
        marker_stat = os.stat(
            data_dir / IDENTITY_BOUND_STATE_MARKER,
            follow_symlinks=False,
        )
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(marker_stat.st_mode)
