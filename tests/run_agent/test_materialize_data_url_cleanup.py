"""Regression test: file cleanup when materializing data URLs for vision.

`_materialize_data_url_for_vision` writes the decoded image to a path that can
be handed to vision backends.  If `base64.b64decode` raises on a corrupt or
unsupported data URL, that file would otherwise persist forever on disk,
leaking once per failed call.

The destination is the sandbox-reachable image cache, not the host `$TMPDIR` —
see tests/agent/test_image_materialization_reachable.py for why.  These tests
patch that directory so the leak invariant is still checked where files
actually land; patching `tempfile.tempdir` would silently assert nothing.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from run_agent import AIAgent


def _patch_image_dir(monkeypatch, target: Path) -> None:
    import run_agent as ra

    monkeypatch.setattr(ra, "_sandbox_reachable_image_dir", lambda: target)


def _list_materialized(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return [p.name for p in directory.iterdir()]


def test_b64decode_failure_does_not_leak_file(monkeypatch, tmp_path):
    image_dir = tmp_path / "cache-images"
    _patch_image_dir(monkeypatch, image_dir)

    bad_url = "data:image/png;base64,!!!not-valid-base64!!!"
    with pytest.raises(Exception):
        AIAgent._materialize_data_url_for_vision(bad_url)

    leftovers = _list_materialized(image_dir)
    assert leftovers == [], f"leaked files after decode failure: {leftovers}"


def test_successful_decode_returns_path_to_existing_file(monkeypatch, tmp_path):
    image_dir = tmp_path / "cache-images"
    _patch_image_dir(monkeypatch, image_dir)

    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16  # a few bytes is enough
    encoded = base64.b64encode(payload).decode("ascii")
    good_url = f"data:image/png;base64,{encoded}"

    path_str, path_obj = AIAgent._materialize_data_url_for_vision(good_url)

    assert isinstance(path_obj, Path)
    assert path_obj.exists()
    assert path_obj.parent == image_dir
    assert path_obj.read_bytes() == payload
    assert path_str == str(path_obj)
    # Caller is responsible for cleanup; mimic that here so the test leaves
    # no artifacts behind.
    path_obj.unlink()
