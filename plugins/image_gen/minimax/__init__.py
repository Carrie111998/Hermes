"""MiniMax text-to-image backends for the global and China APIs.

Both providers use the native ``/v1/image_generation`` endpoint and expose
the same ``image-01`` model catalog. The API returns generated content in
``data.image_urls``; the selected ``response_format`` determines whether the
first value is downloaded as a URL or decoded as base64.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    save_b64_image,
    save_url_image,
    success_response,
)

logger = logging.getLogger(__name__)


DEFAULT_MODEL = "image-01"
REQUEST_TIMEOUT_SECONDS = 120.0

_MODELS: Dict[str, Dict[str, str]] = {
    "image-01": {
        "display": "Image 01",
        "strengths": "Text-to-image generation",
    },
    "image-01-live": {
        "display": "Image 01 Live",
        "strengths": "Text-to-image generation",
    },
}

_PROVIDERS: Dict[str, Dict[str, str]] = {
    "minimax": {
        "display_name": "MiniMax",
        "api_key_env": "MINIMAX_API_KEY",
        "endpoint": "https://api.minimax.io/v1/image_generation",
        "docs_url": "https://platform.minimax.io/docs/api-reference/image-generation-t2i",
    },
    "minimax-cn": {
        "display_name": "MiniMax (China)",
        "api_key_env": "MINIMAX_CN_API_KEY",
        "endpoint": "https://api.minimaxi.com/v1/image_generation",
        "docs_url": "https://platform.minimaxi.com/docs/api-reference/image-generation-t2i",
    },
}

_ASPECT_RATIOS = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
}

_OUTPUT_FORMATS = {"url", "base64"}
_OPTIONAL_REQUEST_FIELDS = (
    "subject_reference",
    "width",
    "height",
    "seed",
    "prompt_optimizer",
)


def _resolve_model(candidate: Any) -> str:
    """Return a supported model id, falling back to the documented default."""
    if isinstance(candidate, str):
        normalized = candidate.strip()
        if normalized in _MODELS:
            return normalized
    return DEFAULT_MODEL


def _split_base64_image(value: str) -> tuple[str, str]:
    """Return raw base64 data and a cache-file extension."""
    extension = "png"
    if value.startswith("data:image/") and "," in value:
        header, value = value.split(",", 1)
        subtype = header.split("data:image/", 1)[1].split(";", 1)[0].lower()
        if subtype in {"jpeg", "jpg", "png", "webp", "gif"}:
            extension = "jpg" if subtype == "jpeg" else subtype
    return value, extension


class MiniMaxImageGenProvider(ImageGenProvider):
    """MiniMax native text-to-image provider for one API region."""

    def __init__(self, provider_name: str = "minimax") -> None:
        if provider_name not in _PROVIDERS:
            raise ValueError(f"Unsupported MiniMax image provider: {provider_name}")
        self._provider_name = provider_name
        self._provider_config = _PROVIDERS[provider_name]

    @property
    def name(self) -> str:
        return self._provider_name

    @property
    def display_name(self) -> str:
        return self._provider_config["display_name"]

    def is_available(self) -> bool:
        return bool(os.environ.get(self._provider_config["api_key_env"], "").strip())

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": model_id,
                "display": metadata["display"],
                "strengths": metadata["strengths"],
            }
            for model_id, metadata in _MODELS.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def capabilities(self) -> Dict[str, Any]:
        return {"modalities": ["text"], "max_reference_images": 0}

    def get_setup_schema(self) -> Dict[str, Any]:
        api_key_env = self._provider_config["api_key_env"]
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "image-01 and image-01-live text-to-image generation",
            "env_vars": [
                {
                    "key": api_key_env,
                    "prompt": f"{self.display_name} API key",
                    "url": self._provider_config["docs_url"],
                }
            ],
        }

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

        if image_url or reference_image_urls:
            return error_response(
                error=(
                    f"{self.display_name} is configured for text-to-image generation; "
                    "image_url and reference_image_urls are unsupported."
                ),
                error_type="modality_unsupported",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider=self.name,
                aspect_ratio=aspect,
            )

        api_key_env = self._provider_config["api_key_env"]
        api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            return error_response(
                error=(
                    f"{api_key_env} not set. Run `hermes tools` -> Image "
                    f"Generation -> {self.display_name} to configure it."
                ),
                error_type="auth_required",
                provider=self.name,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        model = _resolve_model(kwargs.get("model"))
        response_format = str(kwargs.get("response_format") or "url").strip().lower()
        if response_format not in _OUTPUT_FORMATS:
            return error_response(
                error="response_format must be 'url' or 'base64'",
                error_type="invalid_argument",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "aspect_ratio": _ASPECT_RATIOS[aspect],
            "response_format": response_format,
            "n": kwargs.get("n", 1),
        }
        for field in _OPTIONAL_REQUEST_FIELDS:
            value = kwargs.get(field)
            if value is not None:
                payload[field] = value

        try:
            response = requests.post(
                self._provider_config["endpoint"],
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.Timeout:
            return error_response(
                error=f"{self.display_name} image generation timed out",
                error_type="timeout",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.RequestException as exc:
            logger.debug("MiniMax image generation request failed", exc_info=True)
            return error_response(
                error=f"{self.display_name} image generation failed: {exc}",
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if response.status_code >= 400:
            message = ""
            try:
                error_body = response.json()
                if isinstance(error_body, dict):
                    base_resp = error_body.get("base_resp") or {}
                    error_value = error_body.get("error") or {}
                    message = (
                        error_body.get("message")
                        or (
                            base_resp.get("status_msg")
                            if isinstance(base_resp, dict)
                            else ""
                        )
                        or (
                            error_value.get("message")
                            if isinstance(error_value, dict)
                            else ""
                        )
                        or ""
                    )
            except ValueError:
                message = str(getattr(response, "text", ""))[:200]
            return error_response(
                error=(
                    f"{self.display_name} image generation HTTP "
                    f"{response.status_code}: {message or 'request failed'}"
                ),
                error_type="api_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            body = response.json()
        except ValueError:
            return error_response(
                error=f"{self.display_name} returned a non-JSON response",
                error_type="invalid_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if not isinstance(body, dict):
            return error_response(
                error=f"{self.display_name} returned an invalid response object",
                error_type="invalid_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        base_resp = body.get("base_resp") or {}
        status_code = (
            base_resp.get("status_code") if isinstance(base_resp, dict) else None
        )
        if status_code not in (None, 0):
            status_message = base_resp.get("status_msg") or "request failed"
            return error_response(
                error=f"{self.display_name} error {status_code}: {status_message}",
                error_type="provider_error",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        data = body.get("data") or {}
        image_values = data.get("image_urls") if isinstance(data, dict) else None
        if (
            not isinstance(image_values, list)
            or not image_values
            or not isinstance(image_values[0], str)
        ):
            return error_response(
                error=f"{self.display_name} returned no image data",
                error_type="empty_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        image_value = image_values[0].strip()
        if not image_value:
            return error_response(
                error=f"{self.display_name} returned an empty image value",
                error_type="empty_response",
                provider=self.name,
                model=model,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        prefix = f"minimax_{model}"
        if response_format == "base64":
            raw_base64, extension = _split_base64_image(image_value)
            try:
                image_ref = str(
                    save_b64_image(raw_base64, prefix=prefix, extension=extension)
                )
            except Exception as exc:  # noqa: BLE001
                return error_response(
                    error=f"Could not save generated image to cache: {exc}",
                    error_type="invalid_response",
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
        else:
            parsed = urlparse(image_value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return error_response(
                    error=f"{self.display_name} returned an invalid image URL",
                    error_type="invalid_response",
                    provider=self.name,
                    model=model,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            try:
                image_ref = str(save_url_image(image_value, prefix=prefix))
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "MiniMax image URL could not be cached; returning the URL: %s",
                    exc,
                )
                image_ref = image_value

        metadata = body.get("metadata") or {}
        extra: Dict[str, Any] = {"response_format": response_format}
        if isinstance(metadata, dict):
            for source_key, result_key in (
                ("success_count", "success_count"),
                ("failed_count", "failed_count"),
            ):
                if metadata.get(source_key) is not None:
                    extra[result_key] = metadata[source_key]

        return success_response(
            image=image_ref,
            model=model,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
            extra=extra,
        )


def register(ctx) -> None:
    """Register both regional MiniMax image providers."""
    ctx.register_image_gen_provider(MiniMaxImageGenProvider("minimax"))
    ctx.register_image_gen_provider(MiniMaxImageGenProvider("minimax-cn"))
