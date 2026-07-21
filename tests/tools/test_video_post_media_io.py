import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.video_post import media_io as mio
from agent.video_post.media_io import InputError, TempTracker


class _FakeResponse:
    def __init__(self, chunks, url="http://example.com/f.mp4"):
        self._chunks = chunks
        self.url = url

    def raise_for_status(self):
        pass

    def iter_bytes(self, n):
        for c in self._chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url):
        return _FakeResponse(self._chunks, url)


class TempTrackerTest(unittest.TestCase):
    def test_cleanup_removes_files_and_dirs(self):
        tracker = TempTracker()
        f = Path(tempfile.NamedTemporaryFile(delete=False).name)
        f.write_text("x")
        tracker.add_file(f)
        d = tracker.mkdtemp()
        (d / "inner.txt").write_text("y")
        self.assertTrue(f.exists())
        self.assertTrue(d.exists())
        tracker.cleanup()
        self.assertFalse(f.exists())
        self.assertFalse(d.exists())

    def test_context_manager_cleans_on_exit(self):
        with TempTracker() as tracker:
            f = Path(tempfile.NamedTemporaryFile(delete=False).name)
            tracker.add_file(f)
        self.assertFalse(f.exists())


class ResolveInputTest(unittest.TestCase):
    def setUp(self):
        self.tracker = TempTracker()

    def tearDown(self):
        self.tracker.cleanup()

    def test_local_path_existing(self):
        f = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name)
        self.tracker.add_file(f)
        got = mio.resolve_input(str(f), tracker=self.tracker, max_bytes=1024, download_timeout=10)
        self.assertEqual(got, f)

    def test_local_path_missing(self):
        with self.assertRaises(InputError) as ctx:
            mio.resolve_input("/nonexistent/nope.mp4", tracker=self.tracker,
                              max_bytes=1024, download_timeout=10)
        self.assertEqual(ctx.exception.code, "input_not_found")

    def test_empty_ref(self):
        with self.assertRaises(InputError) as ctx:
            mio.resolve_input("  ", tracker=self.tracker, max_bytes=1024, download_timeout=10)
        self.assertEqual(ctx.exception.code, "invalid_args")

    def test_file_uri(self):
        f = Path(tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name)
        self.tracker.add_file(f)
        got = mio.resolve_input(f.as_uri(), tracker=self.tracker, max_bytes=1024, download_timeout=10)
        self.assertEqual(got, f)

    def test_data_uri_base64(self):
        payload = base64.b64encode(b"hello-video").decode()
        got = mio.resolve_input(f"data:video/mp4;base64,{payload}",
                                tracker=self.tracker, max_bytes=1024, download_timeout=10)
        self.assertEqual(got.read_bytes(), b"hello-video")
        self.assertTrue(str(got).endswith(".mp4"))

    def test_data_uri_oversize(self):
        payload = base64.b64encode(b"x" * 100).decode()
        with self.assertRaises(InputError) as ctx:
            mio.resolve_input(f"data:video/mp4;base64,{payload}",
                              tracker=self.tracker, max_bytes=10, download_timeout=10)
        self.assertEqual(ctx.exception.code, "download_failed")

    def test_data_uri_malformed(self):
        with self.assertRaises(InputError) as ctx:
            mio.resolve_input("data:notbase64", tracker=self.tracker, max_bytes=10, download_timeout=10)
        self.assertEqual(ctx.exception.code, "invalid_args")


class DownloadGuardTest(unittest.TestCase):
    def setUp(self):
        self.tracker = TempTracker()

    def tearDown(self):
        self.tracker.cleanup()

    @patch("agent.video_post.media_io.is_safe_url", return_value=False)
    def test_ssrf_blocked(self, _safe):
        with self.assertRaises(InputError) as ctx:
            mio.download_to_temp("http://169.254.169.254/x", tracker=self.tracker,
                                 max_bytes=1024, timeout_sec=10)
        self.assertEqual(ctx.exception.code, "ssrf_blocked")

    @patch("agent.video_post.media_io.httpx.Client")
    @patch("agent.video_post.media_io.is_safe_url", return_value=True)
    def test_download_respects_size_limit(self, _safe, client_cls):
        client_cls.return_value = _FakeClient([b"x" * 100])
        with self.assertRaises(InputError) as ctx:
            mio.download_to_temp("http://example.com/big.mp4", tracker=self.tracker,
                                 max_bytes=50, timeout_sec=10)
        self.assertEqual(ctx.exception.code, "download_failed")

    @patch("agent.video_post.media_io.httpx.Client")
    @patch("agent.video_post.media_io.is_safe_url", return_value=True)
    def test_download_success(self, _safe, client_cls):
        client_cls.return_value = _FakeClient([b"abc", b"def"])
        got = mio.download_to_temp("http://example.com/ok.mp4", tracker=self.tracker,
                                   max_bytes=1024, timeout_sec=10)
        self.assertEqual(got.read_bytes(), b"abcdef")


class OutputPathTest(unittest.TestCase):
    def test_output_uses_hermes_home_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HERMES_HOME": tmp}):
                p = mio.output_path("video_concat")
            self.assertTrue(str(p).startswith(tmp))
            self.assertIn("vpp_video_concat_", p.name)
            self.assertTrue(p.name.endswith(".mp4"))

    def test_output_dir_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = mio.output_path("video_pip", output_dir=tmp)
            self.assertTrue(str(p).startswith(tmp))


if __name__ == "__main__":
    unittest.main()
