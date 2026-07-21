"""Input resolution, SSRF-guarded download, temp tracking, and output paths.

Supported input references (uniform across all tools):
  * ``http://`` / ``https://`` URL  (public only; SSRF-checked per hop)
  * ``file:///abs/path`` URI
  * ``data:<mime>;base64,<payload>`` URI
  * absolute (or ``~``) local filesystem path

Output is always a local absolute path under ``$HERMES_HOME/cache/videos`` (or
``settings.output_dir``); the caller never chooses the path (no arbitrary-write
surface, SEC-07).
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from tools.url_safety import is_safe_url

logger = logging.getLogger(__name__)


class InputError(Exception):
    """Raised for any input resolution failure, tagged with a machine code."""

    def __init__(self, message, *, code="input_not_found", hint=""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class TempTracker:
    """Track temp files/dirs for best-effort cleanup across all exit paths."""

    def __init__(self):
        self._files: list[Path] = []
        self._dirs: list[Path] = []

    def add_file(self, p: Path) -> Path:
        self._files.append(p)
        return p

    def add_dir(self, p: Path) -> Path:
        self._dirs.append(p)
        return p

    def mkdtemp(self, prefix="vpp_") -> Path:
        d = Path(tempfile.mkdtemp(prefix=prefix))
        self._dirs.append(d)
        return d

    def cleanup(self) -> None:
        for f in self._files:
            with contextlib.suppress(OSError):
                if f.exists():
                    f.unlink()
        for d in self._dirs:
            with contextlib.suppress(OSError):
                shutil.rmtree(d, ignore_errors=True)

    def __enter__(self) -> "TempTracker":
        return self

    def __exit__(self, *exc) -> bool:
        self.cleanup()
        return False


def _safe_url_or_raise(url: str) -> None:
    if not is_safe_url(url):
        raise InputError(
            f"URL blocked by SSRF policy: {url}", code="ssrf_blocked",
            hint="Only public http(s) URLs are allowed.",
        )


def _redirect_guard(response) -> None:
    # httpx fires this for the initial response AND every redirect target,
    # giving per-hop SSRF re-validation (mirrors tools/vision_tools.py).
    _safe_url_or_raise(str(response.url))


def download_to_temp(url: str, *, tracker: TempTracker, max_bytes: int,
                     timeout_sec: float, suffix="") -> Path:
    """Streaming, SSRF-guarded download of a public URL to a temp file."""
    _safe_url_or_raise(url)
    tmp = Path(tempfile.NamedTemporaryFile(prefix="vpp_dl_", suffix=suffix, delete=False).name)
    tracker.add_file(tmp)
    downloaded = 0
    try:
        with httpx.Client(
            follow_redirects=True,
            event_hooks={"response": [_redirect_guard]},
            timeout=httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=15.0),
            headers={"User-Agent": "hermes-video-post-tools/0.1"},
        ) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(65536):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise InputError(
                                f"Download exceeds {max_bytes} bytes limit",
                                code="download_failed",
                                hint="Reduce input size or raise max_download_bytes.",
                            )
                        fh.write(chunk)
    except InputError:
        raise
    except Exception as exc:
        raise InputError(f"Download failed: {exc}", code="download_failed") from exc
    return tmp


def _decode_data_uri(ref: str, *, tracker: TempTracker, max_bytes: int) -> Path:
    try:
        header, payload = ref.split(",", 1)
    except ValueError as exc:
        raise InputError("Malformed data URI", code="invalid_args") from exc
    if ";base64" not in header.lower():
        raise InputError("Only base64 data URIs are supported", code="invalid_args")
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise InputError("Invalid base64 in data URI", code="invalid_args") from exc
    if len(raw) > max_bytes:
        raise InputError("data URI exceeds size limit", code="download_failed")
    ext = _ext_from_mime(header)
    tmp = Path(tempfile.NamedTemporaryFile(prefix="vpp_data_", suffix=ext, delete=False).name)
    tracker.add_file(tmp)
    with open(tmp, "wb") as fh:
        fh.write(raw)
    return tmp


def _ext_from_mime(header: str) -> str:
    mime = header[5:].split(";")[0].strip().lower()  # strip leading "data:"
    if "/" in mime:
        sub = mime.split("/", 1)[1].split("+")[0]
        if sub:
            return "." + sub
    return ".bin"


def resolve_input(ref: str, *, tracker: TempTracker, max_bytes: int,
                  download_timeout: float, suffix="") -> Path:
    """Resolve any supported reference to an existing local file path."""
    if not isinstance(ref, str) or not ref.strip():
        raise InputError("Input reference must be a non-empty string", code="invalid_args")
    ref = ref.strip()
    if ref.startswith("http://") or ref.startswith("https://"):
        return download_to_temp(ref, tracker=tracker, max_bytes=max_bytes,
                                timeout_sec=download_timeout, suffix=suffix)
    if ref.startswith("data:"):
        return _decode_data_uri(ref, tracker=tracker, max_bytes=max_bytes)
    if ref.startswith("file://"):
        local = unquote(urlparse(ref).path)
    else:
        local = ref
    path = Path(local).expanduser()
    if not path.is_file():
        raise InputError(f"Input file not found: {path}", code="input_not_found")
    return path


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def output_path(tool: str, ext: str = "mp4", output_dir: str | None = None) -> Path:
    base = Path(output_dir).expanduser() if output_dir else _hermes_home() / "cache" / "videos"
    base.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    return base / f"vpp_{tool}_{ts}_{uuid.uuid4().hex[:8]}.{ext}"


def free_space_ok(path: Path, need_bytes: int) -> bool:
    target = path.parent if path.parent.exists() else path
    try:
        return shutil.disk_usage(str(target)).free > need_bytes
    except OSError:
        return True  # cannot determine; do not block
