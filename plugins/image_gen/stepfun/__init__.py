"""StepFun image generation backend.

Exposes StepFun's ``step-image-edit-2`` text-to-image model as an
:class:`ImageGenProvider` implementation. (The name retains the "-edit-"
segment because that's what StepFun currently ships; the endpoint serves
both text-to-image and image-to-image.)

Features:
- Text-to-image generation
- Five sizes (1024x1024, 768x1360, 896x1184, 1360x768, 1184x896)
- Adjustable ``steps`` (1–50, default 8) and ``cfg_scale`` (1.0–10.0,
  default 1.0) — surfaced as kwargs
- ``negative_prompt`` (up to 512 chars) — surfaced as kwarg
- ``text_mode`` toggle for text-rendering optimization — surfaced as kwarg
- Reproducible generation via ``seed``
- Base64 or URL output (we prefer base64 so the gateway has a stable
  file path; URLs expire after 2h per the StepFun API spec)

Selection precedence (first hit wins):
1. ``STEPFUN_IMAGE_MODEL`` env var
2. ``image_gen.stepfun.model`` in ``config.yaml``
3. :data:`DEFAULT_MODEL`

Docs: https://platform.stepfun.ai/docs/en/api-reference/images/image
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

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

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

# Per the StepFun image API docs (June 2026), only step-image-edit-2 is
# currently exposed for the v1/images/generations endpoint. Model dict shape
# matches the multi-model providers so the picker UX is identical.
_MODELS: Dict[str, Dict[str, Any]] = {
    "step-image-edit-2": {
        "display": "StepFun step-image-edit-2",
        "speed": "~5-15s",
        "strengths": (
            "Fast; configurable steps/cfg_scale; supports negative_prompt; "
            "good for text rendering."
        ),
        "price": "see https://platform.stepfun.ai/docs/pricing/overview",
    },
}

DEFAULT_MODEL = "step-image-edit-2"

# Map Hermes's three abstract aspect ratios onto StepFun's size strings.
# Note: StepFun uses ``{height}x{width}`` ordering for the size param,
# which is the *opposite* of OpenAI's ``{width}x{height}`` convention.
_STEPFUN_SIZES = {
    "landscape": "1360x768",   # 16:9-ish — width > height
    "square": "1024x1024",
    "portrait": "768x1360",    # 9:16-ish — height > width
}

# StepFun default base URL — international endpoint. Override with
# ``STEPFUN_IMAGE_BASE_URL`` for the China endpoint
# (https://api.stepfun.com/v1) where applicable.
DEFAULT_BASE_URL = "https://api.stepfun.ai/v1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_stepfun_config() -> Dict[str, Any]:
    """Read ``image_gen.stepfun`` from config.yaml."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        stepfun_section = section.get("stepfun") if isinstance(section, dict) else None
        return stepfun_section if isinstance(stepfun_section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load image_gen.stepfun config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which model to use and return ``(model_id, meta)``."""
    env_override = os.environ.get("STEPFUN_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_stepfun_config()
    candidate = cfg.get("model") if isinstance(cfg.get("model"), str) else None
    if candidate and candidate in _MODELS:
        return candidate, _MODELS[candidate]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _resolve_base_url() -> str:
    """Pick the base URL, honoring ``STEPFUN_IMAGE_BASE_URL`` (China users)."""
    override = os.environ.get("STEPFUN_IMAGE_BASE_URL")
    if override:
        return override.rstrip("/")
    cfg = _load_stepfun_config()
    candidate = cfg.get("base_url") if isinstance(cfg.get("base_url"), str) else None
    if candidate:
        return candidate.rstrip("/")
    return DEFAULT_BASE_URL


def _resolve_int_kwarg(
    name: str,
    kwargs: Dict[str, Any],
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    """Read an int kwarg clamped to ``[minimum, maximum]``."""
    candidate: Optional[int] = None
    if name in kwargs and isinstance(kwargs[name], int) and not isinstance(kwargs[name], bool):
        candidate = kwargs[name]
    if candidate is None:
        cfg = _load_stepfun_config()
        config_value = cfg.get(name)
        if isinstance(config_value, int) and not isinstance(config_value, bool):
            candidate = config_value
    if candidate is None:
        return default
    return max(minimum, min(maximum, candidate))


def _resolve_float_kwarg(
    name: str,
    kwargs: Dict[str, Any],
    *,
    minimum: float,
    maximum: float,
    default: float,
) -> float:
    """Read a float kwarg clamped to ``[minimum, maximum]``."""
    candidate: Optional[float] = None
    if name in kwargs:
        value = kwargs[name]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            candidate = float(value)
    if candidate is None:
        cfg = _load_stepfun_config()
        config_value = cfg.get(name)
        if isinstance(config_value, (int, float)) and not isinstance(config_value, bool):
            candidate = float(config_value)
    if candidate is None:
        return default
    return max(minimum, min(maximum, candidate))


def _resolve_str_kwarg(
    name: str,
    kwargs: Dict[str, Any],
    *,
    max_length: int,
) -> Optional[str]:
    """Read a string kwarg, truncating to ``max_length`` if exceeded."""
    candidate: Optional[str] = None
    if name in kwargs and isinstance(kwargs[name], str):
        candidate = kwargs[name]
    if candidate is None:
        cfg = _load_stepfun_config()
        config_value = cfg.get(name)
        if isinstance(config_value, str):
            candidate = config_value
    if not candidate:
        return None
    if len(candidate) > max_length:
        logger.debug(
            "StepFun kwarg %s exceeded %d chars (got %d); truncating.",
            name,
            max_length,
            len(candidate),
        )
        candidate = candidate[:max_length]
    return candidate


def _resolve_bool_kwarg(name: str, kwargs: Dict[str, Any], default: bool) -> bool:
    """Read a bool kwarg, defaulting when unset."""
    if name in kwargs and isinstance(kwargs[name], bool):
        return kwargs[name]
    cfg = _load_stepfun_config()
    if isinstance(cfg.get(name), bool):
        return cfg[name]
    return default


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class StepFunImageGenProvider(ImageGenProvider):
    """StepFun ``step-image-edit-2`` backend."""

    @property
    def name(self) -> str:
        return "stepfun"

    @property
    def display_name(self) -> str:
        return "StepFun"

    def is_available(self) -> bool:
        return bool(os.environ.get("STEPFUN_API_KEY"))

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
            "name": "StepFun (step-image-edit-2)",
            "badge": "paid",
            "tag": (
                "step-image-edit-2 — text-to-image; configurable steps/cfg; "
                "uses STEPFUN_API_KEY"
            ),
            "env_vars": [
                {
                    "key": "STEPFUN_API_KEY",
                    "prompt": "StepFun API key",
                    "url": "https://platform.stepfun.ai/",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate an image using StepFun's step-image-edit-2 endpoint."""
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="stepfun",
                aspect_ratio=aspect,
            )
        if len(prompt) > 512:
            return error_response(
                error=(
                    f"Prompt is {len(prompt)} characters; StepFun image API "
                    "accepts at most 512."
                ),
                error_type="invalid_argument",
                provider="stepfun",
                aspect_ratio=aspect,
            )

        api_key = os.environ.get("STEPFUN_API_KEY", "").strip()
        if not api_key:
            return error_response(
                error=(
                    "STEPFUN_API_KEY not set. Run `hermes setup` → Image "
                    "Generation → StepFun to configure, or get a key at "
                    "https://platform.stepfun.ai/."
                ),
                error_type="auth_required",
                provider="stepfun",
                aspect_ratio=aspect,
            )

        model_id, _meta = _resolve_model()
        size = _STEPFUN_SIZES.get(aspect, _STEPFUN_SIZES["square"])
        base_url = _resolve_base_url()

        steps = _resolve_int_kwarg("steps", kwargs, minimum=1, maximum=50, default=8)
        cfg_scale = _resolve_float_kwarg(
            "cfg_scale", kwargs, minimum=1.0, maximum=10.0, default=1.0
        )
        negative_prompt = _resolve_str_kwarg(
            "negative_prompt", kwargs, max_length=512
        )
        text_mode = _resolve_bool_kwarg("text_mode", kwargs, default=False)
        seed = _resolve_int_kwarg(
            "seed", kwargs, minimum=0, maximum=2_147_483_647, default=-1
        )

        # Prefer base64 — URLs expire after 2h per StepFun docs and that
        # outlives the gateway's intent to ship the file around. We send
        # ``n=1`` to match the Hermes single-image contract; the StepFun
        # API only supports n=1 today.
        payload: Dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "size": size,
            "response_format": "b64_json",
            "n": 1,
        }
        if seed >= 0:
            payload["seed"] = seed
        # Only send tunables when the caller actually changed them from the
        # API defaults — keeps the wire request minimal and avoids surprises
        # when StepFun's defaults shift.
        if steps != 8:
            payload["steps"] = steps
        if cfg_scale != 1.0:
            payload["cfg_scale"] = cfg_scale
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if text_mode:
            payload["text_mode"] = True

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{base_url}/images/generations",
                headers=headers,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            err_msg = ""
            if resp is not None:
                try:
                    body = resp.json()
                    err_msg = (
                        body.get("error", {}).get("message")
                        if isinstance(body.get("error"), dict)
                        else body.get("message")
                        or body.get("detail")
                        or resp.text[:300]
                    )
                except Exception:
                    err_msg = resp.text[:300]
            else:
                err_msg = str(exc)
            logger.error("StepFun image gen failed (%d): %s", status, err_msg)
            return error_response(
                error=f"StepFun image generation failed ({status}): {err_msg}",
                error_type="api_error",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.Timeout:
            return error_response(
                error="StepFun image generation timed out (120s)",
                error_type="timeout",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"StepFun connection error: {exc}",
                error_type="connection_error",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:
            return error_response(
                error=f"StepFun returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        data = result.get("data") or []
        if not data:
            return error_response(
                error="StepFun returned no image data",
                error_type="empty_response",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0] if isinstance(data[0], dict) else {}
        b64 = first.get("b64_json")
        url = first.get("url")
        finish_reason = first.get("finish_reason")
        used_seed = first.get("seed")

        # finish_reason == "content_filtered" means the API successfully
        # generated an image but the safety filter rejected it before
        # returning any bytes. Surface this as a clean error rather than
        # the generic "no image data" message.
        if finish_reason and finish_reason != "success" and not b64 and not url:
            return error_response(
                error=(
                    f"StepFun returned finish_reason={finish_reason!r}; "
                    "no image bytes to save. Try rephrasing the prompt."
                ),
                error_type="content_filtered",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if b64:
            try:
                saved_path = save_b64_image(b64, prefix=f"stepfun_{model_id}")
            except Exception as exc:
                return error_response(
                    error=f"Could not save image to cache: {exc}",
                    error_type="io_error",
                    provider="stepfun",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        elif url:
            # Defensive — we requested b64_json, but StepFun may return URL
            # mode in some edge cases. Cache the bytes locally so the
            # gateway never tries to fetch an ephemeral URL after it
            # expires (2h validity per docs).
            try:
                saved_path = save_url_image(url, prefix=f"stepfun_{model_id}")
            except Exception as exc:
                logger.warning(
                    "StepFun image URL %s could not be cached (%s); falling back to bare URL.",
                    url,
                    exc,
                )
                image_ref = url
            else:
                image_ref = str(saved_path)
        else:
            return error_response(
                error="StepFun response contained neither b64_json nor URL",
                error_type="empty_response",
                provider="stepfun",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        extra: Dict[str, Any] = {"size": size}
        if finish_reason:
            extra["finish_reason"] = finish_reason
        if isinstance(used_seed, int):
            extra["seed"] = used_seed
        elif seed >= 0:
            extra["seed"] = seed

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="stepfun",
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register this provider with the image gen registry."""
    ctx.register_image_gen_provider(StepFunImageGenProvider())
