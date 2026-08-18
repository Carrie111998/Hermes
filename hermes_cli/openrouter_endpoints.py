"""Profile-scoped OpenRouter model-endpoint discovery.

The helper owns model-id validation, upstream HTTP, response normalization, and a
short in-memory cache. Credentials are accepted only as call arguments and are
never included in returned data, exception text, or logs.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_OPENROUTER_ENDPOINTS_BASE = "https://openrouter.ai/api/v1/models"
_MODEL_ID_MAX_LENGTH = 256
_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
_CACHE_TTL_SECONDS = 45.0
_MODEL_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class OpenRouterEndpointError(RuntimeError):
    """Safe, classified endpoint-discovery failure."""

    def __init__(self, status_code: int, message: str, *, recoverable: bool = True):
        self.status_code = status_code
        self.recoverable = recoverable
        super().__init__(message)


@dataclass
class _CacheEntry:
    payload: dict[str, Any]
    stored_at: float


_CACHE: dict[tuple[str, str], _CacheEntry] = {}
_CACHE_LOCK = threading.RLock()


def _clear_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def validate_openrouter_model_id(model: str) -> tuple[str, str]:
    """Validate ``author/slug`` and return its two unescaped path components."""
    if not isinstance(model, str):
        raise ValueError("OpenRouter model ID must be a string")
    model = model.strip()
    if not model or len(model) > _MODEL_ID_MAX_LENGTH:
        raise ValueError("OpenRouter model ID must be 1-256 characters")
    if model.count("/") != 1 or "\\" in model:
        raise ValueError("OpenRouter model ID must use exactly author/slug")
    author, slug = model.split("/", 1)
    if not author or not slug:
        raise ValueError("OpenRouter model ID requires non-empty author and slug")
    if author in {".", ".."} or slug in {".", ".."}:
        raise ValueError("OpenRouter model ID cannot contain traversal segments")
    if not _MODEL_PART_RE.fullmatch(author) or not _MODEL_PART_RE.fullmatch(slug):
        raise ValueError("OpenRouter model ID contains unsupported characters")
    return author, slug


def _copy_optional_scalar(
    raw: dict[str, Any], target: dict[str, Any], key: str
) -> None:
    value = raw.get(key)
    if isinstance(value, (str, int, float, bool)) or value is None and key in raw:
        target[key] = value


def normalize_openrouter_endpoint(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize one endpoint while omitting unavailable optional metrics."""
    if not isinstance(raw, dict):
        raise ValueError("OpenRouter endpoint must be an object")

    normalized: dict[str, Any] = {}
    provider_name = raw.get("provider_name") or raw.get("provider") or raw.get("name")
    tag = raw.get("tag") or raw.get("provider_tag") or raw.get("provider_slug")
    if isinstance(provider_name, str) and provider_name.strip():
        normalized["provider_name"] = provider_name.strip()
    if isinstance(tag, str) and tag.strip():
        normalized["tag"] = tag.strip()

    for key in (
        "quantization",
        "status",
        "context_length",
        "latency",
        "throughput",
        "uptime",
    ):
        _copy_optional_scalar(raw, normalized, key)

    for normalized_key, upstream_key in (
        ("latency", "latency_last_30m"),
        ("throughput", "throughput_last_30m"),
        ("uptime", "uptime_last_30m"),
    ):
        if normalized_key not in normalized:
            value = raw.get(upstream_key)
            if isinstance(value, (str, int, float, bool)) or (
                value is None and upstream_key in raw
            ):
                normalized[normalized_key] = value

    pricing = raw.get("pricing")
    if isinstance(pricing, dict):
        normalized_pricing = {
            key: pricing[key]
            for key in (
                "prompt",
                "completion",
                "request",
                "image",
                "input_cache_read",
                "input_cache_write",
                "discount",
            )
            if key in pricing
            and (pricing[key] is None or isinstance(pricing[key], (str, int, float)))
        }
        if normalized_pricing:
            normalized["pricing"] = normalized_pricing

    supported = raw.get("supported_parameters")
    if isinstance(supported, list):
        normalized["supported_parameters"] = [
            item.strip() for item in supported if isinstance(item, str) and item.strip()
        ]
    return normalized


def _cache_result(entry: _CacheEntry, *, stale: bool = False) -> dict[str, Any]:
    result = copy.deepcopy(entry.payload)
    result["cached"] = True
    if stale:
        result["stale"] = True
    return result


