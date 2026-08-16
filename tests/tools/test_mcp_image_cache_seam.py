"""Seam tests for the mcp_tool R1 extraction of the MCP image-block helpers.

The two image helpers moved byte-verbatim from ``tools/mcp_tool.py`` into the
new ``tools/mcp_image_cache.py`` module; ``tools/mcp_tool.py`` now re-exports
them so existing callers and module-global call sites resolve the exact same
objects. These tests lock the seam down: object identity in both directions,
canonical ownership, logger-name preservation, old-owner monkeypatch
authority, and behavioral equivalence through both import paths.
"""

import base64
import inspect
from types import SimpleNamespace

import pytest

from tools import mcp_image_cache
from tools import mcp_tool

MOVED_NAMES = ("_mcp_image_extension_for_mime_type", "_cache_mcp_image_block")


def _png_bytes():
    """Minimal valid 1x1 transparent PNG (real signature)."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


class TestObjectIdentitySeam:
    def test_reexports_are_the_same_objects(self):
        for name in MOVED_NAMES:
            assert getattr(mcp_tool, name) is getattr(mcp_image_cache, name), name

    def test_canonical_definitions_live_in_mcp_image_cache(self):
        for name in MOVED_NAMES:
            fn = getattr(mcp_tool, name)
            assert fn.__module__ == "tools.mcp_image_cache", name
            source_file = inspect.getsourcefile(fn).replace("\\", "/")
            assert source_file.endswith("tools/mcp_image_cache.py"), source_file


class TestLoggerBinding:
    def test_logger_name_is_preserved(self):
        assert mcp_image_cache.logger.name == "tools.mcp_tool"
        assert mcp_image_cache.logger is mcp_tool.logger


class TestMonkeypatchAuthority:
    def test_patching_old_owner_shadows_reexport_but_not_canonical(self, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(mcp_tool, "_cache_mcp_image_block", sentinel)
        assert mcp_tool._cache_mcp_image_block is sentinel
        assert mcp_image_cache._cache_mcp_image_block is not sentinel
        assert callable(mcp_image_cache._cache_mcp_image_block)


class TestBehavioralEquivalence:
    def test_extension_cases_match_through_both_paths(self):
        cases = [
            "image/jpeg",
            "image/jpg",
            "IMAGE/JPEG",
            "image/jpeg; charset=utf-8",
            "image/png",
            "",
            "image/unheard-of-format",
        ]
        for mime in cases:
            via_old = mcp_tool._mcp_image_extension_for_mime_type(mime)
            via_new = mcp_image_cache._mcp_image_extension_for_mime_type(mime)
            assert via_old == via_new, mime
        assert mcp_tool._mcp_image_extension_for_mime_type("image/jpeg") == ".jpg"
        assert mcp_tool._mcp_image_extension_for_mime_type("") == ".png"

    def test_non_image_block_returns_empty_through_both_paths(self):
        block = SimpleNamespace(
            data=base64.b64encode(b"x").decode("ascii"), mimeType="application/pdf"
        )
        assert mcp_tool._cache_mcp_image_block(block) == ""
        assert mcp_image_cache._cache_mcp_image_block(block) == ""

    def test_missing_data_returns_empty(self):
        block = SimpleNamespace(data=None, mimeType="image/png")
        assert mcp_tool._cache_mcp_image_block(block) == ""
        assert mcp_image_cache._cache_mcp_image_block(block) == ""

    def test_malformed_base64_returns_empty(self):
        block = SimpleNamespace(data="!!!not-base64!!!", mimeType="image/png")
        assert mcp_tool._cache_mcp_image_block(block) == ""
        assert mcp_image_cache._cache_mcp_image_block(block) == ""

    def test_valid_png_uses_cache_helper_through_reexport(self, monkeypatch):
        import gateway.platforms.base as base

        captured = {}

        def fake_cache(raw_bytes, ext=None):
            captured["ext"] = ext
            return "/fake/cache/pic" + ext

        monkeypatch.setattr(base, "cache_image_from_bytes", fake_cache)
        block = SimpleNamespace(
            data=base64.b64encode(_png_bytes()).decode("ascii"),
            mimeType="image/png",
        )
        assert mcp_tool._cache_mcp_image_block(block) == "MEDIA:/fake/cache/pic.png"
        assert captured["ext"] == ".png"
        # canonical owner behaves identically through its own module
        assert mcp_image_cache._cache_mcp_image_block(block) == "MEDIA:/fake/cache/pic.png"

    def test_jpeg_passes_jpg_extension(self, monkeypatch):
        import gateway.platforms.base as base

        captured = {}

        def fake_cache(raw_bytes, ext=None):
            captured["ext"] = ext
            return "/fake/cache/pic" + ext

        monkeypatch.setattr(base, "cache_image_from_bytes", fake_cache)
        block = SimpleNamespace(
            data=base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 100).decode("ascii"),
            mimeType="image/jpeg",
        )
        tag = mcp_tool._cache_mcp_image_block(block)
        assert tag == "MEDIA:/fake/cache/pic.jpg"
        assert captured["ext"] == ".jpg"

    def test_cache_helper_exception_returns_empty(self, monkeypatch):
        import gateway.platforms.base as base

        def boom(*args, **kwargs):
            raise RuntimeError("cache full")

        monkeypatch.setattr(base, "cache_image_from_bytes", boom)
        block = SimpleNamespace(
            data=base64.b64encode(_png_bytes()).decode("ascii"),
            mimeType="image/png",
        )
        assert mcp_tool._cache_mcp_image_block(block) == ""
        assert mcp_image_cache._cache_mcp_image_block(block) == ""

    def test_import_error_fallback_returns_empty(self, monkeypatch):
        """gateway.platforms.base unavailable -> silently drop (debug log)."""
        import sys
        import types

        fake_base = types.ModuleType("gateway.platforms.base")  # no cache_image_from_bytes
        monkeypatch.setitem(sys.modules, "gateway.platforms.base", fake_base)
        block = SimpleNamespace(
            data=base64.b64encode(_png_bytes()).decode("ascii"),
            mimeType="image/png",
        )
        assert mcp_tool._cache_mcp_image_block(block) == ""
        assert mcp_image_cache._cache_mcp_image_block(block) == ""
