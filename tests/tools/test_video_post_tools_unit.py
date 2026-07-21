import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.video_post import tools as T
from agent.video_post.ffmpeg import FfmpegError

_DEFAULT_INFO = {"duration": 1.0, "width": 320, "height": 240, "fps": 30.0,
                 "has_video": True, "has_audio": True}


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


class ToolTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vpp_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.commands = []
        self.concat_list_content = None
        self.out_file = self.tmp / "out.mp4"
        self.input_info = {}

        def fake_run(args, *, timeout):
            self.commands.append(list(args))
            if "-f" in args and args[args.index("-f") + 1] == "concat":
                self.concat_list_content = Path(args[args.index("-i") + 1]).read_text()
            Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[-1]).write_bytes(b"\x00" * 16)

        def fake_probe(path):
            p = str(path)
            if p == str(self.out_file):
                return {**_DEFAULT_INFO, "duration": 3.0}
            return self.input_info.get(p, dict(_DEFAULT_INFO))

        self._patch("agent.video_post.tools.run_ffmpeg", side_effect=fake_run)
        self._patch("agent.video_post.tools.probe_media", side_effect=fake_probe)
        self._patch("agent.video_post.tools.output_path", return_value=self.out_file)
        self._patch("agent.video_post.tools.resolve_input",
                    side_effect=lambda ref, **kw: Path(ref))
        self._patch("agent.video_post.tools.subtitles_filter_available", return_value=True)

    def _patch(self, target, **kwargs):
        p = patch(target, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def _result(self, handler, args):
        return json.loads(handler(args))


class ConcatTest(ToolTestBase):
    def test_normalize_builds_concat_filter_with_silent_fill(self):
        self.input_info["/clip1.mp4"] = {**_DEFAULT_INFO, "has_audio": False}
        res = self._result(T.handle_video_concat,
                           {"clips": ["/clip0.mp4", "/clip1.mp4", "/clip2.mp4"]})
        self.assertTrue(res["success"])
        self.assertEqual(res["video"], str(self.out_file))
        fc = _flag(self.commands[0], "-filter_complex")
        self.assertIn("concat=n=3:v=1:a=1", fc)
        self.assertIn("anullsrc", fc)          # silent clip filled
        self.assertIn("format=yuv420p", fc)
        self.assertIn("aresample=44100", fc)   # audio clip normalized

    def test_demuxer_mode_uses_stream_copy_and_list_file(self):
        res = self._result(T.handle_video_concat,
                           {"clips": ["/a.mp4", "/b.mp4"], "normalize": False})
        self.assertTrue(res["success"])
        cmd = self.commands[0]
        self.assertEqual(_flag(cmd, "-f"), "concat")
        self.assertEqual(_flag(cmd, "-safe"), "0")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertIn("file '/a.mp4'", self.concat_list_content)
        self.assertIn("file '/b.mp4'", self.concat_list_content)

    def test_too_few_clips_invalid(self):
        res = self._result(T.handle_video_concat, {"clips": ["/only.mp4"]})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "invalid_args")

    def test_clips_wrong_type_invalid(self):
        res = self._result(T.handle_video_concat, {"clips": "nope"})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "invalid_args")

    def test_audio_only_clip_invalid(self):
        self.input_info["/clip1.mp4"] = {**_DEFAULT_INFO, "has_video": False}
        res = self._result(T.handle_video_concat,
                           {"clips": ["/clip0.mp4", "/clip1.mp4"]})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "invalid_args")

    def test_ffmpeg_error_returned_not_raised(self):
        self._patch("agent.video_post.tools.run_ffmpeg",
                    side_effect=FfmpegError("boom", code="ffmpeg_error", stderr_tail="x"))
        res = self._result(T.handle_video_concat, {"clips": ["/a.mp4", "/b.mp4"]})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "ffmpeg_error")


class CaptionsTest(ToolTestBase):
    def test_burn_uses_clean_ascii_subs_path_not_user_content(self):
        res = self._result(T.handle_video_add_captions, {
            "video": "/v.mp4",
            "subtitles_text": "1\n00:00:00,000 --> 00:00:02,000\n你好世界",
            "mode": "burn",
        })
        self.assertTrue(res["success"])
        vf = _flag(self.commands[0], "-vf")
        self.assertIn("subtitles=", vf)
        # The path inside the filter must be an ASCII temp name, no CJK.
        path_part = vf.split("subtitles=", 1)[1]
        self.assertTrue(path_part.split(":")[0].isascii())
        self.assertIn("subs_", path_part)
        self.assertNotIn("你好", vf)

    def test_burn_force_style_assembled(self):
        self._result(T.handle_video_add_captions, {
            "video": "/v.mp4", "subtitles_text": "x",
            "style": {"font": "PingFang SC", "font_size": 48, "outline": 2},
        })
        vf = _flag(self.commands[0], "-vf")
        self.assertIn("force_style=", vf)
        self.assertIn("FontName=PingFang SC", vf)
        self.assertIn("FontSize=48", vf)
        self.assertIn("Outline=2", vf)

    def test_chinese_subtitle_file_path_not_in_filter(self):
        cn = self.tmp / "用户字幕.srt"
        cn.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi", encoding="utf-8")
        self._result(T.handle_video_add_captions,
                     {"video": "/v.mp4", "subtitles_file": str(cn), "mode": "burn"})
        vf = _flag(self.commands[0], "-vf")
        self.assertNotIn("用户字幕", vf)
        self.assertIn("subs_", vf)

    def test_soft_uses_mov_text(self):
        res = self._result(T.handle_video_add_captions,
                           {"video": "/v.mp4", "subtitles_text": "x", "mode": "soft"})
        self.assertTrue(res["success"])
        cmd = self.commands[0]
        self.assertIn("mov_text", cmd)

    def test_soft_with_ass_rejected(self):
        res = self._result(T.handle_video_add_captions, {
            "video": "/v.mp4", "mode": "soft",
            "subtitles_text": "[Script Info]\n[V4+ Styles]\n",
        })
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "invalid_args")

    def test_both_sources_invalid(self):
        res = self._result(T.handle_video_add_captions,
                           {"video": "/v.mp4", "subtitles_text": "a", "subtitles_file": "/b.srt"})
        self.assertEqual(res["code"], "invalid_args")

    def test_neither_source_invalid(self):
        res = self._result(T.handle_video_add_captions, {"video": "/v.mp4"})
        self.assertEqual(res["code"], "invalid_args")

    def test_burn_without_subtitles_filter_reports_missing_libass(self):
        self._patch("agent.video_post.tools.subtitles_filter_available", return_value=False)
        res = self._result(T.handle_video_add_captions,
                           {"video": "/v.mp4", "subtitles_text": "x", "mode": "burn"})
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], "ffmpeg_missing_filter")
        self.assertIn("libass", res["hint"])

    def test_soft_mode_does_not_require_subtitles_filter(self):
        self._patch("agent.video_post.tools.subtitles_filter_available", return_value=False)
        res = self._result(T.handle_video_add_captions,
                           {"video": "/v.mp4", "subtitles_text": "x", "mode": "soft"})
        self.assertTrue(res["success"])  # mov_text path, no libass needed


