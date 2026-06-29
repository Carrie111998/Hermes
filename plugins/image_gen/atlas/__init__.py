"""AtlasCloud image generation backend."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from agent.image_gen_provider import (
    DEFAULT_ASPECT_RATIO,
    ImageGenProvider,
    error_response,
    resolve_aspect_ratio,
    success_response,
)

from . import client
from .catalog import DEFAULT_MODEL, list_model_rows, resolve_model

logger = logging.getLogger(__name__)


class AtlasImageGenProvider(ImageGenProvider):
    """AtlasCloud backend for Hermes' unified image_generate tool."""

    @property
    def name(self) -> str:
        return "atlas"

    @property
    def display_name(self) -> str:
        return "AtlasCloud"

    def is_available(self) -> bool:
        api_key, _ = client.resolve_credentials()
        return bool(api_key)

    def list_models(self) -> List[Dict[str, Any]]:
        return list_model_rows()

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "AtlasCloud",
            "badge": "paid",
            "tag": "Nano Banana text-to-image via ATLAS_API_KEY",
            "env_vars": [
                {
                    "key": "ATLAS_API_KEY",
                    "prompt": "AtlasCloud API key",
                    "url": "https://api.atlascloud.ai/",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)
        model_arg = kwargs.get("model") if isinstance(kwargs.get("model"), str) else None
        reference_image_urls = _normalize_reference_images(
            kwargs.get("reference_image_urls")
            or kwargs.get("reference_images")
            or kwargs.get("images")
        )
        seed = kwargs.get("seed") if isinstance(kwargs.get("seed"), int) else None
        model_id, atlas_model = resolve_model(model_arg, edit=bool(reference_image_urls))

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="atlas",
                model=model_id,
                aspect_ratio=aspect,
            )

        api_key, api_root = client.resolve_credentials()
        if not api_key:
            return error_response(
                error="ATLAS_API_KEY not set. Configure AtlasCloud before generating images.",
                error_type="auth_required",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            payload = client.build_payload(
                atlas_model=atlas_model,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                reference_image_urls=reference_image_urls,
                seed=seed,
            )
        except ValueError as exc:
            return error_response(
                error=str(exc),
                error_type="invalid_reference_image",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            body = client.generate_image(payload, api_key=api_key, api_root=api_root)
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = exc.response.text[:500]
            except Exception:
                pass
            status = getattr(exc.response, "status_code", 0)
            return error_response(
                error=f"Atlas API error ({status}): {detail or exc}",
                error_type="api_error",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except httpx.TimeoutException:
            return error_response(
                error="Atlas image generation timed out (120s)",
                error_type="timeout",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except httpx.RequestError as exc:
            return error_response(
                error=f"Atlas connection error: {exc}",
                error_type="connection_error",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except Exception as exc:
            logger.warning("Atlas image generation failed: %s", exc, exc_info=True)
            return error_response(
                error=f"Atlas image generation failed: {exc}",
                error_type="api_error",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        output = client.first_output(body)
        if not output:
            return error_response(
                error="Atlas image generation completed without an output image.",
                error_type="empty_response",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            image_ref = client.materialize_output(output, model_id=model_id)
        except Exception as exc:
            return error_response(
                error=f"Could not save Atlas image output: {exc}",
                error_type="io_error",
                provider="atlas",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="atlas",
            extra={
                "atlas_model": atlas_model,
                "atlas_aspect_ratio": payload["aspect_ratio"],
                "reference_image_count": len(reference_image_urls),
            },
        )


def _normalize_reference_images(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def register(ctx: Any) -> None:
    """Register this provider with the image gen registry."""
    ctx.register_image_gen_provider(AtlasImageGenProvider())
