import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from agent.video_post import ffmpeg as ff
from agent.video_post.ffmpeg import FfmpegError


class RunFfmpegTest(unittest.TestCase):
    @patch("agent.video_post.ffmpeg.subprocess.run")
    @patch("agent.video_post.ffmpeg.find_ffmpeg", return_value="/fake/ffmpeg")
    def test_builds_list_command_with_overwrite_no_shell(self, _find, run):
        run.return_value = Mock(returncode=0, stderr="")
        ff.run_ffmpeg(["-i", "in.mp4", "out.mp4"], timeout=60)
        args, kwargs = run.call_args
        cmd = args[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "/fake/ffmpeg")
        self.assertIn("-y", cmd)
        self.assertIn("-i", cmd)
        self.assertNotIn("shell", kwargs)  # never pass shell=True
        self.assertEqual(kwargs.get("timeout"), 60)
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)

    @patch("agent.video_post.ffmpeg.subprocess.run")
    @patch("agent.video_post.ffmpeg.find_ffmpeg", return_value="/fake/ffmpeg")
    def test_nonzero_exit_raises_ffmpeg_error(self, _find, run):
        run.return_value = Mock(returncode=1, stderr="boom details")
        with self.assertRaises(FfmpegError) as ctx:
            ff.run_ffmpeg(["-i", "x"], timeout=60)
        self.assertEqual(ctx.exception.code, "ffmpeg_error")
        self.assertIn("boom", ctx.exception.stderr_tail)
        self.assertEqual(ctx.exception.returncode, 1)

    @patch(
        "agent.video_post.ffmpeg.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5, stderr="late"),
    )
    @patch("agent.video_post.ffmpeg.find_ffmpeg", return_value="/fake/ffmpeg")
    def test_timeout_maps_to_timeout_code(self, _find, _run):
        with self.assertRaises(FfmpegError) as ctx:
            ff.run_ffmpeg(["-i", "x"], timeout=5)
        self.assertEqual(ctx.exception.code, "timeout")

    @patch("agent.video_post.ffmpeg.find_ffmpeg", return_value=None)
    def test_missing_binary_raises_not_found(self, _find):
        with self.assertRaises(FfmpegError) as ctx:
            ff.run_ffmpeg(["-i", "x"], timeout=5)
        self.assertEqual(ctx.exception.code, "ffmpeg_not_found")


class AvailabilityTest(unittest.TestCase):
    @patch("agent.video_post.ffmpeg.find_ffmpeg", return_value=None)
    def test_available_false_when_missing(self, _find):
        self.assertFalse(ff.ffmpeg_available())

    @patch("agent.video_post.ffmpeg.find_ffmpeg", return_value="/fake/ffmpeg")
    def test_available_true_when_present(self, _find):
        self.assertTrue(ff.ffmpeg_available())

    @patch("agent.video_post.ffmpeg.find_ffmpeg", side_effect=RuntimeError("boom"))
    def test_available_never_raises(self, _find):
        self.assertFalse(ff.ffmpeg_available())


class ProbeTest(unittest.TestCase):
    _PROBE_JSON = json.dumps({
        "format": {"duration": "3.5"},
        "streams": [
            {"codec_type": "video", "width": 320, "height": 240, "avg_frame_rate": "30/1"},
            {"codec_type": "audio"},
        ],
    })

    @patch("agent.video_post.ffmpeg.subprocess.run")
    @patch("agent.video_post.ffmpeg.find_ffprobe", return_value="/fake/ffprobe")
    def test_probe_summarizes_streams(self, _find, run):
        run.return_value = Mock(returncode=0, stdout=self._PROBE_JSON, stderr="")
        info = ff.probe_media("/some/file.mp4")
        self.assertEqual(info["width"], 320)
        self.assertEqual(info["height"], 240)
        self.assertAlmostEqual(info["fps"], 30.0)
        self.assertAlmostEqual(info["duration"], 3.5)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])

    @patch("agent.video_post.ffmpeg.find_ffprobe", return_value=None)
    def test_probe_missing_ffprobe(self, _find):
        with self.assertRaises(FfmpegError) as ctx:
            ff.probe_media("/x")
        self.assertEqual(ctx.exception.code, "ffprobe_not_found")

    @patch("agent.video_post.ffmpeg.subprocess.run")
    @patch("agent.video_post.ffmpeg.find_ffprobe", return_value="/fake/ffprobe")
    def test_probe_invalid_json(self, _find, run):
        run.return_value = Mock(returncode=0, stdout="{not json", stderr="")
        with self.assertRaises(FfmpegError) as ctx:
            ff.probe_media("/x")
        self.assertEqual(ctx.exception.code, "ffmpeg_error")


class HelperTest(unittest.TestCase):
    def test_parse_fps_fraction(self):
        self.assertAlmostEqual(ff.parse_fps("30000/1001"), 29.97, places=2)

    def test_parse_fps_plain(self):
        self.assertAlmostEqual(ff.parse_fps("25"), 25.0)

    def test_parse_fps_bad(self):
        self.assertEqual(ff.parse_fps("garbage"), 0.0)
        self.assertEqual(ff.parse_fps("0/0"), 0.0)
        self.assertEqual(ff.parse_fps(""), 0.0)

    def test_snap_even(self):
        self.assertEqual(ff.snap_even(321), 320)
        self.assertEqual(ff.snap_even(320), 320)
        self.assertEqual(ff.snap_even(1), 0)


if __name__ == "__main__":
    unittest.main()
