"""Symlink-capability probe for tests that must *create* a symlink.

Creating a symlink on Windows requires ``SeCreateSymbolicLinkPrivilege``,
which an ordinary process only holds when it is elevated or when Developer
Mode is enabled.  Without it ``Path.symlink_to()`` raises
``OSError: [WinError 1314] A required privilege is not held by the client``
while the test is still building its fixture — the code under test is never
reached.

Guard those tests with :data:`requires_symlinks` rather than a blanket
``os.name == "nt"`` skip.  Following and resolving symlinks works fine on
Windows (``os.path.islink`` / ``os.path.realpath`` are fully functional), so
invariants like "an atomic write must not detach a symlink" are just as real
there.  A platform skip would disable that coverage permanently, including on
Windows machines that *do* hold the privilege; a capability probe re-enables
it automatically the moment the privilege is present.

Usage::

    from tests.symlink_support import requires_symlinks

    @requires_symlinks
    def test_something_with_a_symlink(tmp_path):
        ...
"""
from __future__ import annotations

import functools
import tempfile
from pathlib import Path

import pytest


@functools.lru_cache(maxsize=1)
def symlinks_supported() -> bool:
    """Return True if this process can actually create a symlink.

    Probes by creating real symlinks in a temporary directory, because the
    answer depends on runtime privilege rather than on anything statically
    knowable — the same Windows build and Python answer differently depending
    on elevation and Developer Mode.

    Probes a *directory* symlink as well as a file one: Windows makes them
    distinct reparse types (hence ``target_is_directory``), and guarded tests
    here create both kinds.  A probe that only covered files would under-
    approximate what its consumers need.

    Fails closed: any error, or a link that does not report itself as one, is
    reported as "unsupported".  A false negative only skips a test; a false
    positive would surface as a confusing fixture-time crash.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-symlink-probe-") as td:
            probe_dir = Path(td)

            target_file = probe_dir / "target"
            target_file.write_text("probe", encoding="utf-8")
            file_link = probe_dir / "file-link"
            file_link.symlink_to(target_file)

            target_dir = probe_dir / "target-dir"
            target_dir.mkdir()
            dir_link = probe_dir / "dir-link"
            dir_link.symlink_to(target_dir, target_is_directory=True)

            return file_link.is_symlink() and dir_link.is_symlink()
    except (OSError, NotImplementedError, AttributeError):
        return False


requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(),
    reason=(
        "cannot create symlinks in this process — on Windows this needs "
        "SeCreateSymbolicLinkPrivilege (enable Developer Mode or run elevated)"
    ),
)
