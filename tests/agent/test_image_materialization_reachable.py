"""Data-URL materialization must land somewhere the sandbox can read.

Observed failure (gateway.log, 2026-08-08), with ``terminal.backend: docker``::

    tools.image_source.SourceNotFound: '/var/folders/.../T/anthropic_image_x.jpg'
    is not reachable inside the sandbox and no active sandbox session is
    available to read it

``_materialize_data_url_for_vision`` wrote the decoded image to the host
``$TMPDIR``. Under a non-local terminal backend, ``vision_analyze`` resolves
paths *inside the container*, and ``$TMPDIR`` is not mounted there — so every
such analysis failed, the model got no description and no usable path, and it
fell back to asking the user for a URL or base64 blob.

The Hermes cache directory *is* auto-mounted and is translated by
``to_agent_visible_cache_path``, so materialization belongs there.
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import pytest

from run_agent import AIAgent


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _data_url(payload: bytes = PNG) -> str:
    return f"data:image/png;base64,{base64.b64encode(payload).decode('ascii')}"


class TestMaterializationLocation:
    def test_host_tempfile_api_is_not_used(self, monkeypatch, tmp_path):
        """The whole bug: $TMPDIR is invisible to the sandbox.

        Asserted by forbidding the temp-file API rather than by comparing path
        prefixes — pytest's own ``tmp_path`` lives under ``$TMPDIR``, so a
        prefix check cannot distinguish the two.
        """
        import run_agent as ra

        cache = tmp_path / "cache-images"
        monkeypatch.setattr(ra, "_sandbox_reachable_image_dir", lambda: cache)

        def _forbidden(*_args, **_kwargs):
            raise AssertionError("materialization must not use the host temp dir")

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", _forbidden)
        monkeypatch.setattr(tempfile, "mkstemp", _forbidden)

        path_str, path_obj = AIAgent._materialize_data_url_for_vision(_data_url())
        assert path_obj is not None
        try:
            assert Path(path_str).parent == cache
        finally:
            path_obj.unlink(missing_ok=True)

    def test_file_lands_in_the_reachable_image_dir(self, monkeypatch, tmp_path):
        import run_agent as ra

        cache = tmp_path / "cache-images"
        monkeypatch.setattr(ra, "_sandbox_reachable_image_dir", lambda: cache)

        path_str, path_obj = AIAgent._materialize_data_url_for_vision(_data_url())
        assert path_obj is not None
        try:
            assert path_obj.parent == cache
            assert path_obj.read_bytes() == PNG
            assert path_str == str(path_obj)
        finally:
            path_obj.unlink(missing_ok=True)

    def test_directory_is_created_when_absent(self, monkeypatch, tmp_path):
        import run_agent as ra

        cache = tmp_path / "not-yet" / "images"
        monkeypatch.setattr(ra, "_sandbox_reachable_image_dir", lambda: cache)

        _path_str, path_obj = AIAgent._materialize_data_url_for_vision(_data_url())
        assert path_obj is not None
        try:
            assert path_obj.exists()
        finally:
            path_obj.unlink(missing_ok=True)


class TestPreservedInvariants:
    def test_no_leak_on_decode_failure(self, monkeypatch, tmp_path):
        """A corrupt data URL must not leave a zero-byte file behind."""
        import run_agent as ra

        cache = tmp_path / "cache-images"
        monkeypatch.setattr(ra, "_sandbox_reachable_image_dir", lambda: cache)

        with pytest.raises(Exception):
            AIAgent._materialize_data_url_for_vision(
                "data:image/png;base64,!!!not-valid-base64!!!"
            )

        leftovers = list(cache.iterdir()) if cache.exists() else []
        assert leftovers == [], f"leaked files after decode failure: {leftovers}"

    def test_oversized_payload_is_refused(self, monkeypatch, tmp_path):
        import run_agent as ra

        cache = tmp_path / "cache-images"
        monkeypatch.setattr(ra, "_sandbox_reachable_image_dir", lambda: cache)

        oversized = "data:image/png;base64," + ("A" * (AIAgent._MAX_DATA_URL_BASE64_BYTES + 1))
        assert AIAgent._materialize_data_url_for_vision(oversized) == ("", None)


class TestSandboxReachableImageDir:
    def test_returns_a_path(self):
        from run_agent import _sandbox_reachable_image_dir as sandbox_reachable_image_dir

        assert isinstance(sandbox_reachable_image_dir(), Path)

    def test_result_translates_for_the_container(self, monkeypatch):
        """The chosen directory must be one the path translator recognises."""
        monkeypatch.setenv("TERMINAL_ENV", "docker")
        from run_agent import _sandbox_reachable_image_dir as sandbox_reachable_image_dir
        from tools.credential_files import to_agent_visible_cache_path

        sample = str(sandbox_reachable_image_dir() / "img_demo.jpg")
        assert to_agent_visible_cache_path(sample).startswith("/root/.hermes/")
