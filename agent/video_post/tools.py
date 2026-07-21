"""Handlers for the four ffmpeg-backed video post-production tools.

Contract: every handler takes ``(args: dict, **kwargs)`` and returns a JSON
string; it never raises. User-supplied paths only ever appear as ``-i``
argument values, never inside filtergraph strings (SEC-01 + escaping safety).
"""

from __future__ import annotations

import contextlib
import logging
import uuid

from tools.registry import tool_error, tool_result

from .ffmpeg import FfmpegError, probe_media, run_ffmpeg, snap_even, subtitles_filter_available
from .media_io import InputError, TempTracker, output_path, resolve_input
from .settings import clamp_timeout, load_settings

logger = logging.getLogger(__name__)

_HINTS = {
    "timeout": "Increase timeout_sec, or reduce resolution/crf/input size.",
    "ffmpeg_not_found": "Install ffmpeg and ensure it is on PATH.",
    "ffprobe_not_found": "Install ffprobe (ships with ffmpeg) and ensure it is on PATH.",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve(ref: str, tracker: TempTracker, settings):
    return resolve_input(
        ref, tracker=tracker,
        max_bytes=settings.max_download_bytes,
        download_timeout=settings.download_timeout_sec,
    )


def _err(exc: Exception, tool: str) -> str:
    """Map any handler exception to a structured tool_error JSON string."""
    if isinstance(exc, InputError):
        return tool_error(str(exc), success=False, code=exc.code, tool=tool, hint=exc.hint)
    if isinstance(exc, FfmpegError):
        hint = exc.hint or _HINTS.get(exc.code, "")
        return tool_error(str(exc), success=False, code=exc.code, tool=tool,
                          stderr_tail=exc.stderr_tail, hint=hint)
    logger.exception("video_post tool %s unexpected failure", tool)
    return tool_error(str(exc), success=False, code="internal", tool=tool)


def _success(tool: str, path, info: dict | None = None, **extra) -> str:
    payload = {"success": True, "tool": tool, "video": str(path),
               "size_bytes": path.stat().st_size}
    if info:
        payload.update({"duration_sec": info.get("duration"),
                        "width": info.get("width"), "height": info.get("height")})
    payload.update(extra)
    return tool_result(payload)


def _cleanup_partial(out) -> None:
    if out is None:
        return
    with contextlib.suppress(OSError):
        if out.exists():
            out.unlink()


def _ensure_nonempty(info: dict, out, tool: str) -> None:
    if (info.get("duration") or 0) < 0.05:
        raise FfmpegError(
            "Output video is empty or near-empty", code="ffmpeg_error",
            hint="Check input integrity (stream-copy + -shortest on bad input can yield empty output).",
        )


def _require_str(args: dict, *keys: str) -> str | None:
    for key in keys:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    return "ok"


# ---------------------------------------------------------------------------
# video_concat
# ---------------------------------------------------------------------------

def handle_video_concat(args: dict, **kwargs) -> str:
    del kwargs
    tool = "video_concat"
    clips = args.get("clips")
    if not isinstance(clips, list) or len(clips) < 2:
        return tool_error("clips must be an array of at least 2 references",
                          success=False, code="invalid_args", tool=tool)
    settings = load_settings()
    timeout = clamp_timeout(args.get("timeout_sec"), settings)
    normalize = args.get("normalize", True)
    out = None
    try:
        with TempTracker() as tracker:
            paths = [_resolve(c, tracker, settings) for c in clips]
            out = output_path(tool, output_dir=settings.output_dir)
            if normalize:
                _concat_normalize(paths, out, args, timeout)
            else:
                _concat_demuxer(paths, out, tracker, timeout)
            info = probe_media(str(out))
            _ensure_nonempty(info, out, tool)
            return _success(tool, out, info)
    except Exception as exc:  # handler boundary: never raise
        _cleanup_partial(out)
        return _err(exc, tool)


def _concat_normalize(paths, out, args, timeout) -> None:
    infos = [probe_media(str(p)) for p in paths]
    if any(not i.get("has_video") for i in infos):
        raise InputError("All clips must contain a video stream (audio-only unsupported)",
                         code="invalid_args")
    first = infos[0]
    width = snap_even(args.get("width") or first.get("width") or 1280)
    height = snap_even(args.get("height") or first.get("height") or 720)
    fps = args.get("fps") or first.get("fps") or 30
    crf = int(args.get("crf", 20))
    inputs, vparts, aparts, concat_in = [], [], [], []
    for i, info in enumerate(infos):
        inputs += ["-i", str(paths[i])]
        vparts.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]"
        )
        aparts.append(_audio_segment(i, info))
        concat_in.append(f"[v{i}][a{i}]")
    fc = ";".join(vparts + aparts) + ";" + "".join(concat_in) + \
        f"concat=n={len(paths)}:v=1:a=1[v][a]"
    run_ffmpeg([*inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
               timeout=timeout)


def _audio_segment(index: int, info: dict) -> str:
    if info.get("has_audio"):
        return (f"[{index}:a]aresample=44100,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{index}]")
    dur = info.get("duration") or 0
    return (f"anullsrc=channel_layout=stereo:sample_rate=44100,"
            f"atrim=0:{dur}[a{index}]")


def _concat_demuxer(paths, out, tracker, timeout) -> None:
    list_file = tracker.mkdtemp(prefix="vpp_concat_") / "concat_list.txt"
    lines = [f"file '{_demuxer_escape(str(p))}'" for p in paths]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file),
                    "-c", "copy", str(out)], timeout=timeout)
    except FfmpegError as exc:
        exc.hint = "Clips likely differ in codec/resolution/fps; retry with normalize=true."
        raise


