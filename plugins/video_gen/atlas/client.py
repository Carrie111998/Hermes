"""HTTP and payload helpers for the AtlasCloud video backend."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import unquote, urlparse

import httpx

from .catalog import (
    VALID_ASPECT_RATIOS,
    clamp_duration,
    normalize_resolution,
)

DEFAULT_API_BASE = "https://api.atlascloud.ai/v1"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_POLL_INTERVAL_SECONDS = 5
MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024

_COMPLETE_STATUSES = {"completed", "succeeded", "success", "done"}
_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "expired"}
_INPUT_DATA_URI_RE = re.compile(
    r"^data:(?P<mime>image/(?:png|jpe?g|webp|gif));base64,(?P<data>.+)$",
    re.I | re.S,
)
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
        "User-Agent": "hermes-agent/video_gen_atlas",
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


def _local_file_from_url(value: str) -> Optional[Path]:
    parsed = urlparse(value)
    if parsed.scheme != "file":
        return None
    if parsed.netloc and parsed.netloc != "localhost":
        raise ValueError("file:// image inputs must point to local files")
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
            f"local image input is too large; max {MAX_LOCAL_IMAGE_BYTES} bytes"
        )
    mime = _sniff_image_mime(path)
    if mime not in _SUPPORTED_LOCAL_IMAGE_MIMES:
        raise ValueError("local image inputs must be PNG, JPEG, WebP, or GIF files")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _validate_data_image_uri(value: str) -> str:
    match = _INPUT_DATA_URI_RE.match(value)
    if not match:
        raise ValueError(
            "image data URIs must be PNG, JPEG, WebP, or GIF base64 images"
        )
    try:
        decoded = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image data URI must contain valid base64") from exc
    if len(decoded) > MAX_LOCAL_IMAGE_BYTES:
        raise ValueError(
            f"image data URI is too large; max {MAX_LOCAL_IMAGE_BYTES} bytes"
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
            "image_url must be an HTTP(S) URL, data URI, or readable local file path"
        )
    return _image_to_data_uri(path)


def build_payload(
    family: Dict[str, Any],
    *,
    atlas_model: str,
    prompt: str,
    image_url: Optional[str],
    duration: Optional[int],
    aspect_ratio: str,
    resolution: str,
    audio: Optional[bool],
    seed: Optional[int],
    negative_prompt: Optional[str] = None,
    reference_image_urls: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": atlas_model,
        "prompt": prompt,
        "duration": clamp_duration(family, duration),
        "resolution": normalize_resolution(family, resolution),
        "enable_sync_mode": False,
    }
    if image_url:
        payload["image"] = normalize_image_input(image_url)
    else:
        ratio = (aspect_ratio or "").strip()
        if ratio in VALID_ASPECT_RATIOS:
            payload["aspect_ratio"] = ratio
    refs = [
        normalize_image_input(item)
        for item in reference_image_urls or []
        if isinstance(item, str) and item.strip()
    ]
    if refs:
        payload["reference_images"] = refs
    if seed is not None:
        payload["seed"] = seed
    if family.get("negative") and negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if family.get("audio") and audio is not None:
        payload[str(family.get("audio_param") or "audio")] = bool(audio)
    return payload


def extract_prediction_id(body: Dict[str, Any]) -> Optional[str]:
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for container in (data, body):
        for key in ("id", "prediction_id", "predictionId", "task_id", "taskId"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _looks_like_media_output(value: str) -> bool:
    if value.startswith("data:"):
        return True
    if "/api/v1/model/prediction/" in value:
        return False
    if value.startswith(("http://", "https://")):
        return True
    return False


def _candidate_outputs(value: Any, *, allow_url: bool = False) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, dict):
        outputs: list[str] = []
        for key in ("video", "output"):
            outputs.extend(_candidate_outputs(value.get(key), allow_url=True))
        if "url" in value and (
            allow_url or any(k in value for k in ("content_type", "file_size"))
        ):
            outputs.extend(_candidate_outputs(value.get("url"), allow_url=True))
        urls = value.get("urls")
        if isinstance(urls, dict):
            for item in urls.values():
                outputs.extend(_candidate_outputs(item, allow_url=True))
        elif isinstance(urls, list):
            outputs.extend(_candidate_outputs(urls, allow_url=True))
        outputs.extend(_candidate_outputs(value.get("outputs"), allow_url=True))
        return outputs
    if isinstance(value, list):
        outputs = []
        for item in value:
            outputs.extend(_candidate_outputs(item, allow_url=allow_url))
        return outputs
    return []


def first_output_url(body: Dict[str, Any]) -> Optional[str]:
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    for item in _candidate_outputs(data):
        if _looks_like_media_output(item):
            return item
    return None


async def submit(
    client: httpx.AsyncClient,
    payload: Dict[str, Any],
    *,
    api_key: str,
    api_root: str,
) -> str:
    response = await client.post(
        f"{api_root}/api/v1/model/generateVideo",
        headers=headers(api_key),
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    prediction_id = extract_prediction_id(response.json())
    if not prediction_id:
        raise RuntimeError("Atlas response did not include prediction id")
    return prediction_id


async def poll(
    client: httpx.AsyncClient,
    prediction_id: str,
    *,
    api_key: str,
    api_root: str,
    timeout_seconds: int,
    poll_interval: int,
) -> Dict[str, Any]:
    elapsed = 0
    last_body: Dict[str, Any] = {}
    while elapsed <= timeout_seconds:
        response = await client.get(
            f"{api_root}/api/v1/model/prediction/{prediction_id}",
            headers=headers(api_key),
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        last_body = data if isinstance(data, dict) else body
        status = str(last_body.get("status") or "").lower()

        if status in _COMPLETE_STATUSES:
            return {"status": "completed", "body": last_body}
        if status in _FAILED_STATUSES:
            return {"status": status or "failed", "body": last_body}

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return {"status": "timeout", "body": last_body}
