import os
from pathlib import Path

import pytest

from tui_gateway.private_storage import write_private_file_atomic_exclusive


def test_atomic_exclusive_private_publication_never_replaces_existing(tmp_path: Path):
    destination = tmp_path / "private" / "artifact.txt"
    previous_umask = os.umask(0)
    try:
        published = write_private_file_atomic_exclusive(destination, b"first")
        with pytest.raises(FileExistsError):
            write_private_file_atomic_exclusive(destination, b"second")
    finally:
        os.umask(previous_umask)

    assert published.read_bytes() == b"first"
    if os.name != "nt":
        assert published.parent.stat().st_mode & 0o777 == 0o700
        assert published.stat().st_mode & 0o777 == 0o600
    assert not list(published.parent.glob(".*.tmp"))


def test_atomic_exclusive_private_publication_rejects_symlink_destination(tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"sentinel")
    private = tmp_path / "private"
    private.mkdir()
    destination = private / "artifact.txt"
    try:
        destination.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(OSError, match="unsafe private artifact file"):
        write_private_file_atomic_exclusive(destination, b"replacement")

    assert outside.read_bytes() == b"sentinel"
