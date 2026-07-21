"""Optional runtime settings for the video_post tools.

Every setting is optional. The backend is local ffmpeg (no secrets), so the
tools are fully functional with zero configuration. Settings, when provided,
live under the top-level ``video_post:`` key in ``~/.hermes/config.yaml``::

    video_post:
      default_timeout_sec: 600
      ffmpeg_path: /opt/homebrew/bin/ffmpeg

Read failures fall back to defaults (logged) instead of raising, so config
noise can never hide the tools from the model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SEC = 600
_MAX_TIMEOUT_SEC = 1800
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # 512 MB
_DOWNLOAD_TIMEOUT_SEC = 300


@dataclass(frozen=True)
class Settings:
    default_timeout_sec: int = _DEFAULT_TIMEOUT_SEC
    max_timeout_sec: int = _MAX_TIMEOUT_SEC
    max_download_bytes: int = _MAX_DOWNLOAD_BYTES
    download_timeout_sec: int = _DOWNLOAD_TIMEOUT_SEC
    output_dir: str | None = None
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None


def _read_settings_dict() -> dict:
    try:
        from hermes_cli.config import load_config_readonly
    except Exception as exc:  # hermes not importable (bare unit test)
        logger.debug("load_config_readonly unavailable: %s", exc)
        return {}
    try:
        config = load_config_readonly()
    except Exception as exc:
        logger.warning("Failed to read hermes config: %s", exc)
        return {}
    section = config.get("video_post") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def _positive_int(d: dict, key: str, default: int) -> int:
    value = d.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if key in d:
            logger.warning("settings.%s invalid (%r); using default %s", key, value, default)
        return default
    return value


def _optional_str(d: dict, key: str) -> str | None:
    value = d.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def load_settings() -> Settings:
    d = _read_settings_dict()
    return Settings(
        default_timeout_sec=_positive_int(d, "default_timeout_sec", _DEFAULT_TIMEOUT_SEC),
        max_timeout_sec=_positive_int(d, "max_timeout_sec", _MAX_TIMEOUT_SEC),
        max_download_bytes=_positive_int(d, "max_download_bytes", _MAX_DOWNLOAD_BYTES),
        download_timeout_sec=_positive_int(d, "download_timeout_sec", _DOWNLOAD_TIMEOUT_SEC),
        output_dir=_optional_str(d, "output_dir"),
        ffmpeg_path=_optional_str(d, "ffmpeg_path"),
        ffprobe_path=_optional_str(d, "ffprobe_path"),
    )


def clamp_timeout(requested: object, settings: Settings, fallback: int | None = None) -> int:
    """Clamp a per-call timeout_sec into [10, settings.max_timeout_sec]."""
    default = fallback if fallback is not None else settings.default_timeout_sec
    if isinstance(requested, bool) or not isinstance(requested, (int, float)):
        return default
    return int(max(10.0, min(float(requested), float(settings.max_timeout_sec))))