def _demuxer_escape(path: str) -> str:
    return path.replace("'", "'\\''")


# ---------------------------------------------------------------------------
# video_add_captions
# ---------------------------------------------------------------------------

def handle_video_add_captions(args: dict, **kwargs) -> str:
    del kwargs
    tool = "video_add_captions"
    if not _require_str(args, "video"):
        return tool_error("video is required", success=False, code="invalid_args", tool=tool)
    text = args.get("subtitles_text")
    sfile = args.get("subtitles_file")
    if bool(text) == bool(sfile):
        return tool_error("Provide exactly one of subtitles_text or subtitles_file",
                          success=False, code="invalid_args", tool=tool)
    mode = args.get("mode", "burn")
    if mode not in ("burn", "soft"):
        return tool_error("mode must be 'burn' or 'soft'", success=False, code="invalid_args", tool=tool)
    settings = load_settings()
    timeout = clamp_timeout(args.get("timeout_sec"), settings)
    out = None
    try:
        with TempTracker() as tracker:
            video_path = _resolve(args["video"], tracker, settings)
            subs_path, is_ass = _materialize_subtitles(text, sfile, tracker, settings)
            if mode == "soft" and is_ass:
                return tool_error("soft mode supports SRT only; use burn for ASS",
                                  success=False, code="invalid_args", tool=tool)
            out = output_path(tool, output_dir=settings.output_dir)
            if mode == "burn":
                _captions_burn(video_path, subs_path, out, args, timeout)
            else:
                run_ffmpeg(["-i", str(video_path), "-i", str(subs_path),
                            "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text",
                            str(out)], timeout=timeout)
            info = probe_media(str(out))
            _ensure_nonempty(info, out, tool)
            return _success(tool, out, info)
    except Exception as exc:  # handler boundary: never raise
        _cleanup_partial(out)
        return _err(exc, tool)


def _materialize_subtitles(text, sfile, tracker, settings):
    """Read subtitles from text or file, normalize to a clean ASCII-named file."""
    workdir = tracker.mkdtemp(prefix="vpp_subs_")
    if text is not None:
        content = str(text).lstrip("﻿")
    else:
        src = _resolve(sfile, tracker, settings)
        content = _decode_subtitle_bytes(src.read_bytes())
    is_ass = _looks_ass(content)
    ext = "ass" if is_ass else "srt"
    path = workdir / f"subs_{uuid.uuid4().hex[:8]}.{ext}"
    path.write_text(content, encoding="utf-8")
    return path, is_ass


