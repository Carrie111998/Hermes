"""NVIDIA NIM image generation backend.

Exposes NVIDIA NIM's hosted visual generative AI models (FLUX.1, FLUX.2, SD 3.5 Large,
Qwen-Image, Qwen-Image-Edit) via the OpenAI-compatible ``/v1/images/generations``
and ``/v1/images/edits`` endpoints as an :class:`ImageGenProvider` implementation.

Uses the OpenAI-compatible API at ``https://integrate.api.nvidia.com/v1`` (hosted)
or a custom self-hosted container URL specified by ``NVIDIA_BASE_URL``.
Requires ``NVIDIA_API_KEY`` env var (``nvapi-...`` from build.nvidia.com for hosted,
or any string for self-hosted).

Model selection (first hit wins):

1. ``NVIDIA_IMAGE_MODEL`` env var
2. ``image_gen.nvidia_nim.model`` in ``config.yaml``
3. ``image_gen.model`` in ``config.yaml``
4. :data:`DEFAULT_MODEL` — ``black-forest-labs/flux.1-schnell``
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.secret_scope import get_secret
from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    normalize_reference_images,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

_MODELS: Dict[str, Dict[str, Any]] = {
    "black-forest-labs/flux.1-schnell": {
        "display": "FLUX.1-schnell",
        "speed": "~4 steps (8-15s)",
        "strengths": "Fast, excellent quality — default for iteration",
        "price": "free credits",
        "api_model": "black-forest-labs/flux.1-schnell",
        "supports_editing": False,
    },
    "black-forest-labs/flux.1-dev": {
        "display": "FLUX.1-dev",
        "speed": "~20-50 steps (30-60s)",
        "strengths": "Maximum quality, best prompt adherence",
        "price": "free credits",
        "api_model": "black-forest-labs/flux.1-dev",
        "supports_editing": False,
    },
    "black-forest-labs/flux.2-klein-4b": {
        "display": "FLUX.2-klein-4B",
        "speed": "~4 steps (8-15s)",
        "strengths": "Fast, 4B params — lightweight",
        "price": "free credits",
        "api_model": "black-forest-labs/flux.2-klein-4b",
        "supports_editing": True,
    },
    "stabilityai/stable-diffusion-3.5-large": {
        "display": "Stable Diffusion 3.5 Large",
        "speed": "~50 steps (45-90s)",
        "strengths": "High quality, strong text rendering",
        "price": "free credits",
        "api_model": "stabilityai/stable-diffusion-3.5-large",
        "supports_editing": True,
    },
    "qwen/qwen-image": {
        "display": "Qwen-Image",
        "speed": "~20-50 steps (30-60s)",
        "strengths": "Multilingual text rendering (EN/CN), precise editing",
        "price": "free credits",
        "api_model": "qwen/qwen-image",
        "supports_editing": False,
    },
    "qwen/qwen-image-2512": {
        "display": "Qwen-Image-2512",
        "speed": "~20-50 steps (30-60s)",
        "strengths": "Updated Qwen-Image, better quality",
        "price": "free credits",
        "api_model": "qwen/qwen-image-2512",
        "supports_editing": False,
    },
    "qwen/qwen-image-edit": {
        "display": "Qwen-Image-Edit",
        "speed": "~20-50 steps (30-60s)",
        "strengths": "Image-to-image editing, style transfer, object add/remove",
        "price": "free credits",
        "api_model": "qwen/qwen-image-edit",
        "supports_editing": True,
    },
}

DEFAULT_MODEL = "black-forest-labs/flux.1-schnell"

_SIZES = {
    "landscape": "1536x1024",
    "square": "1024x1024",
    "portrait": "1024x1536",
}


def _load_nvidia_config() -> Dict[str, Any]:
    """Read ``image_gen`` from config.yaml."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:
        logger.debug("Could not load image_gen config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which model to use and return ``(model_id, meta)``."""
    env_override = os.environ.get("NVIDIA_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_nvidia_config()
    nvidia_cfg = (
        cfg.get("nvidia_nim") if isinstance(cfg.get("nvidia_nim"), dict) else {}
    )
    candidate: Optional[str] = None
    if isinstance(nvidia_cfg, dict):
        value = nvidia_cfg.get("model")
        if isinstance(value, str) and value in _MODELS:
            candidate = value
    if candidate is None:
        top = cfg.get("model")
        if isinstance(top, str) and top in _MODELS:
            candidate = top

    if candidate is not None:
        return candidate, _MODELS[candidate]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _load_image_bytes(ref: str) -> Tuple[bytes, str]:
    """Load image bytes from a URL, data URI, or local file path."""
    ref = ref.strip()
    lower = ref.lower()
    if lower.startswith(("http://", "https://")):
        import requests

        resp = requests.get(ref, timeout=60)
        resp.raise_for_status()
        name = ref.split("?", 1)[0].rsplit("/", 1)[-1] or "image.png"
        return resp.content, name
    if lower.startswith("data:"):
        import base64

        header, _, b64 = ref.partition(",")
        ext = "png"
        if "image/" in header:
            ext = header.split("image/", 1)[1].split(";", 1)[0] or "png"
        return base64.b64decode(b64), f"image.{ext}"

    from agent.file_safety import raise_if_read_blocked

    raise_if_read_blocked(ref)
    with open(ref, "rb") as fh:
        data = fh.read()
    name = os.path.basename(ref) or "image.png"
    return data, name


class NvidiaNimImageGenProvider(ImageGenProvider):
    """NVIDIA NIM ``images.generate`` / ``images.edit`` backend."""

    @property
    def name(self) -> str:
        return "nvidia-nim"

    @property
    def display_name(self) -> str:
        return "NVIDIA NIM"

    def is_available(self) -> bool:
        if not get_secret("NVIDIA_API_KEY"):
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
            }
            for model_id, meta in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "NVIDIA NIM",
            "badge": "free credits",
            "tag": "FLUX, Qwen-Image, SD3.5 via NVIDIA NIM (hosted or self-hosted)",
            "env_vars": [
                {
                    "key": "NVIDIA_API_KEY",
                    "prompt": "NVIDIA API key (nvapi-... from build.nvidia.com)",
                    "url": "https://build.nvidia.com",
                },
                {
                    "key": "NVIDIA_BASE_URL",
                    "prompt": "Base URL (default: https://integrate.api.nvidia.com/v1)",
                    "url": "https://docs.nvidia.com/nim/visual-genai/latest/getting-started.html",
                    "optional": True,
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text", "image"], "max_reference_images": 16}

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="nvidia-nim",
                aspect_ratio=aspect,
            )

        api_key = get_secret("NVIDIA_API_KEY")
        if not api_key:
            return error_response(
                error=(
                    "NVIDIA_API_KEY not set. Run `hermes tools` → Image "
                    "Generation → NVIDIA NIM to configure, or `hermes setup` "
                    "to add the key. Get a free key at https://build.nvidia.com"
                ),
                error_type="auth_required",
                provider="nvidia-nim",
                aspect_ratio=aspect,
            )

        try:
            import openai
        except ImportError:
            return error_response(
                error="openai Python package not installed (pip install openai)",
                error_type="missing_dependency",
                provider="nvidia-nim",
                aspect_ratio=aspect,
            )

        model_id, meta = _resolve_model()
        api_model = meta["api_model"]
        size = _SIZES.get(aspect, _SIZES["square"])
        supports_editing = meta.get("supports_editing", False)

        sources: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            sources.append(image_url.strip())
        for ref in normalize_reference_images(reference_image_urls) or []:
            sources.append(ref)
        sources = sources[:16]
        is_edit = bool(sources)
        modality = "image" if is_edit else "text"

        if is_edit and not supports_editing:
            return error_response(
                error=(
                    f"Model {model_id} does not support image editing. "
                    f"Use a model with editing support: "
                    f"black-forest-labs/flux.2-klein-4b, "
                    f"stabilityai/stable-diffusion-3.5-large, "
                    f"qwen/qwen-image-edit"
                ),
                error_type="model_mismatch",
                provider="nvidia-nim",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        base_url = (
            os.environ.get("NVIDIA_BASE_URL") or "https://integrate.api.nvidia.com/v1"
        )

        client = openai.OpenAI(api_key=api_key, base_url=base_url)

        if is_edit:
            import io

            try:
                files = []
                for ref in sources:
                    data, fname = _load_image_bytes(ref)
                    bio = io.BytesIO(data)
                    bio.name = fname
                    files.append(bio)
            except Exception as exc:
                return error_response(
                    error=f"Could not load source image for editing: {exc}",
                    error_type="io_error",
                    provider="nvidia-nim",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

            try:
                response = client.images.edit(
                    model=api_model,
                    image=files if len(files) > 1 else files[0],
                    prompt=prompt,
                    size=size,
                    n=1,
                    response_format="b64_json",
                )
            except Exception as exc:
                logger.debug("NVIDIA NIM image edit failed", exc_info=True)
                return error_response(
                    error=f"NVIDIA NIM image editing failed: {exc}",
                    error_type="api_error",
                    provider="nvidia-nim",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
        else:
            payload: Dict[str, Any] = {
                "model": api_model,
                "prompt": prompt,
                "size": size,
                "n": 1,
                "response_format": "b64_json",
            }

            for opt in ("seed", "steps", "quality"):
                if opt in kwargs and kwargs[opt] is not None:
                    payload[opt] = kwargs[opt]

            try:
                response = client.images.generate(**payload)
            except Exception as exc:
                logger.debug("NVIDIA NIM image generation failed", exc_info=True)
                return error_response(
                    error=f"NVIDIA NIM image generation failed: {exc}",
                    error_type="api_error",
                    provider="nvidia-nim",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

        data = getattr(response, "data", None) or []
        if not data:
            return error_response(
                error="NVIDIA NIM returned no image data",
                error_type="empty_response",
                provider="nvidia-nim",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0]
        b64 = getattr(first, "b64_json", None)
        url = getattr(first, "url", None)
        revised_prompt = getattr(first, "revised_prompt", None)

        if b64:
            try:
                saved_path = save_b64_image(
                    b64, prefix=f"nvidia_{model_id.replace('/', '_')}"
                )
            except Exception as exc:
                return error_response(
                    error=f"Could not save image to cache: {exc}",
                    error_type="io_error",
                    provider="nvidia-nim",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        elif url:
            try:
                saved_path = save_url_image(
                    url, prefix=f"nvidia_{model_id.replace('/', '_')}"
                )
            except Exception as exc:
                logger.warning(
                    "NVIDIA NIM image URL %s could not be cached (%s); falling back to bare URL.",
                    url,
                    exc,
                )
                image_ref = url
            else:
                image_ref = str(saved_path)
        else:
            return error_response(
                error="NVIDIA NIM response contained neither b64_json nor URL",
                error_type="empty_response",
                provider="nvidia-nim",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": size}
        if revised_prompt:
            extra["revised_prompt"] = revised_prompt

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="nvidia-nim",
            modality=modality,
            extra=extra,
        )


def register(ctx) -> None:
    """Plugin entry point — wire ``NvidiaNimImageGenProvider`` into the registry."""
    ctx.register_image_gen_provider(NvidiaNimImageGenProvider())
