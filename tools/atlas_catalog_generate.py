"""Atlas/aiproxy video generation driven from the tool layer (dynamic catalog).

This is the tool-layer fix for the bug where the atlas plugin's static family
resolver silently swapped any model it did not know (e.g. ``bytedance/
seedance-2.0/reference-to-video``) for ``wan-2.6-flash``. The plugin is left
untouched; when atlas is the active backend, ``video_generate`` routes here
instead of ``provider.generate()``.

What this module does:

1. Resolves the requested model from the same sources the backend used
   (explicit arg, ``ATLAS_VIDEO_MODEL`` env, ``video_gen.atlas.model``,
   ``video_gen.model``).
2. Maps it to a concrete aiproxy model id:
   - a known legacy family id (``wan-2.6-flash``, ``kling-v3-pro`` …) → the
     family's text/image model, with the plugin's proven payload clamping;
   - a known full model id → itself (modality checked);
   - anything else → validated against the dynamic backend catalog (fail
     closed) and passed through verbatim. aiproxy validates the constraints
     server-side.
3. Submits/polls aiproxy via the plugin client's transport helpers (imported,
   **not modified**) and returns a uniform success/error dict.

Fail-closed contract (per the no-silent-degradation rule): no resolvable model
→ ``model_required``; catalog-only model not listed → ``model_not_found``;
catalog unreachable → ``catalog_unavailable``. Never a hidden default.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import httpx

from agent.video_gen_provider import (
    COMMON_ASPECT_RATIOS,
    DEFAULT_RESOLUTION,
    error_response,
    success_response,
)
from plugins.video_gen.atlas import client as atlas_client
from plugins.video_gen.atlas.catalog import ATLAS_FAMILIES
from tools.media_catalog import MediaCatalogError, get_catalog_client

logger = logging.getLogger(__name__)

PROVIDER_NAME = "atlas"
MAX_REFERENCE_IMAGES = 4


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


def _family_for_full_model(model_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return ``(family, modality)`` when ``model_id`` is a known full atlas
    model id (e.g. ``alibaba/wan-2.6/text-to-video``); else ``(None, None)``."""
    for family in ATLAS_FAMILIES.values():
        if family.get("text_model") == model_id:
            return family, "text"
        if family.get("image_model") == model_id:
            return family, "image"
    return None, None


