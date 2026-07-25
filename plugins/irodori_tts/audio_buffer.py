from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from hermes_constants import get_hermes_home

_DEFAULT_BUFFER_DIR = get_hermes_home() / "workspace" / "irodori-tts" / "audio-buffer"
DEFAULT_MAX_FILES = int(os.environ.get("HERMES_IRODORI_BUFFER_MAX_FILES", "100"))
DEFAULT_MAX_BYTES = int(os.environ.get("HERMES_IRODORI_BUFFER_MAX_BYTES", str(200 * 1024 * 1024)))
DEFAULT_MAX_AGE_SECONDS = int(os.environ.get("HERMES_IRODORI_BUFFER_MAX_AGE", "3600"))


def _buffer_dir_from_config() -> Path:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return _DEFAULT_BUFFER_DIR
    tts = config.get("tts") if isinstance(config, dict) else {}
    if not isinstance(tts, dict):
        return _DEFAULT_BUFFER_DIR
    iro = tts.get("irodori") if isinstance(tts, dict) else {}
    if not isinstance(iro, dict):
        return _DEFAULT_BUFFER_DIR
    raw = iro.get("audio_buffer_dir") or os.environ.get("HERMES_IRODORI_BUFFER_DIR")
    if raw:
        return Path(str(raw)).expanduser()
    return _DEFAULT_BUFFER_DIR


def _buffer_int_config(key: str, fallback: int) -> int:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return fallback
    tts = config.get("tts") if isinstance(config, dict) else {}
    iro = tts.get("irodori") if isinstance(tts, dict) else {}
    if not isinstance(iro, dict):
        return fallback
    raw = iro.get(key) or os.environ.get(f"HERMES_IRODORI_{key.upper()}")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return fallback


class AudioBuffer:
    def __init__(
        self,
        buffer_dir: Path | str | None = None,
        max_files: int | None = None,
        max_bytes: int | None = None,
        max_age_seconds: int | None = None,
    ) -> None:
        self.buffer_dir = Path(buffer_dir) if buffer_dir else _buffer_dir_from_config()
        self.max_files = (
            max_files
            if max_files is not None
            else _buffer_int_config("max_files", DEFAULT_MAX_FILES)
        )
        self.max_bytes = (
            max_bytes
            if max_bytes is not None
            else _buffer_int_config("max_bytes", DEFAULT_MAX_BYTES)
        )
        self.max_age_seconds = (
            max_age_seconds
            if max_age_seconds is not None
            else _buffer_int_config("max_age_seconds", DEFAULT_MAX_AGE_SECONDS)
        )
        self.buffer_dir.mkdir(parents=True, exist_ok=True)

    def _buffer_files(self) -> list[Path]:
        return sorted(self.buffer_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime)

    def _total_size(self) -> int:
        return sum(p.stat().st_size for p in self._buffer_files())

    def add(self, path: Path | str) -> Path | None:
        src = Path(path)
        if not src.exists() or not src.is_file() or not src.suffix.lower() == ".wav":
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        dest = self.buffer_dir / f"{stamp}_{src.name}"
        shutil.copy2(src, dest)
        return dest

    def maybe_zip(self) -> dict[str, Any]:
        files = self._buffer_files()
        if not files:
            return {"zipped": False, "files": 0, "bytes": 0}
        before_zip_bytes = self._total_size()
        if len(files) < self.max_files and before_zip_bytes < self.max_bytes:
            return {"zipped": False, "files": len(files), "bytes": before_zip_bytes}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        zip_path = self.buffer_dir / f"audio-{stamp}.zip"
        with ZipFile(zip_path, "w", compression=8) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
        try:
            for path in files:
                path.unlink()
        except Exception:
            pass
        return {
            "zipped": True,
            "zip_path": str(zip_path),
            "files": len(files),
            "bytes": before_zip_bytes,
            "cleaned": True,
        }


_buffer = AudioBuffer()


def buffer_dir() -> Path:
    return _buffer.buffer_dir


def add_audio(path: Path | str) -> Path | None:
    return _buffer.add(path)


def maybe_zip(buffer_dir: Path | str | None = None) -> dict[str, Any]:
    if buffer_dir:
        target = AudioBuffer(buffer_dir=buffer_dir)
        return target.maybe_zip()
    return _buffer.maybe_zip()


def buffer_status(buffer_dir: Path | str | None = None) -> dict[str, Any]:
    if buffer_dir:
        target = AudioBuffer(buffer_dir=buffer_dir)
    else:
        target = _buffer
    files = target._buffer_files()
    return {
        "buffer_dir": str(target.buffer_dir),
        "files": len(files),
        "bytes": target._total_size(),
        "max_files": target.max_files,
        "max_bytes": target.max_bytes,
    }
