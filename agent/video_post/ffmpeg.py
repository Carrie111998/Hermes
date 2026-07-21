"""Safe local ffmpeg / ffprobe invocation helpers.

Security baseline (SEC-01): subprocess arguments are ALWAYS a list, never a
shell string; ``shell=True`` is never used. User-supplied paths only ever
appear as ``-i`` argument values, never inside filtergraph strings (see the
handlers in ``tools.py``).
"""

from __future__ import annotations

import functools
import json
import logging
import shutil
import subprocess
from typing import Optional

from .settings import load_settings

logger = logging.getLogger(__name__)

_STDERR_TAIL = 2000


class FfmpegError(Exception):
    """Raised for any ffmpeg/ffprobe failure, tagged with a machine code."""

    def __init__(self, message, *, code="ffmpeg_error", stderr_tail="", returncode=None, hint=""):
        super().__init__(message)
        self.code = code
        self.stderr_tail = stderr_tail
        self.returncode = returncode
        self.hint = hint


def _windows_hide_flags() -> int:
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags
        return windows_hide_flags()
    except Exception:
        return 0


def find_ffmpeg() -> Optional[str]:
    s = load_settings()
    return s.ffmpeg_path or shutil.which("ffmpeg")


def find_ffprobe() -> Optional[str]:
    s = load_settings()
    return s.ffprobe_path or shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    """Toolset check_fn: fail-closed, never raises."""
    try:
        return find_ffmpeg() is not None
    except Exception as exc:  # defensive: a raising check_fn disables the toolset
        logger.debug("ffmpeg_available check failed: %s", exc)
        return False


@functools.lru_cache(maxsize=1)
def subtitles_filter_available() -> bool:
    """True when this ffmpeg build has the libass-backed ``subtitles`` filter.

    Some ffmpeg builds (minimal/homebrew variants without libass) lack it; burn
    mode then fails. Detecting this up front lets the handler return a clear,
    actionable error instead of a generic ffmpeg_error. Fail-closed, never raises.
    """
    try:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            return False
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-h", "filter=subtitles"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL, creationflags=_windows_hide_flags(),
        )
    except Exception as exc:  # defensive: never raise from a probe
        logger.debug("subtitles_filter_available probe failed: %s", exc)
        return False
    if "Unknown filter" in f"{result.stdout}{result.stderr}":
        return False
    return result.returncode == 0


def snap_even(n) -> int:
    """Round down to an even integer (libx264 requires even dimensions)."""
    n = int(n)
    return n - (n % 2)


def run_ffmpeg(args: list, *, timeout: float) -> None:
    """Run a one-shot ffmpeg command. Raises FfmpegError on any failure."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FfmpegError("ffmpeg binary not found", code="ffmpeg_not_found")
    cmd = [ffmpeg, "-y", "-hide_banner", *args]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=_windows_hide_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(
            f"ffmpeg timed out after {timeout}s", code="timeout",
            stderr_tail=str(exc.stderr or "")[-_STDERR_TAIL:],
        ) from exc
    except FileNotFoundError as exc:
        raise FfmpegError("ffmpeg binary not found", code="ffmpeg_not_found") from exc
    if result.returncode != 0:
        raise FfmpegError(
            f"ffmpeg exited with code {result.returncode}",
            code="ffmpeg_error",
            stderr_tail=(result.stderr or "")[-_STDERR_TAIL:],
            returncode=result.returncode,
        )


def run_ffmpeg_pipe(args: list, *, stderr=subprocess.PIPE) -> subprocess.Popen:
    """Spawn ffmpeg reading from stdin (for streaming frames). Caller feeds stdin."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FfmpegError("ffmpeg binary not found", code="ffmpeg_not_found")
    cmd = [ffmpeg, "-y", "-hide_banner", *args]
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            creationflags=_windows_hide_flags(),
        )
    except FileNotFoundError as exc:
        raise FfmpegError("ffmpeg binary not found", code="ffmpeg_not_found") from exc


def parse_fps(rate) -> float:
    """Parse ffprobe avg_frame_rate such as '30/1' or '30000/1001'."""
    if not rate:
        return 0.0
    try:
        text = str(rate)
        if "/" in text:
            num, den = text.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        return float(text)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_media(path: str, *, timeout: float = 30) -> dict:
    """Return {duration, width, height, fps, has_video, has_audio} for a file."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise FfmpegError("ffprobe binary not found", code="ffprobe_not_found")
    cmd = [ffprobe, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        result = subprocess.run(
            cmd, check=False, capture_output=True, text=True,
            timeout=timeout, stdin=subprocess.DEVNULL,
            creationflags=_windows_hide_flags(),
        )
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError("ffprobe timed out", code="timeout") from exc
    except FileNotFoundError as exc:
        raise FfmpegError("ffprobe binary not found", code="ffprobe_not_found") from exc
    if result.returncode != 0:
        raise FfmpegError(
            "ffprobe failed", code="ffmpeg_error",
            stderr_tail=(result.stderr or "")[-_STDERR_TAIL:],
            returncode=result.returncode,
        )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise FfmpegError("ffprobe returned invalid JSON", code="ffmpeg_error") from exc
    return _summarize_probe(data)


def _summarize_probe(data: dict) -> dict:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    width = int(vstream.get("width") or 0) if vstream else 0
    height = int(vstream.get("height") or 0) if vstream else 0
    fps = parse_fps(vstream.get("avg_frame_rate")) if vstream else 0.0
    duration = _extract_duration(fmt, vstream)
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_video": vstream is not None,
        "has_audio": astream is not None,
    }


def _extract_duration(fmt: dict, vstream) -> float:
    for source in (fmt, vstream or {}):
        raw = source.get("duration")
        if raw is None:
            continue
        try:
            value = float(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    return 0.0