def _resolve_model(explicit: Optional[str], video_gen_section: Dict[str, Any]) -> Optional[str]:
    """First non-empty model across arg / env / config.atlas.model / config.model."""
    candidates: List[Any] = [explicit, os.environ.get("ATLAS_VIDEO_MODEL")]
    atlas_cfg = video_gen_section.get("atlas")
    if isinstance(atlas_cfg, dict):
        candidates.append(atlas_cfg.get("model"))
    candidates.append(video_gen_section.get("model"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _resolve_concrete_model(
    model_id: str,
    modality: str,
) -> Tuple[str, Dict[str, Any], Optional[str], Optional[str]]:
    """Map a requested model to ``(concrete_model, family_hint, error, error_type)``.

    ``family_hint`` is the ``ATLAS_FAMILIES`` entry for legacy families (drives
    payload clamping) or ``{}`` for catalog passthrough models.
    """
    # 1. Legacy family id.
    if model_id in ATLAS_FAMILIES:
        family = ATLAS_FAMILIES[model_id]
        concrete = family.get(f"{modality}_model")
        if not concrete:
            return "", {}, (
                f"atlas family {model_id!r} does not support {modality}-to-video."
            ), "modality_unsupported"
        return str(concrete), family, None, None

    # 2. Known full model id.
    family, full_modality = _family_for_full_model(model_id)
    if family is not None:
        if full_modality != modality:
            return "", {}, (
                f"atlas model {model_id!r} is {full_modality}-to-video only."
            ), "modality_unsupported"
        return model_id, family, None, None

    # 3. Dynamic catalog membership check — fail closed.
    try:
        exists = get_catalog_client().model_exists(model_id, type="video")
    except MediaCatalogError as exc:
        return "", {}, (
            f"could not validate model {model_id!r} against the backend catalog "
            f"({exc.message}). Restore catalog access, or call models_explore to "
            f"pick a known model."
        ), "catalog_unavailable"
    except Exception as exc:  # boundary: unexpected failure → fail closed, observable
        logger.warning("catalog check failed for %s: %s", model_id, exc, exc_info=True)
        return "", {}, (
            f"catalog check failed for model {model_id!r}: {exc}. "
            f"Call models_explore to pick a known model."
        ), "catalog_unavailable"

    if not exists:
        return "", {}, (
            f"model {model_id!r} is not available in the backend video catalog. "
            f"Call models_explore(action='list', type='video') to see available "
            f"models."
        ), "model_not_found"

    # 4. Passthrough: the catalog knows it; aiproxy validates constraints.
    return model_id, {}, None, None


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _normalize_resolution_passthrough(resolution: str) -> str:
    requested = (resolution or DEFAULT_RESOLUTION).strip().upper().replace("_", "-")
    return requested if requested.endswith(("P", "P-SR")) else f"{requested}P"


def _passthrough_payload(
    concrete: str,
    *,
    prompt: str,
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    duration: Optional[int],
    aspect_ratio: str,
    resolution: str,
    audio: Optional[bool],
    seed: Optional[int],
    negative_prompt: Optional[str],
) -> Dict[str, Any]:
    """Payload for a catalog passthrough model — constraints sent as requested;
    aiproxy validates them server-side (no client-side forcing)."""
    payload: Dict[str, Any] = {
        "model": concrete,
        "prompt": prompt,
        "enable_sync_mode": False,
        "resolution": _normalize_resolution_passthrough(resolution),
    }
    if duration is not None:
        payload["duration"] = max(1, int(duration))
    if image_url:
        payload["image"] = atlas_client.normalize_image_input(image_url)
    else:
        ratio = (aspect_ratio or "").strip()
        if ratio in COMMON_ASPECT_RATIOS:
            payload["aspect_ratio"] = ratio
    refs = [
        atlas_client.normalize_image_input(item)
        for item in reference_image_urls or []
        if isinstance(item, str) and item.strip()
    ]
    if refs:
        payload["reference_images"] = refs
    if seed is not None:
        payload["seed"] = seed
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if audio is not None:
        payload["audio"] = bool(audio)
    return payload


def _build_payload(
    concrete: str,
    family_hint: Dict[str, Any],
    *,
    prompt: str,
    image_url: Optional[str],
    reference_image_urls: Optional[List[str]],
    duration: Optional[int],
    aspect_ratio: str,
    resolution: str,
    audio: Optional[bool],
    seed: Optional[int],
    negative_prompt: Optional[str],
) -> Dict[str, Any]:
    if family_hint:
        # Legacy family: reuse the plugin's proven clamping (duration/resolution).
        return atlas_client.build_payload(
            family_hint,
            atlas_model=concrete,
            prompt=prompt,
            image_url=image_url,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            audio=audio,
            seed=seed,
            negative_prompt=negative_prompt,
            reference_image_urls=reference_image_urls,
        )
    return _passthrough_payload(
        concrete,
        prompt=prompt,
        image_url=image_url,
        reference_image_urls=reference_image_urls,
        duration=duration,
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        audio=audio,
        seed=seed,
        negative_prompt=negative_prompt,
    )


# ---------------------------------------------------------------------------
# Transport (imported plugin client; not modified)
# ---------------------------------------------------------------------------


async def _submit_and_poll(
    payload: Dict[str, Any],
    *,
    api_key: str,
    api_root: str,
    concrete: str,
    prompt: str,
    modality: str,
) -> Dict[str, Any]:
    async with httpx.AsyncClient() as http:
        prediction_id = await atlas_client.submit(
            http, payload, api_key=api_key, api_root=api_root
        )
        poll_result = await atlas_client.poll(
            http,
            prediction_id,
            api_key=api_key,
            api_root=api_root,
            timeout_seconds=atlas_client.DEFAULT_TIMEOUT_SECONDS,
            poll_interval=atlas_client.DEFAULT_POLL_INTERVAL_SECONDS,
        )

    status = poll_result["status"]
    body = poll_result["body"]
    if status == "completed":
        video = atlas_client.first_output_url(body)
        if not video:
            return error_response(
                error="Atlas video generation completed without an output URL.",
                error_type="empty_response",
                provider=PROVIDER_NAME,
                model=concrete,
                prompt=prompt,
            )
        return success_response(
            video=video,
            model=concrete,
            prompt=prompt,
            modality=modality,
            aspect_ratio=payload.get("aspect_ratio", ""),
            duration=int(payload.get("duration", 0)),
            provider=PROVIDER_NAME,
            extra={
                "atlas_model": concrete,
                "prediction_id": prediction_id,
                "resolution": payload.get("resolution", ""),
            },
        )

    if status == "timeout":
        return error_response(
            error=f"Timed out waiting for Atlas video after "
            f"{atlas_client.DEFAULT_TIMEOUT_SECONDS}s.",
            error_type="timeout",
            provider=PROVIDER_NAME,
            model=concrete,
            prompt=prompt,
        )

    message = body.get("error") or body.get("message") or f"Atlas status: {status}"
    return error_response(
        error=str(message),
        error_type=f"atlas_{status}",
        provider=PROVIDER_NAME,
        model=concrete,
        prompt=prompt,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def generate(
    prompt: str,
    *,
    model: Optional[str],
    video_gen_section: Dict[str, Any],
    image_url: Optional[str] = None,
    reference_image_urls: Optional[List[str]] = None,
    duration: Optional[int] = None,
    aspect_ratio: str = "16:9",
    resolution: str = DEFAULT_RESOLUTION,
    negative_prompt: Optional[str] = None,
    audio: Optional[bool] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Generate via aiproxy with dynamic-catalog model validation.

    Returns the same uniform dict shape as a ``VideoGenProvider.generate``.
    """
    api_key, api_root = atlas_client.resolve_credentials()
    if not api_key:
        return error_response(
            error="ATLAS_API_KEY not set. Configure AtlasCloud before generating video.",
            error_type="auth_required",
            provider=PROVIDER_NAME,
            prompt=prompt,
        )

    prompt = (prompt or "").strip()
    if not prompt:
        return error_response(
            error="prompt is required.",
            error_type="missing_prompt",
            provider=PROVIDER_NAME,
            prompt=prompt,
        )
    if reference_image_urls and len(reference_image_urls) > MAX_REFERENCE_IMAGES:
        return error_response(
            error=f"Atlas video_generate supports at most {MAX_REFERENCE_IMAGES} "
            f"reference_image_urls.",
            error_type="too_many_references",
            provider=PROVIDER_NAME,
            prompt=prompt,
        )

    image_url_norm = (image_url or "").strip() or None
    modality = "image" if image_url_norm else "text"

    model_id = _resolve_model(model, video_gen_section)
    if not model_id:
        return error_response(
            error=(
                "no video model specified. Call models_explore(action='recommend' "
                "or 'list', type='video') to pick one, or set video_gen.model. "
                "The atlas backend no longer silently picks a default model."
            ),
            error_type="model_required",
            provider=PROVIDER_NAME,
            prompt=prompt,
        )

    concrete, family_hint, resolve_error, error_type = _resolve_concrete_model(
        model_id, modality
    )
    if resolve_error:
        return error_response(
            error=resolve_error,
            error_type=error_type or "model_resolution",
            provider=PROVIDER_NAME,
            model=model_id,
            prompt=prompt,
        )

    try:
        payload = _build_payload(
            concrete,
            family_hint,
            prompt=prompt,
            image_url=image_url_norm,
            reference_image_urls=reference_image_urls,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            audio=audio,
            seed=seed,
            negative_prompt=negative_prompt,
        )
    except ValueError as exc:
        return error_response(
            error=str(exc),
            error_type="invalid_image_url",
            provider=PROVIDER_NAME,
            model=concrete,
            prompt=prompt,
        )

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _submit_and_poll(
                    payload,
                    api_key=api_key,
                    api_root=api_root,
                    concrete=concrete,
                    prompt=prompt,
                    modality=modality,
                )
            )
        finally:
            loop.close()
    except httpx.HTTPStatusError as exc:
        detail = ""
        try:
            detail = exc.response.text[:500]
        except Exception:  # noqa: BLE001 — best-effort detail extraction
            pass
        return error_response(
            error=f"Atlas API error ({exc.response.status_code}): {detail or exc}",
            error_type="api_error",
            provider=PROVIDER_NAME,
            model=concrete,
            prompt=prompt,
        )
    except Exception as exc:  # noqa: BLE001 — surface as structured error, never crash the tool
        logger.warning("Atlas catalog generation failed: %s", exc, exc_info=True)
        return error_response(
            error=f"Atlas video generation failed: {exc}",
            error_type="api_error",
            provider=PROVIDER_NAME,
            model=concrete,
            prompt=prompt,
        )
