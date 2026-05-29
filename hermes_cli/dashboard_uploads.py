"""Dashboard chat upload routes."""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from hermes_cli.config import get_hermes_home

router = APIRouter()

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MEDIA_MIME_PREFIXES = ("image/", "video/", "audio/")
MEDIA_SUFFIXES = {
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".oga",
    ".ogg",
    ".png",
    ".wav",
    ".webm",
    ".webp",
}


def _safe_filename(raw_name: str) -> str:
    name = Path(raw_name or "upload.bin").name.strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    if not name or name in {".", ".."}:
        return "upload.bin"
    if len(name) <= 140:
        return name
    path = Path(name)
    suffix = path.suffix[:20]
    stem = path.stem[: max(1, 140 - len(suffix))]
    return f"{stem}{suffix}"


def _looks_like_media(filename: str, content_type: str) -> bool:
    if any(content_type.startswith(prefix) for prefix in MEDIA_MIME_PREFIXES):
        return True
    return Path(filename).suffix.lower() in MEDIA_SUFFIXES


@router.post("/api/chat/uploads")
async def upload_chat_media(request: Request) -> dict[str, object]:
    raw_filename = request.headers.get("x-hermes-filename", "upload.bin")
    filename = _safe_filename(raw_filename)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()

    if not _looks_like_media(filename, content_type):
        raise HTTPException(status_code=415, detail="Unsupported media type")

    upload_dir = (
        Path(get_hermes_home())
        / "dashboard-uploads"
        / datetime.now().strftime("%Y%m%d")
    )
    upload_dir.mkdir(parents=True, exist_ok=True)

    target = upload_dir / f"{datetime.now().strftime('%H%M%S')}_{secrets.token_hex(4)}_{filename}"
    temp_target = target.with_suffix(f"{target.suffix}.part")
    total = 0

    try:
        with temp_target.open("wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Upload too large")
                fh.write(chunk)

        if total == 0:
            raise HTTPException(status_code=400, detail="Empty upload")

        temp_target.replace(target)
    except Exception:
        temp_target.unlink(missing_ok=True)
        raise

    return {
        "path": str(target),
        "name": filename,
        "mime_type": content_type,
        "size": total,
    }
