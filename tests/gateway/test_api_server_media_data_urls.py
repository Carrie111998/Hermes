"""MEDIA: tag → base64 data-URL resolution for the API server (salvage of #2696).

Remote OpenAI-compatible frontends can't read local file paths, so
``MEDIA:<path>`` image tags in final responses are inlined as markdown
data URLs before crossing the HTTP boundary.
"""

import base64
import unittest

import pytest

pytest.importorskip("aiohttp")

from gateway.platforms.api_server import (  # noqa: E402
    _resolve_media_to_data_urls,
    _StreamingMediaResolver,
)

# 1x1 transparent PNG
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg=="
)


class TestResolveMediaToDataUrls(unittest.TestCase):
    def _write_png(self, tmpdir_name="hermes_media_test"):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix=tmpdir_name))
        p = d / "shot.png"
        p.write_bytes(_PNG_BYTES)
        return p

    def test_media_tag_inlined(self):
        p = self._write_png()
        out = _resolve_media_to_data_urls(f"Here you go: MEDIA:{p}")
        self.assertIn("data:image/png;base64,", out)
        self.assertNotIn("MEDIA:", out)

    def test_backtick_wrapped_tag(self):
        p = self._write_png()
        out = _resolve_media_to_data_urls(f"See `MEDIA:{p}` above")
        self.assertIn("data:image/png;base64,", out)

    def test_missing_file_left_untouched(self):
        text = "MEDIA:/nonexistent/path/shot.png"
        self.assertEqual(_resolve_media_to_data_urls(text), text)

    def test_non_image_left_untouched(self):
        text = "MEDIA:/tmp/archive.zip"
        self.assertEqual(_resolve_media_to_data_urls(text), text)


class TestStreamingMediaResolver(unittest.TestCase):
    """``/v1/chat/completions`` with ``stream: true`` forwards the model's
    raw text deltas as they're generated, bypassing the non-streaming
    path's post-hoc ``_resolve_media_to_data_urls`` call entirely — a
    ``MEDIA:<path>`` tag reached streaming clients as literal, unrendered
    text. ``_StreamingMediaResolver`` buffers deltas so a tag split across
    chunk boundaries (the normal case under token-by-token streaming)
    still resolves before reaching the client.
    """

    def _write_png(self, tmpdir_name="hermes_media_stream_test"):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp(prefix=tmpdir_name))
        p = d / "shot.png"
        p.write_bytes(_PNG_BYTES)
        return p

    def test_tag_split_across_many_small_chunks(self):
        p = self._write_png()
        full = f"Here is the plot: MEDIA:{p} enjoy!"
        chunks = [full[i : i + 3] for i in range(0, len(full), 3)]
        r = _StreamingMediaResolver()
        out = "".join(r.feed(c) for c in chunks) + r.flush()
        self.assertIn("data:image/png;base64,", out)
        self.assertNotIn("MEDIA:", out)
        self.assertIn("Here is the plot:", out)
        self.assertIn("enjoy!", out)

    def test_tag_ends_exactly_at_stream_end_needs_flush(self):
        """A tag with nothing streamed after it must still resolve — the
        buffer can only know it's complete once ``flush()`` is called,
        since more path characters could otherwise still be coming."""
        p = self._write_png()
        full = f"Done: MEDIA:{p}"
        chunks = [full[i : i + 2] for i in range(0, len(full), 2)]
        r = _StreamingMediaResolver()
        out = "".join(r.feed(c) for c in chunks)
        # Nothing after the tag yet arrived, so it must still be withheld
        # pending flush() — this is what the bug looked like: a stream
        # that ends right after the tag with no data delivered at all.
        self.assertNotIn("data:image/png;base64,", out)
        out += r.flush()
        self.assertIn("data:image/png;base64,", out)
        self.assertNotIn("MEDIA:", out)

    def test_plain_text_streams_immediately_without_media_tag(self):
        """Text that never contains a MEDIA: tag must not be held back
        waiting for more chunks — that would add latency to the common
        case (a reply with no image) for no reason."""
        r = _StreamingMediaResolver()
        self.assertEqual(r.feed("Generating the plot now"), "Generating the plot now")
        self.assertEqual(r.feed(", please wait"), ", please wait")
        self.assertEqual(r.flush(), "")

    def test_partial_prefix_at_chunk_boundary_is_held_back(self):
        """A chunk ending in a partial ``MEDIA:`` prefix (e.g. the word
        got split as ``...MED`` + ``IA:/path...``) must not flush that
        partial prefix as literal text before the tag is known."""
        r = _StreamingMediaResolver()
        held = r.feed("some text MED")
        self.assertEqual(held, "some text ")
        p = self._write_png()
        out = r.feed(f"IA:{p} done") + r.flush()
        self.assertIn("data:image/png;base64,", out)
        self.assertIn("done", out)

    def test_missing_file_left_as_literal_text_after_flush(self):
        text = "MEDIA:/nonexistent/path/shot.png"
        r = _StreamingMediaResolver()
        out = "".join(r.feed(c) for c in [text[:10], text[10:]]) + r.flush()
        self.assertEqual(out, text)


if __name__ == "__main__":
    unittest.main()
