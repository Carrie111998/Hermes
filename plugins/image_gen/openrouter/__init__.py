"""OpenRouter-compatible image generation backends (OpenRouter + Nous Portal).

Two distinct OpenRouter API surfaces are supported by separate provider classes
in this module:

1. **Chat-completions image output** (``OpenRouterCompatImageProvider``)
   — sends ``modalities: ["image", "text"]`` to ``/chat/completions`` with an
   image-output model (e.g. ``openai/gpt-5.4-image-2``, ``google/gemini-3-pro-image``).
   Generated images arrive as ``choices[0].message.images[].image_url.url``
   (typically a base64 data URI). Reference images are passed as ``image_url``
   content parts for grounding.

2. **Dedicated Image API** (``OpenRouterImageAPIProvider``)
   — sends text-to-image requests to ``POST /api/v1/images``, the OpenRouter
   endpoint designed for models like ``x-ai/grok-imagine-image-2.0`` that
   expose a pure image-generation API. Supports ``aspect_ratio``, ``resolution``,
   ``quality``, and ``input_references`` (image-to-image, up to 3 refs).
   Generated images arrive as ``data[i].b64_json`` in the JSON response body.

Nous Portal proxies OpenRouter, so the chat-completions implementation services
both — we only swap the resolved ``(base_url, api_key)``. Credentials are
resolved through the agent's existing
:func:`~hermes_cli.runtime_provider.resolve_runtime_provider`, which already
understands OpenRouter's key pool and the Nous OAuth device-code token, so this
plugin never reinvents auth.

Reference grounding is the reason pet sprite generation cares about the
chat-completions backend: each animation row must stay the same character as the
chosen base frame, which only works on models that accept image input as content
parts. The Image API backend uses ``input_references`` for image-to-image, which
is a different mechanism.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

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

# Quality-first model chain for OpenRouter-compatible endpoints.
#
# Default behavior (no env/config override): try the highest-fidelity OpenAI
# image model first, then fall back to Gemini 3 Pro Image if the OpenAI model
# is access-gated / unavailable / times out on this endpoint.
#
# Explicit override (OPENROUTER_IMAGE_MODEL, image_gen.<provider>.model, or
# image_gen.model from ``hermes tools``): use exactly that model (no auto
# fallback), so power users keep full control.
DEFAULT_MODEL = "openai/gpt-5.4-image-2"
_FALLBACK_MODEL = "google/gemini-3-pro-image"
_DEFAULT_MODEL_CHAIN = (DEFAULT_MODEL, _FALLBACK_MODEL)

# Semantic aspect ratio (the image_gen contract) → OpenRouter's image_config
# aspect_ratio strings.
_ASPECT_RATIOS = {
    "square": "1:1",
    "landscape": "16:9",
    "portrait": "9:16",
}

# Gemini Flash Image accepts up to 3 input images per prompt; clamp references
# so we never overflow the model's limit.
_MAX_REFERENCE_IMAGES = 3

# Per single image call. The quality-first default (OpenAI image via OpenRouter)
# is genuinely slow — a single cold row can run well past 3 minutes — so give
# each call real headroom before we treat it as hung and fall back / retry.
_REQUEST_TIMEOUT = 300.0


def _load_image_gen_config() -> Dict[str, Any]:
    """Read the ``image_gen`` section from config.yaml (``{}`` on failure)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        section = cfg.get("image_gen") if isinstance(cfg, dict) else None
        return section if isinstance(section, dict) else {}
    except Exception as exc:  # noqa: BLE001 - config is best-effort
        logger.debug("could not load image_gen config: %s", exc)
        return {}


