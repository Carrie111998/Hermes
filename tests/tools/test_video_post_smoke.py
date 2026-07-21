"""Real-ffmpeg smoke tests for the video_post tools.

These actually run ffmpeg/ffprobe and produce real MP4s, then verify duration
and stream presence with ffprobe. Skipped entirely when ffmpeg/ffprobe are
absent. HERMES_HOME is pointed at a temp dir so the real ``~/.hermes`` cache is
never touched.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent.video_post import ffmpeg as ff
from agent.video_post.html_video import handle_html_to_video
from agent.video_post.tools import (
    handle_video_add_captions,
    handle_video_audio_mix,
    handle_video_concat,
    handle_video_pip,
)

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def _make_clip(path, *, size="320x240", rate=30, dur=1, audio=True):
    args = ["ffmpeg", "-y", "-f", "lavfi",
            "-i", f"testsrc=duration={dur}:size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                 "-c:a", "aac", "-shortest"]
    else:
        args += ["-an"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True, capture_output=True, timeout=120)


def _make_audio(path, dur=1):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=660:duration={dur}",
                    "-c:a", "libmp3lame", str(path)],
                   check=True, capture_output=True, timeout=120)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class VideoPostSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vpp_smoke_"))
        cls.voiced = cls.tmp / "voiced.mp4"      # 320x240 @30, with audio
        cls.silent = cls.tmp / "silent.mp4"      # 640x480 @25, no audio (heterogeneous)
        cls.music = cls.tmp / "music.mp3"
        _make_clip(cls.voiced, size="320x240", rate=30, dur=1, audio=True)
        _make_clip(cls.silent, size="640x480", rate=25, dur=1, audio=False)
        _make_audio(cls.music, dur=1)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="vpp_smoke_home_"))
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _run(self, handler, args):
        res = json.loads(handler(args))
        self.assertTrue(res.get("success"), f"tool failed: {res}")
        out = Path(res["video"])
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 0)
        return res, ff.probe_media(str(out))

    def test_concat_normalize_heterogeneous(self):
        res, info = self._run(handle_video_concat, {
            "clips": [str(self.voiced), str(self.silent)], "normalize": True})
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.5)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])  # silent clip filled via anullsrc

    def test_concat_demuxer_same_codec(self):
        res, info = self._run(handle_video_concat, {
            "clips": [str(self.voiced), str(self.voiced)], "normalize": False})
        self.assertAlmostEqual(info["duration"], 2.0, delta=0.5)
        self.assertTrue(info["has_video"])

    def test_captions_burn_chinese_srt(self):
        if not ff.subtitles_filter_available():
            self.skipTest("ffmpeg build lacks the subtitles filter (libass)")
        srt = "1\n00:00:00,000 --> 00:00:01,000\n你好世界字幕"
        res, info = self._run(handle_video_add_captions, {
            "video": str(self.voiced), "subtitles_text": srt, "mode": "burn"})
        self.assertTrue(info["has_video"])

    def test_captions_burn_missing_filter_returns_actionable_error(self):
        # On a libass-less build, burn must fail with a clear code+hint, not a
        # generic ffmpeg_error and never a silent no-subtitle output.
        if ff.subtitles_filter_available():
            self.skipTest("ffmpeg has the subtitles filter; cannot exercise missing path")
        res = json.loads(handle_video_add_captions({
            "video": str(self.voiced), "subtitles_text": "1\n00:00:00,000 --> 00:00:01,000\nx",
            "mode": "burn"}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "ffmpeg_missing_filter")
        self.assertIn("libass", res["hint"])

    def test_captions_soft_embeds_track(self):
        # soft mode (mov_text) must work even without libass.
        srt = "1\n00:00:00,000 --> 00:00:01,000\nsoft subtitle"
        res, info = self._run(handle_video_add_captions, {
            "video": str(self.voiced), "subtitles_text": srt, "mode": "soft"})
        self.assertTrue(info["has_video"])

    def test_audio_mix_replace_adds_track(self):
        res, info = self._run(handle_video_audio_mix, {
            "video": str(self.silent), "audio": str(self.music), "mode": "replace"})
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])  # silent base now has audio

    def test_audio_mix_mix_over_existing(self):
        res, info = self._run(handle_video_audio_mix, {
            "video": str(self.voiced), "audio": str(self.music), "mode": "mix"})
        self.assertTrue(info["has_audio"])

    def test_pip_overlay(self):
        res, info = self._run(handle_video_pip, {
            "base": str(self.voiced), "overlay": str(self.voiced), "scale": 0.3})
        self.assertTrue(info["has_video"])

    def test_html_to_video_degrades_without_browser(self):
        # playwright is not installed in this environment: the tool must return
        # a clean browser_not_available error, never raise, never "internal".
        try:
            import playwright.sync_api  # noqa: F401
            self.skipTest("playwright installed; degradation path not applicable")
        except ImportError:
            pass
        res = json.loads(handle_html_to_video({"html": "<h1>hi</h1>"}))
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "browser_not_available")
        self.assertIn("playwright", res["hint"])

    def test_html_to_video_renders_with_browser(self):
        # With playwright installed, a real render must produce a non-empty video.
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        res = json.loads(handle_html_to_video({
            "html": "<h1>render test</h1>", "duration_sec": 0.5, "fps": 5,
            "width": 320, "height": 180, "settle_sec": 0.2}))
        self.assertTrue(res.get("success"), f"render failed: {res}")
        info = ff.probe_media(res["video"])
        self.assertTrue(info["has_video"])
        self.assertGreater(info["duration"], 0)


if __name__ == "__main__":
    unittest.main()