class AudioMixTest(ToolTestBase):
    def test_replace_copies_video_stream(self):
        res = self._result(T.handle_video_audio_mix,
                           {"video": "/v.mp4", "audio": "/a.mp3", "mode": "replace"})
        self.assertTrue(res["success"])
        cmd = self.commands[0]
        self.assertIn("0:v:0", cmd)
        self.assertIn("1:a:0", cmd)
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertIn("-shortest", cmd)

    def test_mix_uses_amix_normalize_zero(self):
        res = self._result(T.handle_video_audio_mix,
                           {"video": "/v.mp4", "audio": "/a.mp3", "mode": "mix",
                            "volume": 0.5, "original_volume": 0.8})
        self.assertTrue(res["success"])
        fc = _flag(self.commands[0], "-filter_complex")
        self.assertIn("amix=inputs=2", fc)
        self.assertIn("normalize=0", fc)
        self.assertIn("volume=0.5", fc)
        self.assertIn("volume=0.8", fc)

    def test_mix_degrades_to_replace_when_no_base_audio(self):
        self.input_info["/v.mp4"] = {**_DEFAULT_INFO, "has_audio": False}
        res = self._result(T.handle_video_audio_mix,
                           {"video": "/v.mp4", "audio": "/a.mp3", "mode": "mix"})
        self.assertTrue(res["success"])
        self.assertIn("treated as replace", res["note"])
        self.assertIn("-c:v", self.commands[0])  # replace path used

    def test_base_without_video_invalid(self):
        self.input_info["/v.mp4"] = {**_DEFAULT_INFO, "has_video": False}
        res = self._result(T.handle_video_audio_mix,
                           {"video": "/v.mp4", "audio": "/a.mp3"})
        self.assertEqual(res["code"], "invalid_args")

    def test_missing_audio_invalid(self):
        res = self._result(T.handle_video_audio_mix, {"video": "/v.mp4"})
        self.assertEqual(res["code"], "invalid_args")


class PipTest(ToolTestBase):
    def test_bottom_right_overlay_expression_and_even_width(self):
        res = self._result(T.handle_video_pip,
                           {"base": "/b.mp4", "overlay": "/o.mp4", "scale": 0.25})
        self.assertTrue(res["success"])
        fc = _flag(self.commands[0], "-filter_complex")
        self.assertIn("overlay=main_w-overlay_w-16:main_h-overlay_h-16", fc)
        self.assertIn("eof_action=pass", fc)
        # base width 320 * 0.25 = 80 (even)
        self.assertIn("scale=80:-2", fc)

    def test_center_position(self):
        self._result(T.handle_video_pip,
                     {"base": "/b.mp4", "overlay": "/o.mp4", "position": "center"})
        fc = _flag(self.commands[0], "-filter_complex")
        self.assertIn("overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2", fc)

    def test_loop_overlay_adds_stream_loop_and_shortest(self):
        self._result(T.handle_video_pip,
                     {"base": "/b.mp4", "overlay": "/o.mp4", "loop_overlay": True})
        cmd = self.commands[0]
        self.assertIn("-stream_loop", cmd)
        self.assertEqual(cmd[cmd.index("-stream_loop") + 1], "-1")
        self.assertIn("-shortest", cmd)

    def test_bad_scale_invalid(self):
        res = self._result(T.handle_video_pip,
                           {"base": "/b.mp4", "overlay": "/o.mp4", "scale": 2.0})
        self.assertEqual(res["code"], "invalid_args")

    def test_bad_position_invalid(self):
        res = self._result(T.handle_video_pip,
                           {"base": "/b.mp4", "overlay": "/o.mp4", "position": "middle"})
        self.assertEqual(res["code"], "invalid_args")

    def test_base_without_video_invalid(self):
        self.input_info["/b.mp4"] = {**_DEFAULT_INFO, "has_video": False, "width": 0}
        res = self._result(T.handle_video_pip, {"base": "/b.mp4", "overlay": "/o.mp4"})
        self.assertEqual(res["code"], "invalid_args")


if __name__ == "__main__":
    unittest.main()