def _to_image_url_part(ref: str) -> Optional[str]:
    """Turn a reference (local path or http URL) into an ``image_url`` value.

    Remote URLs pass through unchanged; local files are inlined as base64 data
    URIs so the request is self-contained (the provider endpoint can't reach a
    path on our disk). Returns ``None`` when the reference can't be read.
    """
    ref = str(ref or "").strip()
    if not ref:
        return None
    if ref.startswith(("http://", "https://", "data:")):
        return ref
    path = Path(ref)
    # Enforce the shared credential-read guard before inlining local bytes.
    from agent.file_safety import raise_if_read_blocked

    raise_if_read_blocked(ref)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.debug("could not read reference image %s: %s", ref, exc)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_images(payload: Dict[str, Any]) -> List[str]:
    """Pull generated image URLs from a chat-completions response.

    OpenRouter returns generated images under
    ``choices[0].message.images[].image_url.url`` (typically a base64 data URI).
    """
    out: List[str] = []
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list):
        return out
    for choice in choices:
        message = choice.get("message") if isinstance(choice, dict) else None
        images = message.get("images") if isinstance(message, dict) else None
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            image_url = image.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else None
            if isinstance(url, str) and url.strip():
                out.append(url.strip())
    return out


def _access_error_hint(
    display: str, model_id: str, env_var: str, status: int, err_msg: str
) -> Optional[str]:
    """A targeted hint when an access-gated OpenAI image model can't be reached.

    Some OpenAI image models on OpenRouter need account enablement / BYOK, so the
    failure isn't a missing key (the key is valid) — the *model* is unreachable.
    The generic "check your key" message is misleading there, so we detect that
    case and point the user at the real fix. Returns one actionable line, or
    ``None`` when this isn't the access-gated case.
    """
    if not model_id.startswith("openai/"):
        return None
    low = (err_msg or "").lower()
    gated = status in (402, 403, 404) or any(
        s in low for s in ("no endpoints", "no allowed", "not a valid model", "data policy")
    )
    if not gated:
        return None
    return (
        f"{display} can't reach image model '{model_id}' ({status}) — enable OpenAI "
        f"image access in your {display} account, or set {env_var}={_FALLBACK_MODEL}."
    )


