"""Regression tests: MCP MIME-type field naming across SDK generations.

Background
==========
``mcp`` < 2.0.0 named the Python attribute after the wire field
(``mimeType``). ``mcp`` >= 2.0.0 renamed every such field to snake_case
(``mime_type``) and kept ``mimeType`` as a *serialization alias only*, so
``getattr(block, "mimeType", None)`` yields ``None`` on the new SDK even
when the block carries a MIME type.

Every read site in ``tools/mcp_tool.py`` guarded its camelCase access with
``getattr``/``hasattr``, so on mcp >= 2.0.0 these paths degraded silently
instead of raising:

* ``_cache_mcp_image_block`` saw an empty MIME, failed its
  ``startswith("image/")`` check and dropped screenshots on the floor;
* ``_cache_mcp_audio_block`` did the same for audio blocks;
* ``_render_mcp_resource_block`` omitted the type from resource links and
  from the embedded-resource cache path (which uses it to pick a filename
  extension);
* the sampling bridge fell through to "Unsupported sampling content block
  type" and skipped the image entirely;
* ``list_resources`` returned entries with no ``mimeType``.

These tests feed each helper a snake_case-only object (what the real mcp
2.x models expose to attribute access) and assert the MIME type is still
honoured. They also keep a camelCase-only case so a future cleanup can't
silently drop mcp 1.x support.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace


def _png_bytes():
    """Minimal valid 1x1 PNG — cache_image_from_bytes sniffs the format."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


class TestMcpMimeTypeHelper:
    def test_reads_snake_case_new_sdk(self):
        from tools.mcp_tool import _mcp_mime_type
        assert _mcp_mime_type(SimpleNamespace(mime_type="image/png")) == "image/png"

    def test_reads_camel_case_old_sdk(self):
        from tools.mcp_tool import _mcp_mime_type
        assert _mcp_mime_type(SimpleNamespace(mimeType="image/png")) == "image/png"

    def test_snake_case_wins_when_both_present(self):
        """mcp 2.x pydantic models expose both names in some code paths;
        the snake_case field is the authoritative one."""
        from tools.mcp_tool import _mcp_mime_type
        obj = SimpleNamespace(mime_type="audio/ogg", mimeType=None)
        assert _mcp_mime_type(obj) == "audio/ogg"

    def test_missing_mime_is_empty_string_not_none(self):
        """Callers concatenate the result into f-strings and call
        ``.split()`` on it, so the helper must never return ``None``."""
        from tools.mcp_tool import _mcp_mime_type
        assert _mcp_mime_type(SimpleNamespace()) == ""
        assert _mcp_mime_type(SimpleNamespace(mime_type=None)) == ""


class TestImageBlockSnakeCase:
    def test_snake_case_image_block_still_cached(self, tmp_path, monkeypatch):
        """The mcp 2.x ImageContent shape must still yield a MEDIA tag."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from tools.mcp_tool import _cache_mcp_image_block

        block = SimpleNamespace(
            data=base64.b64encode(_png_bytes()).decode("ascii"),
            mime_type="image/png",
            type="image",
        )
        result = _cache_mcp_image_block(block)
        assert result.startswith("MEDIA:"), result
        assert result.endswith(".png"), result


class TestAudioBlockSnakeCase:
    def test_snake_case_audio_block_recognized(self, tmp_path, monkeypatch):
        """An mcp 2.x AudioContent block must not be mistaken for a
        non-audio block and dropped."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        import tools.mcp_tool as mcp_tool

        captured = {}

        def fake_cache_audio_from_bytes(raw_bytes, ext=".ogg"):
            captured["ext"] = ext
            path = tmp_path / f"audio{ext}"
            path.write_bytes(raw_bytes)
            return str(path)

        import gateway.platforms.base as gwbase
        monkeypatch.setattr(
            gwbase, "cache_audio_from_bytes", fake_cache_audio_from_bytes,
            raising=False,
        )

        block = SimpleNamespace(
            data=base64.b64encode(b"RIFFfake-wav-payload").decode("ascii"),
            mime_type="audio/wav",
            type="audio",
        )
        result = mcp_tool._cache_mcp_audio_block(block)
        assert result.startswith("MEDIA:"), result
        assert captured.get("ext") == ".wav", captured


class TestResourceBlockSnakeCase:
    def test_resource_link_reports_snake_case_mime(self):
        """A resource link on mcp 2.x must still advertise its type so the
        agent can decide whether fetching it is worthwhile."""
        from tools.mcp_tool import _render_mcp_resource_block

        block = SimpleNamespace(
            type="resource_link",
            uri="file:///tmp/report.pdf",
            name="report.pdf",
            mime_type="application/pdf",
        )
        rendered = _render_mcp_resource_block(block, "docserver")
        assert "mimeType=application/pdf" in rendered, rendered
