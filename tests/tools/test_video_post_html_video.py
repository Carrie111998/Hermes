import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.video_post import html_video as H

_DEFAULT_INFO = {"duration": 5.0, "width": 1280, "height": 720, "fps": 30.0,
                 "has_video": True, "has_audio": False}


class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    def close(self):
        pass


class _FakeProc:
    def __init__(self, returncode=0):
        self.stdin = _FakeStdin()
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def _force_import_error():
    """Patch sys.modules so `from playwright.sync_api import ...` raises ImportError."""
    return patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None})


class _FakeMsg:
    def __init__(self, mtype):
        self.type = mtype


class _FakePage:
    def __init__(self):
        self._console_cb = None

    def on(self, event, cb):
        if event == "console":
            self._console_cb = cb

    def set_content(self, html, wait_until=None):
        if self._console_cb:
            self._console_cb(_FakeMsg("error"))  # simulate one console error

    def goto(self, url, wait_until=None):
        pass

    def wait_for_timeout(self, ms):
        pass

    def screenshot(self, type="png"):
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class _FakeBrowser:
    def __init__(self, launch_exc=None):
        self._launch_exc = launch_exc
        self.page = _FakePage()

    def new_page(self, viewport=None, device_scale_factor=1):
        return self.page

    def close(self):
        pass


def _install_fake_playwright(launch_exc=None):
    """Install a fake playwright.sync_api into sys.modules.

    launch_exc: if set, chromium.launch raises it (simulates missing browser).
    """
    browser = _FakeBrowser(launch_exc)

    class _Chromium:
        @staticmethod
        def launch(timeout=0):
            if launch_exc is not None:
                raise launch_exc
            return browser

    class _Pw:
        chromium = _Chromium

    class _Sync:
        def __enter__(self):
            return _Pw()

        def __exit__(self, *a):
            return False

    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _Sync()
    parent = types.ModuleType("playwright")
    parent.sync_api = mod
    return patch.dict(sys.modules, {"playwright": parent, "playwright.sync_api": mod})


class HtmlArgValidationTest(unittest.TestCase):
    def test_both_html_and_source_invalid(self):
        res = json.loads(H.handle_html_to_video({"html": "<h1>x</h1>", "source": "/a.html"}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "invalid_args")

    def test_neither_invalid(self):
        res = json.loads(H.handle_html_to_video({}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "invalid_args")


class HtmlBrowserImportTest(unittest.TestCase):
    def test_missing_playwright_returns_browser_not_available(self):
        with _force_import_error():
            res = json.loads(H.handle_html_to_video({"html": "<h1>x</h1>"}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "browser_not_available")
        self.assertIn("playwright install chromium", res["hint"])


class HtmlBrowserLaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vpp_html_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_file = self.tmp / "out.mp4"

    def test_launch_failure_returns_browser_not_available(self):
        proc = _FakeProc()
        with _install_fake_playwright(launch_exc=RuntimeError("no chromium")), \
             patch("agent.video_post.html_video.run_ffmpeg_pipe", return_value=proc), \
             patch("agent.video_post.html_video.output_path", return_value=self.out_file):
            res = json.loads(H.handle_html_to_video({"html": "<h1>x</h1>"}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "browser_not_available")
        self.assertIn("playwright install chromium", res["hint"])
        # Launch fails before any frame is captured, so nothing hit the pipe.
        self.assertEqual(proc.stdin.written, [])


class HtmlRenderSuccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vpp_html_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out_file = self.tmp / "out.mp4"
        self.proc = _FakeProc()

        def fake_pipe(args, *, stderr=None):
            Path(args[-1]).write_bytes(b"\x00" * 64)  # simulate encoded output
            return self.proc

        self._p1 = patch("agent.video_post.html_video.run_ffmpeg_pipe", side_effect=fake_pipe)
        self._p2 = patch("agent.video_post.html_video.probe_media", return_value=dict(_DEFAULT_INFO))
        self._p3 = patch("agent.video_post.html_video.output_path", return_value=self.out_file)
        self._p1.start()
        self._p2.start()
        self._p3.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)
        self.addCleanup(self._p3.stop)

    def test_render_success_reports_console_errors(self):
        with _install_fake_playwright():
            res = json.loads(H.handle_html_to_video(
                {"html": "<h1>hi</h1>", "duration_sec": 1, "fps": 10}))
        self.assertTrue(res["success"])
        self.assertEqual(res["video"], str(self.out_file))
        self.assertEqual(res["console_errors"], 1)  # one simulated console error
        self.assertEqual(len(self.proc.stdin.written), 10)  # round(1*10) frames

    def test_encoder_failure_returns_ffmpeg_error(self):
        self.proc.returncode = 1  # encoder exits non-zero
        with _install_fake_playwright():
            res = json.loads(H.handle_html_to_video({"html": "<h1>x</h1>"}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "ffmpeg_error")


if __name__ == "__main__":
    unittest.main()
