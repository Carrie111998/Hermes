"""html_to_video handler: render an HTML document into an MP4.

Playwright is imported LAZILY inside the handler so the plugin loads even when
the browser stack is absent; a missing browser surfaces as a runtime
``browser_not_available`` tool error (with an install hint), never as an import
failure that would hide the tool. Frames are captured in real time and streamed
into an ffmpeg ``image2pipe`` encoder.

Security: ``source`` http(s) references are resolved through the shared
SSRF-guarded ``resolve_input`` (the browser only ever opens a local ``file://``
URI). Subprocess args are always a list; user content never enters a filtergraph.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path

from tools.registry import tool_error

from .ffmpeg import FfmpegError, probe_media, run_ffmpeg_pipe
from .media_io import InputError, TempTracker, output_path
from .settings import clamp_timeout, load_settings
from .tools import _cleanup_partial, _ensure_nonempty, _err, _resolve, _success

logger = logging.getLogger(__name__)

_BROWSER_HINT = "pip install playwright && playwright install chromium"


class _BrowserError(Exception):
    """Raised when headless-browser capture fails (distinct from ffmpeg errors)."""


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def handle_html_to_video(args: dict, **kwargs) -> str:
    del kwargs
    tool = "html_to_video"
    html = args.get("html")
    source = args.get("source")
    if bool(html) == bool(source):
        return tool_error("Provide exactly one of html or source",
                          success=False, code="invalid_args", tool=tool)
    width = _int(args.get("width"), 1280)
    height = _int(args.get("height"), 720)
    fps = _int(args.get("fps"), 30)
    duration = _float(args.get("duration_sec"), 5.0)
    settle = _float(args.get("settle_sec"), 1.0)
    dsf = _int(args.get("device_scale_factor"), 1)
    settings = load_settings()
    timeout = clamp_timeout(args.get("timeout_sec"), settings, fallback=900)

    # Lazy import: keep the tool visible even without the browser stack.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return tool_error("headless browser not installed", success=False,
                          code="browser_not_available", tool=tool, hint=_BROWSER_HINT)

    out = None
    try:
        with TempTracker() as tracker:
            page_url = _resolve_page_url(html, source, tracker, settings)
            out = output_path(tool, output_dir=settings.output_dir)
            n_frames = max(1, round(duration * fps))
            interval_ms = int(1000 / fps) if fps else 33
            console_errors = _capture_and_encode(
                sync_playwright, page_url, html, width, height, fps, n_frames,
                interval_ms, settle, dsf, out, tracker, timeout)
            info = probe_media(str(out))
            _ensure_nonempty(info, out, tool)
            return _success(tool, out, info, console_errors=console_errors)
    except _BrowserError as exc:
        _cleanup_partial(out)
        return tool_error(str(exc), success=False, code="browser_not_available",
                          tool=tool, hint=_BROWSER_HINT)
    except (InputError, FfmpegError) as exc:
        _cleanup_partial(out)
        return _err(exc, tool)
    except Exception as exc:  # handler boundary: never raise
        _cleanup_partial(out)
        logger.exception("html_to_video unexpected failure")
        return tool_error(str(exc), success=False, code="internal", tool=tool)


# ---------------------------------------------------------------------------
# Capture + encode
# ---------------------------------------------------------------------------

def _capture_and_encode(sync_playwright, page_url, html, width, height, fps,
                        n_frames, interval_ms, settle, dsf, out, tracker, timeout):
    """Stream browser screenshots into an ffmpeg image2pipe encoder."""
    err_path = tracker.mkdtemp(prefix="vpp_htmlerr_") / "stderr.txt"
    with open(err_path, "wb") as err_fh:
        proc = run_ffmpeg_pipe(
            ["-f", "image2pipe", "-framerate", str(fps), "-i", "pipe:0",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "medium",
             "-crf", "20", "-r", str(fps), "-movflags", "+faststart", str(out)],
            stderr=err_fh,
        )
        browser_error = None
        encoded_ok = False
        console_errors = 0
        try:
            with sync_playwright() as pw:
                console_errors = _run_browser_capture(
                    pw, page_url, html, width, height, n_frames, interval_ms,
                    settle, dsf, proc)
            _finalize_encoder(proc, err_path, timeout)
            encoded_ok = True
        except _BrowserError as exc:
            browser_error = exc
        finally:
            if not encoded_ok:
                _abort_encoder(proc)
        if browser_error is not None:
            raise browser_error
        return console_errors


def _run_browser_capture(pw, page_url, html, width, height, n_frames,
                         interval_ms, settle, dsf, proc) -> int:
    """Launch the browser, capture frames, write each PNG to the encoder pipe."""
    try:
        browser = pw.chromium.launch(timeout=30000)
    except Exception as exc:
        raise _BrowserError(f"failed to launch chromium: {exc}") from exc
    errors: list = []
    try:
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=dsf)
        page.on("console", lambda msg: errors.append(msg.type))
        _load_page(page, page_url, html)
        page.wait_for_timeout(int(settle * 1000))
        for _ in range(n_frames):
            png = page.screenshot(type="png")
            try:
                proc.stdin.write(png)
            except BrokenPipeError as exc:
                raise FfmpegError("ffmpeg encoder exited early",
                                  code="ffmpeg_error") from exc
            page.wait_for_timeout(interval_ms)
    finally:
        with contextlib.suppress(Exception):
            browser.close()
    return sum(1 for t in errors if t == "error")


def _load_page(page, page_url, html) -> None:
    if html is not None:
        page.set_content(html, wait_until="networkidle")
    else:
        page.goto(page_url, wait_until="networkidle")


def _finalize_encoder(proc, err_path: Path, timeout: float) -> None:
    """Close stdin, wait for the encoder, raise FfmpegError on failure."""
    with contextlib.suppress(Exception):
        proc.stdin.close()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise FfmpegError("ffmpeg encoding timed out", code="timeout") from exc
    if proc.returncode != 0:
        raise FfmpegError(f"ffmpeg encoder exited with code {proc.returncode}",
                          code="ffmpeg_error", stderr_tail=_read_tail(err_path),
                          returncode=proc.returncode)


def _abort_encoder(proc) -> None:
    """Best-effort cleanup of a still-running encoder (never raises)."""
    with contextlib.suppress(Exception):
        proc.stdin.close()
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _resolve_page_url(html, source, tracker, settings):
    """Return the URL to open, or None to use page.set_content(html)."""
    if html is not None:
        return None
    local = _resolve(source, tracker, settings)
    return local.as_uri()


def _read_tail(err_path: Path) -> str:
    with contextlib.suppress(OSError):
        if err_path.exists():
            return err_path.read_text(encoding="utf-8", errors="ignore")[-2000:]
    return ""


def _int(value, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _float(value, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)
