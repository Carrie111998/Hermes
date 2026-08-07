"""
Tests for document cache utilities in gateway/platforms/base.py.

Covers: get_document_cache_dir, cache_document_from_bytes,
        cleanup_document_cache, SUPPORTED_DOCUMENT_TYPES.
"""

import os
import time
from pathlib import Path

import pytest

from gateway.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    cache_document_from_bytes,
    cleanup_document_cache,
    get_document_cache_dir,
)

# ---------------------------------------------------------------------------
# Fixture: redirect DOCUMENT_CACHE_DIR to a temp directory for every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Point cache directories to a fresh tmp_path."""
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )
    monkeypatch.setattr(
        "gateway.platforms.base.IMAGE_CACHE_DIR", tmp_path / "image_cache"
    )
    monkeypatch.setattr(
        "gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path / "audio_cache"
    )


# ---------------------------------------------------------------------------
# TestGetDocumentCacheDir
# ---------------------------------------------------------------------------

class TestGetDocumentCacheDir:
    def test_creates_directory(self, tmp_path):
        cache_dir = get_document_cache_dir()
        assert cache_dir.exists()
        assert cache_dir.is_dir()


# ---------------------------------------------------------------------------
# TestCacheDocumentFromBytes
# ---------------------------------------------------------------------------

class TestCacheDocumentFromBytes:
    def test_basic_caching(self):
        data = b"hello world"
        path = cache_document_from_bytes(data, "test.txt")
        assert os.path.exists(path)
        assert Path(path).read_bytes() == data

    def test_filename_preserved_in_path(self):
        path = cache_document_from_bytes(b"data", "report.pdf")
        assert "report.pdf" in os.path.basename(path)

    def test_empty_filename_uses_fallback(self):
        path = cache_document_from_bytes(b"data", "")
        assert "document" in os.path.basename(path)


# ---------------------------------------------------------------------------
# TestCleanupDocumentCache
# ---------------------------------------------------------------------------

class TestCleanupDocumentCache:
    def test_removes_old_files(self, tmp_path):
        cache_dir = get_document_cache_dir()
        old_file = cache_dir / "old.txt"
        old_file.write_text("old")
        # Set modification time to 48 hours ago
        old_mtime = time.time() - 48 * 3600
        os.utime(old_file, (old_mtime, old_mtime))

        removed = cleanup_document_cache(max_age_hours=24)
        assert removed == 1
        assert not old_file.exists()


# ---------------------------------------------------------------------------
# TestSupportedDocumentTypes
# ---------------------------------------------------------------------------

class TestSupportedDocumentTypes:
    def test_all_extensions_have_mime_types(self):
        for ext, mime in SUPPORTED_DOCUMENT_TYPES.items():
            assert ext.startswith("."), f"{ext} missing leading dot"
            assert "/" in mime, f"{mime} is not a valid MIME type"


# ---------------------------------------------------------------------------
# TestCacheMediaBytes — the unified, platform-agnostic caching primitive
# ---------------------------------------------------------------------------

# 1x1 transparent PNG (passes cache_image_from_bytes validation)
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)
_HEIC_STUB = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32
_JPEG_STUB = b"\xff\xd8\xff\xe0" + b"converted-jpeg" + b"\xff\xd9"


