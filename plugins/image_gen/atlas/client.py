"""HTTP and response helpers for AtlasCloud image generation."""

from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx

from agent.image_gen_provider import save_b64_image

DEFAULT_API_BASE = "https://api.atlascloud.ai/v1"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024

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
_INPUT_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>image/(?:png|jpe?g|webp|gif));base64,(?P<data>.+)$",
    re.I | re.S,
)
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_SUPPORTED_LOCAL_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "image/gif",
}


def _is_internal_dev(base: str) -> bool:
    env = (os.environ.get("ATLAS_INTERNAL_ENV") or "").strip().lower()
    return env in {"dev", "development", "internal-dev"} or "api.dev.atlascloud.ai" in base


def resolve_credentials() -> Tuple[str, str]:
    base = (
        os.environ.get("ATLAS_API_BASE")
        or os.environ.get("ATLAS_BASE_URL")
        or DEFAULT_API_BASE
    ).strip().rstrip("/")
    if _is_internal_dev(base):
        key = (
            os.environ.get("ATLAS_DEV_API_KEY")
            or os.environ.get("ATLAS_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or ""
        ).strip()
    else:
        key = (
            os.environ.get("ATLAS_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or ""
        ).strip()
    root = base[:-3] if base.endswith("/v1") else base
    return key, root


def headers(api_key: str) -> Dict[str, str]:
    values = {
        "Content-Type": "application/json",
        "User-Agent": "hermes-agent/image_gen_atlas",
    }
    extra_name = (os.environ.get("ATLAS_API_EXTRA_HEADER_NAME") or "").strip()
    extra_value = (os.environ.get("ATLAS_API_EXTRA_HEADER_VALUE") or "").strip()
    if extra_name and extra_value and extra_name.lower() not in {
        "authorization",
        "content-type",
        "user-agent",
    }:
        values[extra_name] = extra_value
    values["Authorization"] = f"Bearer {api_key}"
    return values


def normalize_aspect_ratio(value: str) -> str:
    return _ASPECT_RATIO_MAP.get((value or "").strip().lower(), "16:9")


def _local_file_from_url(value: str) -> Optional[Path]:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return None
    if parsed.netloc and parsed.netloc != "localhost":
        raise ValueError("file:// reference images must point to local files")
    return Path(unquote(parsed.path)).expanduser()


def _sniff_image_mime(path: Path) -> str:
    with path.open("rb") as fh:
        header = fh.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return ""


def _image_to_data_uri(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_LOCAL_IMAGE_BYTES:
        raise ValueError(
            f"local reference image is too large; max {MAX_LOCAL_IMAGE_BYTES} bytes"
        )
    mime = _sniff_image_mime(path)
    if mime not in _SUPPORTED_LOCAL_IMAGE_MIMES:
        raise ValueError(
            "local reference images must be PNG, JPEG, WebP, or GIF files"
        )
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _validate_data_image_uri(value: str) -> str:
    match = _INPUT_DATA_URI_RE.match(value)
    if not match:
        raise ValueError(
            "reference image data URIs must be PNG, JPEG, WebP, or GIF base64 images"
        )
    try:
        decoded = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("reference image data URI must contain valid base64") from exc
    if len(decoded) > MAX_LOCAL_IMAGE_BYTES:
        raise ValueError(
            f"reference image data URI is too large; max {MAX_LOCAL_IMAGE_BYTES} bytes"
        )
    return value


def normalize_image_input(value: str) -> str:
    raw = (value or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("data:"):
        return _validate_data_image_uri(raw)

    file_url_path = _local_file_from_url(raw)
    path = file_url_path or Path(raw).expanduser()
    if not path.is_file():
        raise ValueError(
            "reference_image_urls must contain HTTP(S) URLs, data image URIs, or readable local image paths"
        )
    return _image_to_data_uri(path)


def normalize_reference_images(value: Optional[Iterable[str]]) -> list[str]:
    refs: list[str] = []
    for item in value or []:
        if not isinstance(item, str) or not item.strip():
            continue
        refs.append(normalize_image_input(item))
    return refs


def build_payload(
    *,
    atlas_model: str,
    prompt: str,
    aspect_ratio: str,
    output_format: str = "png",
    num_images: int = 1,
    reference_image_urls: Optional[Iterable[str]] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": atlas_model,
        "prompt": prompt,
        "aspect_ratio": normalize_aspect_ratio(aspect_ratio),
        "output_format": output_format,
        "enable_sync_mode": True,
        "num_images": num_images,
    }
    refs = normalize_reference_images(reference_image_urls)
    if refs:
        payload["images"] = refs
    if seed is not None:
        payload["seed"] = seed
    return payload


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
