"""HTTP and response helpers for AtlasCloud image generation."""

from __future__ import annotations

import base64
import binascii
import os
import re
from typing import Any, Dict, Iterable, Optional, Tuple

import httpx

from agent.image_gen_provider import save_b64_image

DEFAULT_API_BASE = "https://api.atlascloud.ai/v1"
DEFAULT_TIMEOUT_SECONDS = 120

_ASPECT_RATIO_MAP = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
    "16:9": "16:9",
    "9:16": "9:16",
    "1:1": "1:1",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
    "4:5": "4:5",
    "5:4": "5:4",
}
_DATA_URI_RE = re.compile(r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.S)
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


def resolve_credentials() -> Tuple[str, str]:
    key = (
        os.environ.get("ATLAS_API_KEY")
        or os.environ.get("LLM_API_KEY")
        or ""
    ).strip()
    base = (
        os.environ.get("ATLAS_API_BASE")
        or os.environ.get("ATLAS_BASE_URL")
        or DEFAULT_API_BASE
    ).strip().rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    return key, root


def headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "hermes-agent/image_gen_atlas",
    }


def normalize_aspect_ratio(value: str) -> str:
    return _ASPECT_RATIO_MAP.get((value or "").strip().lower(), "16:9")


def build_payload(
    *,
    atlas_model: str,
    prompt: str,
    aspect_ratio: str,
    output_format: str = "png",
    num_images: int = 1,
) -> Dict[str, Any]:
    return {
        "model": atlas_model,
        "prompt": prompt,
        "aspect_ratio": normalize_aspect_ratio(aspect_ratio),
        "output_format": output_format,
        "enable_sync_mode": True,
        "num_images": num_images,
    }


def generate_image(
    payload: Dict[str, Any],
    *,
    api_key: str,
    api_root: str,
) -> Dict[str, Any]:
    response = httpx.post(
        f"{api_root}/api/v1/model/generateImage",
        headers=headers(api_key),
        json=payload,
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def _candidate_outputs(value: Any, *, allow_url: bool = False) -> Iterable[str]:
    if isinstance(value, str) and value.strip():
        yield value.strip()
        return

    if isinstance(value, list):
        for item in value:
            yield from _candidate_outputs(item, allow_url=allow_url)
        return

    if not isinstance(value, dict):
        return

    for key in ("image", "b64_json", "base64", "output"):
        yield from _candidate_outputs(value.get(key), allow_url=True)
    if "url" in value and allow_url:
        yield from _candidate_outputs(value.get("url"), allow_url=True)

    urls = value.get("urls")
    if isinstance(urls, dict):
        for item in urls.values():
            yield from _candidate_outputs(item, allow_url=True)
    else:
        yield from _candidate_outputs(urls, allow_url=True)

    yield from _candidate_outputs(value.get("outputs"), allow_url=True)
    yield from _candidate_outputs(value.get("data"), allow_url=allow_url)


def first_output(body: Dict[str, Any]) -> Optional[str]:
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    for item in _candidate_outputs(data):
        if "/api/v1/model/prediction/" in item:
            continue
        if item.startswith(("http://", "https://", "data:image/")):
            return item
        if _looks_like_base64(item):
            return item
    return None


def materialize_output(value: str, *, model_id: str) -> str:
    raw = (value or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw

    match = _DATA_URI_RE.match(raw)
    if match:
        mime = match.group("mime").lower()
        extension = _EXT_BY_MIME.get(mime, "png")
        saved = save_b64_image(
            match.group("data"),
            prefix=f"atlas_{model_id}",
            extension=extension,
        )
        return str(saved)

    saved = save_b64_image(raw, prefix=f"atlas_{model_id}", extension="png")
    return str(saved)


def _looks_like_base64(value: str) -> bool:
    compact = "".join((value or "").split())
    if len(compact) < 16:
        return False
    try:
        base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True
