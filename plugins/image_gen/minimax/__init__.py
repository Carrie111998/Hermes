"""MiniMax image generation backend.

Exposes MiniMax's ``image-01`` text-to-image model as an
:class:`ImageGenProvider` implementation.

Features:
- Text-to-image generation
- Eight aspect ratios (1:1, 16:9, 4:3, 3:2, 2:3, 3:4, 9:16, 21:9)
- 1–9 images per request (we always send ``n=1`` to match the Hermes
  single-image contract — callers wanting a grid should call repeatedly)
- Optional prompt-optimizer pass
- Reproducible generation via ``seed`` kwarg
- URL or base64 output (we prefer base64 so the gateway has a stable
  file path; URLs expire after 24h per the MiniMax API spec)

Selection precedence (first hit wins):
1. ``MINIMAX_IMAGE_MODEL`` env var
2. ``image_gen.minimax.model`` in ``config.yaml``
3. :data:`DEFAULT_MODEL`

Docs: https://platform.minimax.io/docs/api-reference/image-generation-t2i
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

# Only one model currently supported by the T2I endpoint. Listed as a dict
# (not a bare string) so the registry shape matches the multi-model providers
# — the picker will show whatever's here, and adding a future model is a
# one-line append.
_MODELS: Dict[str, Dict[str, Any]] = {
    "image-01": {
        "display": "MiniMax image-01",
        "speed": "~8-15s",
        "strengths": "Photorealistic, anime, illustration; up to 9 images/request.",
        "price": "see https://platform.minimax.io/docs/pricing/overview",
    },
}

DEFAULT_MODEL = "image-01"

# MiniMax aspect ratio options per the public API spec. The three Hermes
# canonical ratios are explicit; the rest map through unchanged.
_MINIMAX_ASPECT_RATIOS = {
    "landscape": "16:9",
    "square": "1:1",
    "portrait": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
    "21:9": "21:9",
}

# Base URL — international endpoint, matches the LLM provider's MINIMAX_API_KEY
# semantics (https://api.minimax.io). China users can override with
# ``MINIMAX_IMAGE_BASE_URL=https://api.minimaxi.com/v1`` if/when the image
# endpoint mirrors to the CN domain.
DEFAULT_BASE_URL = "https://api.minimax.io/v1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _load_minimax_config() -> Dict[str, Any]:
    """Read ``image_gen.minimax`` from config.yaml."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        minimax_section = section.get("minimax") if isinstance(section, dict) else None
        return minimax_section if isinstance(minimax_section, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not load image_gen.minimax config: %s", exc)
        return {}


def _resolve_model() -> Tuple[str, Dict[str, Any]]:
    """Decide which model to use and return ``(model_id, meta)``."""
    env_override = os.environ.get("MINIMAX_IMAGE_MODEL")
    if env_override and env_override in _MODELS:
        return env_override, _MODELS[env_override]

    cfg = _load_minimax_config()
    candidate = cfg.get("model") if isinstance(cfg.get("model"), str) else None
    if candidate and candidate in _MODELS:
        return candidate, _MODELS[candidate]

    return DEFAULT_MODEL, _MODELS[DEFAULT_MODEL]


def _resolve_base_url() -> str:
    """Pick the base URL, honoring ``MINIMAX_IMAGE_BASE_URL`` for China users."""
    override = os.environ.get("MINIMAX_IMAGE_BASE_URL")
    if override:
        return override.rstrip("/")
    cfg = _load_minimax_config()
    candidate = cfg.get("base_url") if isinstance(cfg.get("base_url"), str) else None
    if candidate:
        return candidate.rstrip("/")
    return DEFAULT_BASE_URL


def _resolve_prompt_optimizer(kwargs: Dict[str, Any]) -> bool:
    """Read ``prompt_optimizer`` kwarg / config, defaulting to False."""
    if "prompt_optimizer" in kwargs:
        return bool(kwargs["prompt_optimizer"])
    cfg = _load_minimax_config()
    if isinstance(cfg.get("prompt_optimizer"), bool):
        return cfg["prompt_optimizer"]
    return False


def _resolve_seed(kwargs: Dict[str, Any]) -> Optional[int]:
    """Read ``seed`` kwarg, falling back to config. None = random."""
    if "seed" in kwargs:
        value = kwargs["seed"]
        if isinstance(value, int):
            return value
    cfg = _load_minimax_config()
    if isinstance(cfg.get("seed"), int):
        return cfg["seed"]
    return None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MiniMaxImageGenProvider(ImageGenProvider):
    """MiniMax ``image-01`` backend."""

    @property
    def name(self) -> str:
        return "minimax"

    @property
    def display_name(self) -> str:
        return "MiniMax"

    def is_available(self) -> bool:
        return bool(os.environ.get("MINIMAX_API_KEY"))

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
            "name": "MiniMax (image-01)",
            "badge": "paid",
            "tag": (
                "image-01 — text-to-image; 8 aspect ratios; "
                "1–9 images/request; uses MINIMAX_API_KEY"
            ),
            "env_vars": [
                {
                    "key": "MINIMAX_API_KEY",
                    "prompt": "MiniMax API key",
                    "url": "https://platform.minimax.io/user-center/basic-information/interface-key",
                },
            ],
        }

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Generate an image using MiniMax's image-01 endpoint."""
        prompt = (prompt or "").strip()
        aspect = resolve_aspect_ratio(aspect_ratio)

        if not prompt:
            return error_response(
                error="Prompt is required and must be a non-empty string",
                error_type="invalid_argument",
                provider="minimax",
                aspect_ratio=aspect,
            )
        # MiniMax caps prompts at 1500 chars — surface a clean error rather
        # than letting the API return a generic 4xx.
        if len(prompt) > 1500:
            return error_response(
                error=(
                    f"Prompt is {len(prompt)} characters; MiniMax image-01 "
                    "accepts at most 1500."
                ),
                error_type="invalid_argument",
                provider="minimax",
                aspect_ratio=aspect,
            )

        api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not api_key:
            return error_response(
                error=(
                    "MINIMAX_API_KEY not set. Run `hermes setup` → Image "
                    "Generation → MiniMax to configure, or get a key at "
                    "https://platform.minimax.io/."
                ),
                error_type="auth_required",
                provider="minimax",
                aspect_ratio=aspect,
            )

        model_id, _meta = _resolve_model()
        minimax_ar = _MINIMAX_ASPECT_RATIOS.get(aspect, "1:1")
        base_url = _resolve_base_url()
        prompt_optimizer = _resolve_prompt_optimizer(kwargs)
        seed = _resolve_seed(kwargs)

        # Prefer base64 — URLs expire after 24h per MiniMax docs and that
        # outlives the gateway's intent to ship the file around.
        payload: Dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "aspect_ratio": minimax_ar,
            "response_format": "base64",
            "n": 1,
            "prompt_optimizer": prompt_optimizer,
        }
        if seed is not None:
            payload["seed"] = seed

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                f"{base_url}/image_generation",
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
                    # MiniMax wraps status under base_resp / data.error.
                    err_msg = (
                        body.get("base_resp", {}).get("status_msg")
                        or body.get("message")
                        or resp.text[:300]
                    )
                except Exception:
                    err_msg = resp.text[:300]
            else:
                err_msg = str(exc)
            logger.error("MiniMax image gen failed (%d): %s", status, err_msg)
            return error_response(
                error=f"MiniMax image generation failed ({status}): {err_msg}",
                error_type="api_error",
                provider="minimax",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.Timeout:
            return error_response(
                error="MiniMax image generation timed out (120s)",
                error_type="timeout",
                provider="minimax",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"MiniMax connection error: {exc}",
                error_type="connection_error",
                provider="minimax",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:
            return error_response(
                error=f"MiniMax returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider="minimax",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # Per the spec, base_resp.status_code == 0 indicates success. Other
        # values are MiniMax-specific error codes — surface them precisely
        # rather than treating the whole response as a generic failure.
        base_resp = result.get("base_resp") or {}
        if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
            err_msg = base_resp.get("status_msg") or "unknown MiniMax error"
            return error_response(
                error=f"MiniMax image generation failed: {err_msg}",
                error_type="api_error",
                provider="minimax",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        # Successful path — extract the first asset from data.image_urls
        # (or fall through to a b64 field if MiniMax ever adds one).
        data = result.get("data") or {}
        image_url: Optional[str] = None
        if isinstance(data, dict):
            urls = data.get("image_urls")
            if isinstance(urls, list) and urls:
                first = urls[0]
                if isinstance(first, str) and first.strip():
                    image_url = first.strip()
        b64 = data.get("image_base64") if isinstance(data, dict) else None
        if not b64 and not image_url:
            # Spec mentions ``data.image_urls`` — but also try the raw b64
            # fallback ``data.image_base64`` since some MiniMax deployments
            # use the latter naming. Only fail after exhausting both.
            return error_response(
                error="MiniMax returned no image_urls and no base64 data",
                error_type="empty_response",
                provider="minimax",
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        if b64:
            try:
                saved_path = save_b64_image(b64, prefix=f"minimax_{model_id}")
            except Exception as exc:
                return error_response(
                    error=f"Could not save image to cache: {exc}",
                    error_type="io_error",
                    provider="minimax",
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        else:
            # URL fallback — URLs expire in 24h, so cache locally at
            # tool-completion time, mirroring xAI's ephemeral-URL guard
            # (see plugins/image_gen/xai/__init__.py).
            assert image_url is not None
            try:
                saved_path = save_url_image(image_url, prefix=f"minimax_{model_id}")
            except Exception as exc:
                logger.warning(
                    "MiniMax image URL %s could not be cached (%s); falling back to bare URL.",
                    image_url,
                    exc,
                )
                image_ref = image_url
            else:
                image_ref = str(saved_path)

        extra: Dict[str, Any] = {
            "aspect_ratio_native": minimax_ar,
            "prompt_optimizer": prompt_optimizer,
        }
        if seed is not None:
            extra["seed"] = seed
        request_id = result.get("id")
        if isinstance(request_id, str) and request_id:
            extra["request_id"] = request_id

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider="minimax",
            extra=extra,
        )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def register(ctx: Any) -> None:
    """Register this provider with the image gen registry."""
    ctx.register_image_gen_provider(MiniMaxImageGenProvider())