def _dedupe_models(models: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for model in models:
        m = (model or "").strip()
        if not m or m in seen:
            continue
        seen.add(m)
        out.append(m)
    return out


class OpenRouterCompatImageProvider(ImageGenProvider):
    """Image generation over an OpenRouter-compatible chat-completions endpoint.

    Instantiated once per backend (OpenRouter, Nous Portal). The two differ only
    in which runtime provider supplies ``(base_url, api_key)`` and in the config
    namespace used for the model override.
    """

    def __init__(
        self,
        *,
        provider_name: str,
        display_name: str,
        runtime_name: str,
        config_key: str,
        model_env_var: str,
        setup_schema: Dict[str, Any],
    ) -> None:
        self._name = provider_name
        self._display = display_name
        self._runtime_name = runtime_name
        self._config_key = config_key
        self._model_env_var = model_env_var
        self._setup_schema = setup_schema

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display

    def _resolve_runtime(self) -> Dict[str, Any]:
        """Resolve ``(base_url, api_key)`` via the shared runtime resolver."""
        from hermes_cli.runtime_provider import resolve_runtime_provider

        return resolve_runtime_provider(requested=self._runtime_name)

    def is_available(self) -> bool:
        try:
            runtime = self._resolve_runtime()
        except Exception as exc:  # noqa: BLE001 - treat resolution failure as unavailable
            logger.debug("%s runtime resolution failed: %s", self._name, exc)
            return False
        return bool(str(runtime.get("api_key") or "").strip())

    def capabilities(self) -> Dict[str, Any]:
        # Both text-to-image and image-to-image (reference grounding) — the
        # latter is what makes this backend usable for pet sprite rows.
        return {
            "modalities": ["text", "image"],
            "max_reference_images": _MAX_REFERENCE_IMAGES,
        }

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": DEFAULT_MODEL,
                "display": "OpenAI GPT-5.4 Image 2",
                "strengths": "Highest fidelity; best prompt adherence; slower on OpenRouter",
            },
            {
                "id": _FALLBACK_MODEL,
                "display": "Gemini 3 Pro Image",
                "strengths": "Fast, reliable fallback with good layout adherence",
            },
        ]

    def default_model(self) -> Optional[str]:
        return self._resolve_model()

    def get_setup_schema(self) -> Dict[str, Any]:
        return dict(self._setup_schema)

    def _resolve_model(self, explicit: Optional[str] = None) -> str:
        """Pick the image model (first of :meth:`_resolve_model_chain`)."""
        return self._resolve_model_chain(explicit)[0]

    def _resolve_model_chain(self, explicit: Optional[str] = None) -> list[str]:
        """Ordered model attempts for this request.

        Precedence: explicit caller override (the ``model`` kwarg) → the
        provider's ``*_IMAGE_MODEL`` env override → scoped
        ``image_gen.<provider>.model`` → top-level ``image_gen.model`` (written
        by ``hermes tools``) → the quality-first default chain.

        Any explicit user/model selection means "use this exact model", so no
        fallback. Only the bare default chain carries a Gemini fallback.
        """
        if isinstance(explicit, str) and explicit.strip():
            return [explicit.strip()]
        env_override = os.environ.get(self._model_env_var, "").strip()
        if env_override:
            return [env_override]
        cfg = _load_image_gen_config()
        scoped = cfg.get(self._config_key) if isinstance(cfg.get(self._config_key), dict) else {}
        if isinstance(scoped, dict):
            value = scoped.get("model")
            if isinstance(value, str) and value.strip():
                return [value.strip()]
        top = cfg.get("model")
        if isinstance(top, str) and top.strip():
            return [top.strip()]
        return _dedupe_models(list(_DEFAULT_MODEL_CHAIN))

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        import requests

        try:
            runtime = self._resolve_runtime()
        except Exception as exc:  # noqa: BLE001
            return error_response(
                error=f"Could not resolve {self._display} credentials: {exc}",
                error_type="missing_api_key",
                provider=self._name,
                aspect_ratio=aspect_ratio,
            )
        api_key = str(runtime.get("api_key") or "").strip()
        base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
        if not api_key or not base_url:
            return error_response(
                error=(
                    f"No {self._display} credentials found. "
                    f"Configure {self._display} in `hermes tools` → Image Generation."
                ),
                error_type="missing_api_key",
                provider=self._name,
                aspect_ratio=aspect_ratio,
            )

        model_chain = self._resolve_model_chain(kwargs.get("model"))
        aspect = resolve_aspect_ratio(aspect_ratio)
        or_aspect = _ASPECT_RATIOS.get(aspect, "1:1")

        # Collect every reference: the pet generator passes local paths via the
        # ``reference_images`` kwarg; the generic tool surface uses ``image_url``
        # / ``reference_image_urls``. Accept all three.
        references: List[str] = []
        for ref in kwargs.get("reference_images") or []:
            references.append(str(ref))
        if image_url:
            references.append(str(image_url))
        for ref in reference_image_urls or []:
            references.append(str(ref))

        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for ref in references[:_MAX_REFERENCE_IMAGES]:
            part = _to_image_url_part(ref)
            if part:
                content.append({"type": "image_url", "image_url": {"url": part}})

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (harmless against Nous Portal).
            "HTTP-Referer": "https://github.com/NousResearch/hermes-agent",
            "X-Title": "Hermes Agent",
        }
        last_error: Optional[Dict[str, Any]] = None
        for i, model_id in enumerate(model_chain):
            payload: Dict[str, Any] = {
                "model": model_id,
                "modalities": ["image", "text"],
                "messages": [{"role": "user", "content": content}],
                "image_config": {"aspect_ratio": or_aspect},
            }
            is_last = i == len(model_chain) - 1
            try:
                response = requests.post(
                    f"{base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
            except requests.HTTPError as exc:
                resp = exc.response
                status = resp.status_code if resp is not None else 0
                try:
                    err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
                except Exception:  # noqa: BLE001
                    err_msg = resp.text[:300] if resp is not None else str(exc)
                logger.error("%s image gen failed (%d) on %s: %s", self._name, status, model_id, err_msg)
                hint = _access_error_hint(self._display, model_id, self._model_env_var, status, err_msg)
                if hint and not is_last:
                    logger.info(
                        "%s model %s unavailable; retrying with fallback %s",
                        self._name,
                        model_id,
                        model_chain[i + 1],
                    )
                    continue
                last_error = error_response(
                    error=hint or f"{self._display} image generation failed ({status}): {err_msg}",
                    error_type="model_access" if hint else "api_error",
                    provider=self._name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
                return last_error
            except requests.Timeout:
                if not is_last:
                    logger.info(
                        "%s model %s timed out; retrying with fallback %s",
                        self._name,
                        model_id,
                        model_chain[i + 1],
                    )
                    continue
                return error_response(
                    error=f"{self._display} image generation timed out "
                    f"({int(_REQUEST_TIMEOUT)}s)",
                    error_type="timeout",
                    provider=self._name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            except requests.ConnectionError as exc:
                return error_response(
                    error=f"{self._display} connection error: {exc}",
                    error_type="connection_error",
                    provider=self._name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

            try:
                result = response.json()
            except Exception as exc:  # noqa: BLE001
                return error_response(
                    error=f"{self._display} returned invalid JSON: {exc}",
                    error_type="invalid_response",
                    provider=self._name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

            images = _extract_images(result)
            if not images:
                if not is_last:
                    logger.info(
                        "%s model %s returned no image; retrying with fallback %s",
                        self._name,
                        model_id,
                        model_chain[i + 1],
                    )
                    continue
                # A response with text but no image usually means the model didn't
                # honor image output (wrong model or modalities); surface that.
                return error_response(
                    error=(
                        f"{self._display} returned no image. Ensure the model "
                        f"'{model_id}' supports image output."
                    ),
                    error_type="empty_response",
                    provider=self._name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

            first = images[0]
            try:
                if first.startswith("data:"):
                    b64 = first.split(",", 1)[1] if "," in first else ""
                    saved_path = save_b64_image(b64, prefix=f"{self._name}_gen")
                else:
                    saved_path = save_url_image(first, prefix=f"{self._name}_gen")
            except Exception as exc:  # noqa: BLE001
                return error_response(
                    error=f"Could not save generated image: {exc}",
                    error_type="io_error",
                    provider=self._name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

            return success_response(
                image=str(saved_path),
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
                provider=self._name,
            )

        return last_error or error_response(
            error=f"{self._display} image generation failed after trying all candidate models.",
            error_type="api_error",
            provider=self._name,
            model=model_chain[-1] if model_chain else "",
            prompt=prompt,
            aspect_ratio=aspect,
        )


# ---------------------------------------------------------------------------
# OpenRouter Image API provider (dedicated /api/v1/images endpoint)
# ---------------------------------------------------------------------------

# OpenRouter Image API aspect ratios (full spec)
_IMAGE_API_ASPECT_RATIOS = {
    "square": "1:1",
    "landscape": "16:9",
    "portrait": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
    "3:2": "3:2",
    "2:3": "2:3",
    "9:19.5": "9:19.5",
    "19.5:9": "19.5:9",
    "9:20": "9:20",
    "20:9": "20:9",
    "1:2": "1:2",
    "2:1": "2:1",
}

_IMAGE_API_RESOLUTIONS = {"1k": "1K", "2k": "2K"}
_IMAGE_API_DEFAULT_RESOLUTION = "1K"
_IMAGE_API_DEFAULT_MODEL = "x-ai/grok-imagine-image-2.0"
# Quality options (deliberately excludes "high" as a cost guard — the Image API
# per-call charge scales with the quality tier and the marginal visual improvement
# from "high" over "medium" on the default resolution is negligible for prompt
# generations. Pet sprites are uniformly generated at "medium" anyway.)
_IMAGE_API_QUALITY_OPTIONS = {"low", "medium"}


class OpenRouterImageAPIProvider(ImageGenProvider):
    """Image generation via OpenRouter's dedicated ``/api/v1/images`` endpoint.

    This is the correct OpenRouter API surface for models that expose a pure
    image-generation API (e.g. ``x-ai/grok-imagine-image-2.0``). It is a
    separate endpoint from the chat-completions path used by
    :class:`OpenRouterCompatImageProvider` — different request/response shapes,
    different parameter conventions.
    """

    @property
    def name(self) -> str:
        return "openrouter-image-api"

    @property
    def display_name(self) -> str:
        return "OpenRouter Image API"

    def _resolve_runtime(self) -> Dict[str, str]:
        """Resolve OpenRouter credentials via the shared runtime resolver."""
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested="openrouter")
        api_key = str(runtime.get("api_key") or "").strip()
        base_url = str(runtime.get("base_url") or "https://openrouter.ai/api/v1").strip().rstrip("/")
        return {"api_key": api_key, "base_url": base_url}

    def is_available(self) -> bool:
        try:
            runtime = self._resolve_runtime()
            return bool(str(runtime.get("api_key") or "").strip())
        except Exception:
            return False

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": "OpenRouter Image API",
            "badge": "paid",
            "tag": (
                "x-ai/grok-imagine-image-2.0 via OpenRouter's /api/v1/images endpoint; "
                "uses OPENROUTER_API_KEY"
            ),
            "env_vars": [
                {
                    "key": "OPENROUTER_API_KEY",
                    "prompt": "OpenRouter API key",
                    "url": "https://openrouter.ai/keys",
                }
            ],
        }

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": _IMAGE_API_DEFAULT_MODEL,
                "display": "Grok Imagine Image 2.0",
                "speed": "~5-10s",
                "strengths": "Latest Grok Imagine — OpenRouter Image API",
            },
        ]

    def default_model(self) -> Optional[str]:
        return _IMAGE_API_DEFAULT_MODEL

    def capabilities(self) -> Dict[str, Any]:
        return {
            "modalities": ["text", "image"],
            "max_reference_images": 3,
        }

    def _resolve_image_api_model(self) -> str:
        """Resolve model from env var or provider-scoped config only.

        Does NOT fall back to the top-level ``image_gen.model`` because that
        config key is shared across *all* image backends. A user whose global
        ``image_gen.model`` targets a chat-completions model (e.g.
        ``google/gemini-3-pro-image``) would have that model silently sent to
        ``/api/v1/images``, where it doesn't exist — producing a confusing 404.
        The resolver stops at the provider-scoped layer and uses the dedicated
        default instead.
        """
        env_override = os.environ.get("OPENROUTER_IMAGES_API_MODEL", "").strip()
        if env_override:
            return env_override
        cfg = _load_image_gen_config()
        scoped = cfg.get("openrouter-image-api") if isinstance(cfg.get("openrouter-image-api"), dict) else {}
        value = scoped.get("model") if isinstance(scoped.get("model"), str) else None
        if value and value.strip():
            return value.strip()
        return _IMAGE_API_DEFAULT_MODEL

    def generate(
        self,
        prompt: str,
        aspect_ratio: str = DEFAULT_ASPECT_RATIO,
        *,
        image_url: Optional[str] = None,
        reference_image_urls: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        import requests

        try:
            runtime = self._resolve_runtime()
        except Exception as exc:
            return error_response(
                error=f"Could not resolve OpenRouter credentials: {exc}",
                error_type="missing_api_key",
                provider=self.name,
                aspect_ratio=aspect_ratio,
            )

        api_key = runtime["api_key"]
        base_url = runtime["base_url"]
        if not api_key:
            return error_response(
                error="No OpenRouter API key found. "
                "Set OPENROUTER_API_KEY in ~/.hermes/.env or run `hermes auth add openrouter`.",
                error_type="missing_api_key",
                provider=self.name,
                aspect_ratio=aspect_ratio,
            )

        model_id = self._resolve_image_api_model()
        # Accept raw aspect_ratio keys directly (the API supports many more
        # ratios than the standard three, e.g. "4:3", "3:2", "9:19.5"),
        # then fall back to the standard normalizer for the common names.
        raw = aspect_ratio.strip().lower() if isinstance(aspect_ratio, str) else ""
        if raw in _IMAGE_API_ASPECT_RATIOS:
            or_aspect = _IMAGE_API_ASPECT_RATIOS[raw]
            aspect = raw
        else:
            aspect = resolve_aspect_ratio(aspect_ratio)
            or_aspect = _IMAGE_API_ASPECT_RATIOS.get(aspect)
        if or_aspect is None:
            valid = sorted(_IMAGE_API_ASPECT_RATIOS)
            return error_response(
                error=f"Unsupported aspect ratio '{aspect_ratio}'. "
                f"Valid values: {', '.join(valid)}",
                error_type="invalid_aspect_ratio",
                provider=self.name,
                model=model_id,
                prompt=prompt,
            )

        payload: Dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "n": 1,
            "aspect_ratio": or_aspect,
        }

        # Resolution
        res_env = os.environ.get("OPENROUTER_IMAGES_API_RESOLUTION", "").strip().lower()
        if not res_env:
            cfg = _load_image_gen_config()
            scoped = cfg.get("openrouter-image-api") if isinstance(cfg.get("openrouter-image-api"), dict) else {}
            res_cfg = scoped.get("resolution") if isinstance(scoped.get("resolution"), str) else None
            if res_cfg:
                res_env = res_cfg.strip().lower()
        payload["resolution"] = _IMAGE_API_RESOLUTIONS.get(res_env, _IMAGE_API_DEFAULT_RESOLUTION)

        # Quality
        quality_raw = kwargs.get("quality") or ""
        if quality_raw and str(quality_raw).strip().lower() in _IMAGE_API_QUALITY_OPTIONS:
            payload["quality"] = str(quality_raw).strip().lower()

        # Reference images (image-to-image via input_references)
        ref_images: List[str] = []
        if isinstance(image_url, str) and image_url.strip():
            ref_images.append(image_url.strip())
        if isinstance(reference_image_urls, list):
            for ref in reference_image_urls:
                if isinstance(ref, str) and ref.strip():
                    ref_images.append(ref.strip())
        if ref_images:
            from agent.file_safety import raise_if_read_blocked

            refs: List[Dict[str, Any]] = []
            for ref in ref_images[:3]:
                lower = ref.lower()
                if lower.startswith(("http://", "https://", "data:")):
                    refs.append({"type": "image_url", "image_url": {"url": ref}})
                else:
                    try:
                        raise_if_read_blocked(ref)
                        path = Path(ref).expanduser()
                        raw = path.read_bytes()
                        mime = mimetypes.guess_type(path.name)[0] or "image/png"
                        b64 = base64.b64encode(raw).decode("ascii")
                        refs.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
                    except Exception as exc:
                        return error_response(
                            error=f"Could not read reference image '{ref}': {exc}",
                            error_type="io_error",
                            provider=self.name,
                            model=model_id,
                            prompt=prompt,
                            aspect_ratio=aspect,
                        )
            if refs:
                payload["input_references"] = refs

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/NousResearch/hermes-agent",
            "X-Title": "Hermes Agent",
        }

        try:
            response = requests.post(
                f"{base_url}/images",
                headers=headers,
                json=payload,
                timeout=120,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            try:
                err_msg = resp.json().get("error", {}).get("message", resp.text[:300])
            except Exception:
                err_msg = resp.text[:300] if resp is not None else str(exc)
            logger.error("OpenRouter Image API failed (%d): %s", status, err_msg)
            return error_response(
                error=f"OpenRouter Image API failed ({status}): {err_msg}",
                error_type="api_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.Timeout:
            return error_response(
                error="OpenRouter Image API timed out (120s)",
                error_type="timeout",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )
        except requests.ConnectionError as exc:
            return error_response(
                error=f"OpenRouter connection error: {exc}",
                error_type="connection_error",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        try:
            result = response.json()
        except Exception as exc:
            return error_response(
                error=f"OpenRouter returned invalid JSON: {exc}",
                error_type="invalid_response",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        data = result.get("data", [])
        if not data:
            return error_response(
                error="OpenRouter returned no image data",
                error_type="empty_response",
                provider=self.name,
                model=model_id,
                prompt=prompt,
                aspect_ratio=aspect,
            )

        first = data[0]
        b64 = first.get("b64_json")
        media_type = first.get("media_type", "image/png")

        if b64:
            ext = mimetypes.guess_extension(media_type) or "png"
            ext = ext.lstrip(".")
            try:
                saved_path = save_b64_image(b64, prefix=f"openrouter_{model_id.replace('/', '_')}", extension=ext)
            except Exception as exc:
                return error_response(
                    error=f"Could not save image: {exc}",
                    error_type="io_error",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )
            image_ref = str(saved_path)
        else:
            url = first.get("url")
            if url:
                try:
                    saved_path = save_url_image(url, prefix=f"openrouter_{model_id.replace('/', '_')}")
                    image_ref = str(saved_path)
                except Exception as exc:
                    return error_response(
                        error=f"Could not cache image URL ({url}): {exc}",
                        error_type="io_error",
                        provider=self.name,
                        model=model_id,
                        prompt=prompt,
                        aspect_ratio=aspect,
                    )
            else:
                return error_response(
                    error="OpenRouter response contained neither b64_json nor URL",
                    error_type="empty_response",
                    provider=self.name,
                    model=model_id,
                    prompt=prompt,
                    aspect_ratio=aspect,
                )

        return success_response(
            image=image_ref,
            model=model_id,
            prompt=prompt,
            aspect_ratio=aspect,
            provider=self.name,
        )


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------


def _build_providers() -> List[ImageGenProvider]:
    return [
        OpenRouterCompatImageProvider(
            provider_name="openrouter",
            display_name="OpenRouter",
            runtime_name="openrouter",
            config_key="openrouter",
            model_env_var="OPENROUTER_IMAGE_MODEL",
            setup_schema={
                "name": "OpenRouter (image)",
                "badge": "paid",
                "tag": "Gemini Flash Image & more via OpenRouter; uses OPENROUTER_API_KEY",
                "env_vars": [
                    {
                        "key": "OPENROUTER_API_KEY",
                        "prompt": "OpenRouter API key",
                        "url": "https://openrouter.ai/keys",
                    }
                ],
            },
        ),
        OpenRouterCompatImageProvider(
            provider_name="nous",
            display_name="Nous Portal",
            runtime_name="nous",
            config_key="nous",
            model_env_var="NOUS_IMAGE_MODEL",
            setup_schema={
                "name": "Nous Portal (image)",
                "badge": "subscription",
                "tag": "Reference-grounded image generation via Nous Portal (OpenRouter-backed)",
                "env_vars": [],
                "requires_nous_auth": True,
            },
        ),
        OpenRouterImageAPIProvider(),
    ]


def register(ctx: Any) -> None:
    """Register the OpenRouter + Nous Portal + OpenRouter Image API providers."""
    for provider in _build_providers():
        ctx.register_image_gen_provider(provider)
