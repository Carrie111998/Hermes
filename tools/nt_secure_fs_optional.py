"""Optional boundary for the native Windows skill-filesystem backend.

The hardened NT implementation may be absent from source distributions that
do not ship native Windows support.  Skill discovery and mutation must fail
closed with an actionable ``OSError`` in that case, not crash module loading
with ``ModuleNotFoundError``.
"""

from __future__ import annotations

try:
    from tools.nt_secure_fs import (
        copy_tree_no_reparse,
        delete_tree,
        is_available,
        open_directory,
        read_regular_file,
        replace_regular_file,
    )
except ModuleNotFoundError as exc:
    if exc.name != "tools.nt_secure_fs":
        raise

    class NtSecureFsUnavailable(OSError):
        """The optional native Windows backend is not installed."""

    def is_available() -> bool:
        return False

    def _unavailable(*_args, **_kwargs):
        raise NtSecureFsUnavailable(
            "secure Windows skill filesystem backend is not installed"
        )

    open_directory = _unavailable
    read_regular_file = _unavailable
    replace_regular_file = _unavailable
    delete_tree = _unavailable
    copy_tree_no_reparse = _unavailable


__all__ = [
    "copy_tree_no_reparse",
    "delete_tree",
    "is_available",
    "open_directory",
    "read_regular_file",
    "replace_regular_file",
]
