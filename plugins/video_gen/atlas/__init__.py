"""AtlasCloud video generation backend."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from agent.video_gen_provider import VideoGenProvider, error_response, success_response

from . import client
from .client import DEFAULT_POLL_INTERVAL_SECONDS, DEFAULT_TIMEOUT_SECONDS
from .catalog import (
    ATLAS_FAMILIES,
    DEFAULT_MODEL,
    DEFAULT_RESOLUTION,
    VALID_ASPECT_RATIOS,
    family_modalities,
    resolve_family_and_model,
)

logger = logging.getLogger(__name__)


class AtlasVideoGenProvider(VideoGenProvider):
    """AtlasCloud backend for Hermes' unified video_generate tool."""

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
        return [
            {
                "id": family_id,
                "display": meta["display"],
                "speed": meta["speed"],
                "strengths": meta["strengths"],
                "price": meta["price"],
                "modalities": family_modalities(meta),
            }
            for family_id, meta in ATLAS_FAMILIES.items()
        ]

    def default_model(self) -> Optional[str]:
        return DEFAULT_MODEL

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "AtlasCloud",
            "badge": "paid",
            "tag": "Wan 2.6, Seedance, Kling, Veo 3.1, Sora 2 via ATLAS_API_KEY",
            "env_vars": [
                {
                    "key": "ATLAS_API_KEY",
                    "prompt": "AtlasCloud API key",
                    "url": "https://api.atlascloud.ai/",
                },
            ],
        }

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "aspect_ratios": list(VALID_ASPECT_RATIOS),
            "resolutions": ["720p", "1080p", "1440p-sr"],
            "max_duration": 15,
            "min_duration": 3,
            "supports_audio": True,
            "supports_negative_prompt": True,
            "max_reference_images": 4,
        }

    def generate(
        self,
        prompt: str,
        *,
        model: Optional[str] = None,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        duration: Optional[int] = None,
        aspect_ratio: str = "16:9",
        resolution: str = DEFAULT_RESOLUTION,
        negative_prompt: Optional[str] = None,
        audio: Optional[bool] = None,
        seed: Optional[int] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        del kwargs
        try:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(
                    self._generate_async(
                        prompt=prompt,
                        model=model,
                        image_url=image_url,
                        reference_image_urls=reference_image_urls,
                        duration=duration,
                        aspect_ratio=aspect_ratio,
                        resolution=resolution,
                        audio=audio,
                        seed=seed,
                        negative_prompt=negative_prompt,
                    )
                )
            finally:
                loop.close()
        except Exception as exc:
            logger.warning("Atlas video generation failed: %s", exc, exc_info=True)
            return error_response(
                error=f"Atlas video generation failed: {exc}",
                error_type="api_error",
                provider="atlas",
                model=model or DEFAULT_MODEL,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )

    async def _generate_async(
        self,
        *,
        prompt: str,
        model: Optional[str],
        image_url: Optional[str],
        reference_image_urls: Optional[List[str]],
        duration: Optional[int],
        aspect_ratio: str,
        resolution: str,
        audio: Optional[bool],
        seed: Optional[int],
        negative_prompt: Optional[str],
    ) -> Dict[str, Any]:
        api_key, api_root = client.resolve_credentials()
        if not api_key:
            return error_response(
                error="ATLAS_API_KEY not set. Configure AtlasCloud before generating video.",
                error_type="auth_required",
                provider="atlas",
                prompt=prompt,
            )

        prompt = (prompt or "").strip()
        if not prompt:
            return error_response(
                error="prompt is required.",
                error_type="missing_prompt",
                provider="atlas",
                prompt=prompt,
            )
        if reference_image_urls and len(reference_image_urls) > 4:
            return error_response(
                error="Atlas video_generate supports at most 4 reference_image_urls.",
                error_type="too_many_references",
                provider="atlas",
                prompt=prompt,
            )

        image_url_norm = (image_url or "").strip() or None
        modality = "image" if image_url_norm else "text"
        family_id, family, atlas_model, model_error = resolve_family_and_model(
            model,
            modality=modality,
        )
        if model_error:
            return error_response(
                error=model_error,
                error_type="modality_unsupported",
                provider="atlas",
                model=model or family_id,
                prompt=prompt,
            )

        try:
            payload = client.build_payload(
                family,
                atlas_model=atlas_model,
                prompt=prompt,
                image_url=image_url_norm,
                duration=duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                audio=audio,
                seed=seed,
                negative_prompt=negative_prompt,
                reference_image_urls=reference_image_urls,
            )
        except ValueError as exc:
            return error_response(
                error=str(exc),
                error_type="invalid_image_url",
                provider="atlas",
                model=family_id,
                prompt=prompt,
            )

        async with httpx.AsyncClient() as http:
            try:
                prediction_id = await client.submit(
                    http,
                    payload,
                    api_key=api_key,
                    api_root=api_root,
                )
                poll_result = await client.poll(
                    http,
                    prediction_id,
                    api_key=api_key,
                    api_root=api_root,
                    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                    poll_interval=DEFAULT_POLL_INTERVAL_SECONDS,
                )
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    detail = exc.response.text[:500]
                except Exception:
                    pass
                return error_response(
                    error=f"Atlas API error ({exc.response.status_code}): {detail or exc}",
                    error_type="api_error",
                    provider="atlas",
                    model=family_id,
                    prompt=prompt,
                )

        status = poll_result["status"]
        body = poll_result["body"]
        if status == "completed":
            video = client.first_output_url(body)
            if not video:
                return error_response(
                    error="Atlas video generation completed without an output URL.",
                    error_type="empty_response",
                    provider="atlas",
                    model=family_id,
                    prompt=prompt,
                )
            return success_response(
                video=video,
                model=family_id,
                prompt=prompt,
                modality=modality,
                aspect_ratio=payload.get("aspect_ratio", ""),
                duration=int(payload["duration"]),
                provider="atlas",
                extra={
                    "atlas_model": atlas_model,
                    "prediction_id": prediction_id,
                    "resolution": payload["resolution"],
                },
            )

        if status == "timeout":
            return error_response(
                error=f"Timed out waiting for Atlas video after {DEFAULT_TIMEOUT_SECONDS}s.",
                error_type="timeout",
                provider="atlas",
                model=family_id,
                prompt=prompt,
            )

        message = body.get("error") or body.get("message") or f"Atlas status: {status}"
        return error_response(
            error=str(message),
            error_type=f"atlas_{status}",
            provider="atlas",
            model=family_id,
            prompt=prompt,
        )


def register(ctx) -> None:
    """Plugin entry point."""
    ctx.register_video_gen_provider(AtlasVideoGenProvider())