class TestCacheMediaBytes:
    def test_pdf_routes_to_document(self):
        from gateway.platforms.base import cache_media_bytes
        result = cache_media_bytes(b"%PDF-1.4 body", filename="report.pdf", mime_type="application/pdf")
        assert result is not None
        assert result.kind == "document"
        assert result.media_type == "application/pdf"
        assert "report.pdf" in result.display_name
        assert os.path.exists(result.path)
        assert "report.pdf" in result.context_note()

    def test_png_routes_to_image(self):
        from gateway.platforms.base import cache_media_bytes
        result = cache_media_bytes(_PNG_1PX, filename="photo.png", mime_type="image/png")
        assert result is not None
        assert result.kind == "image"
        assert result.media_type == "image/png"
        assert os.path.exists(result.path)

    def test_heic_routes_to_converted_jpeg_image(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(
            base,
            "_transcode_heic_to_jpeg",
            lambda data: _JPEG_STUB if data == _HEIC_STUB else None,
        )

        result = base.cache_media_bytes(
            _HEIC_STUB,
            filename="IMG_4127.HEIC",
            mime_type="image/heic",
            transcode_heic=True,
        )

        assert result is not None
        assert result.kind == "image"
        assert result.media_type == "image/jpeg"
        assert result.path.endswith(".jpg")
        assert os.path.exists(result.path)
        with open(result.path, "rb") as fh:
            assert fh.read() == _JPEG_STUB

    def test_heic_transcoder_prefers_pillow_heif(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(
            base,
            "_transcode_heic_to_jpeg_with_pillow",
            lambda data: _JPEG_STUB if data == _HEIC_STUB else None,
        )

        def fail_if_command_used(*args, **kwargs):
            raise AssertionError("command fallback should not run after Pillow succeeds")

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg_with_command", fail_if_command_used)

        assert base._transcode_heic_to_jpeg(_HEIC_STUB) == _JPEG_STUB

    def test_heic_transcoder_ensures_lazy_decoder(self, monkeypatch):
        import tools.lazy_deps as lazy_deps
        import gateway.platforms.base as base

        calls = []
        monkeypatch.setattr(
            lazy_deps,
            "ensure",
            lambda feature, *, prompt: calls.append((feature, prompt)),
        )

        base._transcode_heic_to_jpeg_with_pillow(_HEIC_STUB)

        assert calls == [("media.heic", False)]

    def test_heic_lazy_decoder_failure_falls_back_to_document(self, monkeypatch):
        import tools.lazy_deps as lazy_deps
        import gateway.platforms.base as base

        def unavailable(feature, *, prompt):
            raise lazy_deps.FeatureUnavailable(
                feature,
                ("pillow-heif==1.4.0",),
                "disabled for test",
            )

        monkeypatch.setattr(lazy_deps, "ensure", unavailable)
        monkeypatch.setattr(base.sys, "platform", "linux")
        monkeypatch.setattr(base.shutil, "which", lambda name: None)

        result = base.cache_media_bytes(
            _HEIC_STUB,
            filename="IMG_4127.HEIC",
            mime_type="image/heic",
            transcode_heic=True,
        )

        assert result is not None
        assert result.kind == "document"
        assert result.media_type == "application/octet-stream"
        assert Path(result.path).read_bytes() == _HEIC_STUB

    def test_heic_transcoder_falls_back_to_sips_on_macos(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg_with_pillow", lambda data: None)
        monkeypatch.setattr(base.sys, "platform", "darwin")
        monkeypatch.setattr(base.shutil, "which", lambda name: "/usr/bin/sips" if name == "sips" else None)

        def fake_command(data, command_builder):
            command = command_builder("input.heic", "output.jpg")
            assert command[:4] == ["/usr/bin/sips", "-s", "format", "jpeg"]
            return _JPEG_STUB

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg_with_command", fake_command)

        assert base._transcode_heic_to_jpeg(_HEIC_STUB) == _JPEG_STUB

    def test_heic_transcoder_does_not_use_ffmpeg_fallback(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg_with_pillow", lambda data: None)
        monkeypatch.setattr(base.sys, "platform", "linux")
        monkeypatch.setattr(
            base.shutil,
            "which",
            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None,
        )

        def fail_if_command_used(*args, **kwargs):
            raise AssertionError("ffmpeg must not be used for HEIC fallback")

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg_with_command", fail_if_command_used)

        assert base._transcode_heic_to_jpeg(_HEIC_STUB) is None

    def test_unconverted_heic_falls_back_to_document(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg", lambda data: None)

        result = base.cache_media_bytes(
            _HEIC_STUB,
            filename="IMG_4127.HEIC",
            mime_type="image/heic",
            transcode_heic=True,
        )

        assert result is not None
        assert result.kind == "document"
        assert result.media_type == "application/octet-stream"
        assert os.path.exists(result.path)
        with open(result.path, "rb") as fh:
            assert fh.read() == _HEIC_STUB

    def test_malformed_heic_conversion_falls_back_to_document(self, monkeypatch):
        import gateway.platforms.base as base

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg", lambda data: b"not-jpeg")

        result = base.cache_media_bytes(
            _HEIC_STUB,
            filename="IMG_4127.HEIC",
            mime_type="image/heic",
            transcode_heic=True,
        )

        assert result is not None
        assert result.kind == "document"
        assert result.media_type == "application/octet-stream"
        assert os.path.exists(result.path)
        with open(result.path, "rb") as fh:
            assert fh.read() == _HEIC_STUB

    def test_heic_without_transcode_opt_in_falls_back_to_document(self, monkeypatch):
        import gateway.platforms.base as base

        def fail_if_called(data):
            raise AssertionError("HEIC transcode should be opt-in")

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg", fail_if_called)

        result = base.cache_media_bytes(
            _HEIC_STUB,
            filename="IMG_4127.HEIC",
            mime_type="image/heic",
        )

        assert result is not None
        assert result.kind == "document"
        assert result.media_type == "application/octet-stream"

    def test_oversized_heic_rejected_before_transcode(self, monkeypatch):
        import gateway.platforms.base as base

        def fail_if_called(data):
            raise AssertionError("Oversized HEIC should not reach transcoder")

        monkeypatch.setattr(base, "_transcode_heic_to_jpeg", fail_if_called)
        monkeypatch.setattr(base, "get_inbound_media_max_bytes", lambda: 8)

        with pytest.raises(ValueError, match="too large"):
            base.cache_media_bytes(
                _HEIC_STUB,
                filename="IMG_4127.HEIC",
                mime_type="image/heic",
                transcode_heic=True,
            )

    def test_unknown_document_cached_as_octet_stream(self):
        """Unknown file types are cached (not dropped) so the agent can inspect them.

        Authorization to message the agent is the gate, not the file extension.
        """
        from gateway.platforms.base import cache_media_bytes
        result = cache_media_bytes(b"MZ", filename="program.exe", mime_type="application/x-msdownload")
        assert result is not None
        assert result.kind == "document"
        # Caller-supplied MIME is preserved when present.
        assert result.media_type == "application/x-msdownload"
        assert os.path.exists(result.path)