def _cached_entry(cache_key: tuple[str, str]) -> _CacheEntry | None:
    with _CACHE_LOCK:
        return _CACHE.get(cache_key)


def _classified_http_error(status: int) -> OpenRouterEndpointError:
    if status in {401, 403}:
        return OpenRouterEndpointError(
            status,
            "OpenRouter endpoint discovery is unavailable for the current credential",
            recoverable=True,
        )
    if status == 404:
        return OpenRouterEndpointError(
            404, "OpenRouter model was not found", recoverable=True
        )
    if status == 429:
        return OpenRouterEndpointError(
            429, "OpenRouter endpoint discovery is rate limited", recoverable=True
        )
    if status >= 500:
        return OpenRouterEndpointError(
            503, "OpenRouter endpoint discovery is temporarily unavailable"
        )
    return OpenRouterEndpointError(502, "OpenRouter endpoint discovery failed")


def fetch_openrouter_endpoints(
    model: str,
    *,
    api_key: str,
    timeout: float = 8.0,
    profile_id: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch normalized endpoints, using a short profile/model-scoped cache."""
    author, slug = validate_openrouter_model_id(model)
    normalized_model = f"{author}/{slug}"
    if not isinstance(api_key, str) or not api_key.strip():
        raise OpenRouterEndpointError(401, "OpenRouter API key is not configured")

    cache_key = (str(profile_id or ""), normalized_model)
    cached = _cached_entry(cache_key)
    if (
        not refresh
        and cached is not None
        and time.monotonic() - cached.stored_at < _CACHE_TTL_SECONDS
    ):
        return _cache_result(cached)

    encoded_author = urllib.parse.quote(author, safe="")
    encoded_slug = urllib.parse.quote(slug, safe="")
    url = f"{_OPENROUTER_ENDPOINTS_BASE}/{encoded_author}/{encoded_slug}/endpoints"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Accept": "application/json",
            "User-Agent": "Hermes-Agent/OpenRouter-Endpoint-Discovery",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(_RESPONSE_MAX_BYTES + 1)
        if len(body) > _RESPONSE_MAX_BYTES:
            raise OpenRouterEndpointError(
                502, "OpenRouter endpoint response was too large"
            )
        decoded = json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code >= 500 and cached is not None:
            logger.warning(
                "OpenRouter endpoint refresh failed with HTTP %s for model %s; using stale cache",
                exc.code,
                normalized_model,
            )
            return _cache_result(cached, stale=True)
        raise _classified_http_error(exc.code) from None
    except (socket.timeout, TimeoutError) as exc:
        if cached is not None:
            logger.warning(
                "OpenRouter endpoint refresh timed out for model %s; using stale cache",
                normalized_model,
            )
            return _cache_result(cached, stale=True)
        raise OpenRouterEndpointError(
            504, "OpenRouter endpoint discovery timed out"
        ) from None
    except urllib.error.URLError as exc:
        if cached is not None:
            logger.warning(
                "OpenRouter endpoint refresh failed for model %s; using stale cache",
                normalized_model,
            )
            return _cache_result(cached, stale=True)
        raise OpenRouterEndpointError(
            503, "OpenRouter endpoint discovery is unavailable"
        ) from None
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        logger.warning(
            "OpenRouter endpoint response was malformed for model %s", normalized_model
        )
        raise OpenRouterEndpointError(
            502, "OpenRouter returned malformed endpoint data"
        ) from None

    data = decoded.get("data") if isinstance(decoded, dict) else None
    if not isinstance(data, dict):
        raise OpenRouterEndpointError(
            502, "OpenRouter returned malformed endpoint data"
        )
    raw_endpoints = data.get("endpoints")
    if not isinstance(raw_endpoints, list):
        raise OpenRouterEndpointError(
            502, "OpenRouter returned malformed endpoint data"
        )

    endpoints: list[dict[str, Any]] = []
    for raw in raw_endpoints:
        if not isinstance(raw, dict):
            continue
        normalized = normalize_openrouter_endpoint(raw)
        if normalized:
            endpoints.append(normalized)

    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {
        "model": normalized_model,
        "endpoints": endpoints,
        "cached": False,
        "fetched_at": fetched_at,
    }
    with _CACHE_LOCK:
        _CACHE[cache_key] = _CacheEntry(copy.deepcopy(payload), time.monotonic())
    return payload