def _decode_subtitle_bytes(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _looks_ass(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return "[v4+ styles]" in head or "[v4 styles]" in head or head.startswith("[script info]")


def _captions_burn(video_path, subs_path, out, args, timeout) -> None:
    if not subtitles_filter_available():
        raise FfmpegError(
            "This ffmpeg build has no 'subtitles' filter (libass missing)",
            code="ffmpeg_missing_filter",
            hint=("Install a full ffmpeg build with libass (e.g. `brew install ffmpeg` "
                  "or `brew reinstall ffmpeg`). Soft mode does not need libass — "
                  "retry with mode='soft' to embed a selectable subtitle track."),
        )
    subs_filter = f"subtitles={_filter_escape(str(subs_path))}"
    force_style = _build_force_style(args)
    if force_style:
        subs_filter += f":force_style='{force_style}'"
    fonts_dir = args.get("fonts_dir")
    if isinstance(fonts_dir, str) and fonts_dir.strip():
        subs_filter += f":fontsdir={_filter_escape(fonts_dir.strip())}"
    run_ffmpeg(["-i", str(video_path), "-vf", subs_filter,
                "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
                "-c:a", "copy", str(out)], timeout=timeout)


def _filter_escape(value: str) -> str:
    # The subtitle path is a clean ASCII temp name; escape defensively for the
    # filtergraph filename parser anyway.
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _build_force_style(args: dict) -> str:
    raw = args.get("force_style")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    style = args.get("style")
    if not isinstance(style, dict):
        return ""
    parts = []
    if isinstance(style.get("font"), str) and style["font"].strip():
        parts.append(f"FontName={style['font'].strip()}")
    if isinstance(style.get("font_size"), int) and not isinstance(style.get("font_size"), bool):
        parts.append(f"FontSize={style['font_size']}")
    if isinstance(style.get("outline"), (int, float)) and not isinstance(style.get("outline"), bool):
        parts.append(f"Outline={style['outline']}")
    if isinstance(style.get("margin_v"), int) and not isinstance(style.get("margin_v"), bool):
        parts.append(f"MarginV={style['margin_v']}")
    return ",".join(parts)


# ---------------------------------------------------------------------------
# video_audio_mix
# ---------------------------------------------------------------------------

def handle_video_audio_mix(args: dict, **kwargs) -> str:
    del kwargs
    tool = "video_audio_mix"
    if not _require_str(args, "video", "audio"):
        return tool_error("video and audio are required", success=False, code="invalid_args", tool=tool)
    mode = args.get("mode", "replace")
    if mode not in ("replace", "mix"):
        return tool_error("mode must be 'replace' or 'mix'", success=False, code="invalid_args", tool=tool)
    end = args.get("end", "video")
    settings = load_settings()
    timeout = clamp_timeout(args.get("timeout_sec"), settings)
    volume = _gain(args.get("volume", 1.0))
    out = None
    try:
        with TempTracker() as tracker:
            video_path = _resolve(args["video"], tracker, settings)
            audio_path = _resolve(args["audio"], tracker, settings)
            info = probe_media(str(video_path))
            if not info.get("has_video"):
                return tool_error("base video has no video stream",
                                  success=False, code="invalid_args", tool=tool)
            out = output_path(tool, output_dir=settings.output_dir)
            note = None
            if mode == "replace" or not info.get("has_audio"):
                if mode == "mix" and not info.get("has_audio"):
                    note = "base video has no audio track; mode=mix treated as replace"
                _audio_replace(video_path, audio_path, out, volume, timeout)
            else:
                _audio_mix(video_path, audio_path, out, volume,
                           _gain(args.get("original_volume", 1.0)), end, timeout)
            outinfo = probe_media(str(out))
            _ensure_nonempty(outinfo, out, tool)
            extra = {"note": note} if note else {}
            return _success(tool, out, outinfo, **extra)
    except Exception as exc:  # handler boundary: never raise
        _cleanup_partial(out)
        return _err(exc, tool)


def _gain(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    return max(0.0, min(float(value), 3.0))


def _audio_replace(video_path, audio_path, out, volume, timeout) -> None:
    run_ffmpeg(["-i", str(video_path), "-i", str(audio_path),
                "-map", "0:v:0", "-map", "1:a:0", "-af", f"volume={volume}",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(out)],
               timeout=timeout)


def _audio_mix(video_path, audio_path, out, volume, orig_volume, end, timeout) -> None:
    duration = "first" if end == "video" else "longest"
    fc = (f"[0:a]volume={orig_volume}[a0];"
          f"[1:a]volume={volume}[a1];"
          f"[a0][a1]amix=inputs=2:duration={duration}:dropout_transition=0:normalize=0[a]")
    run_ffmpeg(["-i", str(video_path), "-i", str(audio_path),
                "-filter_complex", fc, "-map", "0:v:0", "-map", "[a]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(out)],
               timeout=timeout)


# ---------------------------------------------------------------------------
# video_pip
# ---------------------------------------------------------------------------

_POSITIONS = {
    "top_left": ("{m}", "{m}"),
    "top_right": ("main_w-overlay_w-{m}", "{m}"),
    "bottom_left": ("{m}", "main_h-overlay_h-{m}"),
    "bottom_right": ("main_w-overlay_w-{m}", "main_h-overlay_h-{m}"),
    "center": ("(main_w-overlay_w)/2", "(main_h-overlay_h)/2"),
}


def handle_video_pip(args: dict, **kwargs) -> str:
    del kwargs
    tool = "video_pip"
    if not _require_str(args, "base", "overlay"):
        return tool_error("base and overlay are required", success=False, code="invalid_args", tool=tool)
    position = args.get("position", "bottom_right")
    if position not in _POSITIONS:
        return tool_error("invalid position", success=False, code="invalid_args", tool=tool)
    scale = args.get("scale", 0.25)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not (0.05 <= scale <= 0.95):
        return tool_error("scale must be between 0.05 and 0.95",
                          success=False, code="invalid_args", tool=tool)
    margin = int(args.get("margin_px", 16))
    loop = bool(args.get("loop_overlay", False))
    settings = load_settings()
    timeout = clamp_timeout(args.get("timeout_sec"), settings)
    out = None
    try:
        with TempTracker() as tracker:
            base_path = _resolve(args["base"], tracker, settings)
            overlay_path = _resolve(args["overlay"], tracker, settings)
            info = probe_media(str(base_path))
            if not info.get("has_video") or not info.get("width"):
                return tool_error("base video has no video stream/width",
                                  success=False, code="invalid_args", tool=tool)
            ow = snap_even(int(info["width"] * scale))
            x_expr, y_expr = _pip_xy(position, margin)
            out = output_path(tool, output_dir=settings.output_dir)
            _pip_run(base_path, overlay_path, out, ow, x_expr, y_expr, loop, timeout)
            outinfo = probe_media(str(out))
            _ensure_nonempty(outinfo, out, tool)
            return _success(tool, out, outinfo)
    except Exception as exc:  # handler boundary: never raise
        _cleanup_partial(out)
        return _err(exc, tool)


def _pip_xy(position: str, margin: int) -> tuple[str, str]:
    x_tpl, y_tpl = _POSITIONS[position]
    return x_tpl.format(m=margin), y_tpl.format(m=margin)


def _pip_run(base_path, overlay_path, out, ow, x_expr, y_expr, loop, timeout) -> None:
    inputs = ["-i", str(base_path)]
    if loop:
        inputs += ["-stream_loop", "-1"]
    inputs += ["-i", str(overlay_path)]
    fc = f"[1:v]scale={ow}:-2[pip];[0:v][pip]overlay={x_expr}:{y_expr}:eof_action=pass[v]"
    cmd = [*inputs, "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
           "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "copy"]
    if loop:
        cmd += ["-shortest"]
    cmd.append(str(out))
    run_ffmpeg(cmd, timeout=timeout)
