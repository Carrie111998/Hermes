"""Provider-scoped request identity policy for ambiguous retry recovery."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import stat
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

_DEEPSEEK_REQUEST_MODELS = {
    "homelab/deepseek-v4-flash",
    "homelab/deepseek-v4-flash-0731",
    "deepseek-v4-flash",
    "deepseek-v4-flash-0731",
}
logger = logging.getLogger(__name__)
_SECRET_WARNINGS: set[str] = set()
_ROUTE_OVERRIDE_KEYS = {"api_base", "base_url", "client", "http_client", "provider"}
_TRANSPORT_KEYS = {
    "api_key",
    "client",
    "http_client",
    "max_retries",
    "timeout",
}
_SENSITIVE_OR_TRANSPORT_HEADERS = {
    "accept",
    "authorization",
    "baggage",
    "content-length",
    "content-type",
    "cookie",
    "idempotency-key",
    "proxy-authorization",
    "set-cookie",
    "traceparent",
    "tracestate",
    "user-agent",
    "x-api-key",
    "x-amzn-trace-id",
    "x-request-id",
}


def _semantic_headers(headers: dict) -> dict:
    semantic = {}
    for name, value in headers.items():
        normalized = str(name).lower()
        if (
            normalized in _SENSITIVE_OR_TRANSPORT_HEADERS
            or normalized.startswith("x-b3-")
            or normalized.startswith("x-ot-")
            or normalized.startswith("x-trace-")
        ):
            continue
        semantic[normalized] = value
    return semantic


def _canonicalize(value):
    """Represent supported SDK-body values without process-local repr data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"__bytes__": value.hex()}
    if isinstance(value, Enum):
        return {
            "__enum__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _canonicalize(value.value),
        }
    if isinstance(value, (datetime.date, datetime.time)):
        return {
            "__type__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.isoformat(),
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _canonicalize(dataclasses.asdict(value)),
        }
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return {
            "__model__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": _canonicalize(model_dump(mode="json")),
        }
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonicalize(item) for item in value]
        return {
            "__set__": sorted(
                items,
                key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ),
            )
        }
    type_name = f"{type(value).__module__}.{type(value).__qualname__}"
    try:
        attrs = vars(value)
    except TypeError as exc:
        raise TypeError(f"unsupported canonical value: {type_name}") from exc
    return {"__type__": type_name, "attrs": _canonicalize(attrs)}


def _is_transport_control(key, value) -> bool:
    normalized = str(key).lower()
    return (
        normalized in _TRANSPORT_KEYS
        or normalized.startswith("_")
        or normalized.startswith("on_")
        or normalized.endswith("callback")
        or normalized.endswith("callbacks")
        or normalized.endswith("hook")
        or normalized.endswith("hooks")
        or callable(value)
    )


def _warn_secret_once(reason: str, message: str) -> None:
    if reason not in _SECRET_WARNINGS:
        _SECRET_WARNINGS.add(reason)
        logger.warning(message)


def load_deepseek_identity_secret() -> str | None:
    """Load a valid dedicated HMAC secret from a protected file."""
    path = os.environ.get("HERMES_DEEPSEEK_IDEMPOTENCY_SECRET_FILE", "").strip()
    if not path:
        return None
    secret_path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(secret_path, flags)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                _warn_secret_once(
                    "insecure",
                    "DeepSeek idempotency secret must be a mode-600 regular file owned by the service user and at most 4096 bytes",
                )
                return None
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                secret = handle.read(4097).strip()
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeError):
        _warn_secret_once("unreadable", "DeepSeek idempotency secret file could not be read")
        return None
    if len(secret) < 32:
        _warn_secret_once(
            "too-short",
            "DeepSeek idempotency secret must contain at least 32 characters",
        )
        return None
    return secret


class RequestSecretSnapshot:
    """Lazily pin one secret for all retries of a logical request."""

    def __init__(self, loader):
        self._loader = loader
        self._loaded = False
        self._value = None

    def __call__(self):
        if not self._loaded:
            self._value = self._loader()
            self._loaded = True
        return self._value


def apply_deepseek_request_identity(
    api_kwargs: dict, *, api_request_id, provider, model, base_url,
    identity_secret=None,
    identity_secret_loader=None,
) -> dict:
    """Add stable request and idempotency IDs to homelab DeepSeek calls.

    The effective post-middleware model controls the gate. Idempotency hashes
    cover semantic body, query, and non-sensitive routing headers. Unsupported
    values fail open: X-Request-ID remains, while Idempotency-Key is omitted.
    """
    try:
        parsed = urlparse(str(base_url or ""))
        if not isinstance(api_kwargs, dict):
            return api_kwargs
        if any(key in api_kwargs for key in _ROUTE_OVERRIDE_KEYS):
            return api_kwargs
        effective_model = api_kwargs.get("model", model)
        effective_destination = {
            "scheme": parsed.scheme.lower(),
            "hostname": parsed.hostname,
            "port": parsed.port or 443,
            "path": parsed.path or "/",
            "query": parsed.query,
        }
        if (
            str(provider or "").lower() != "homelab"
            or parsed.scheme.lower() != "https"
            or parsed.hostname != "ai.homelab.samaschke.de"
            or str(effective_model or "").strip().lower()
            not in _DEEPSEEK_REQUEST_MODELS
        ):
            return api_kwargs
    except Exception:
        return api_kwargs

    raw_headers = api_kwargs.get("extra_headers")
    try:
        headers = dict(raw_headers) if raw_headers is not None else {}
    except (TypeError, ValueError):
        return api_kwargs

    header_names = {str(name).lower() for name in headers}
    request_id = str(api_request_id)
    if "x-request-id" not in header_names:
        if re.fullmatch(r"[A-Za-z0-9._:/-]{1,200}", request_id):
            headers["X-Request-ID"] = request_id
        else:
            headers["X-Request-ID"] = "hermes-" + hashlib.sha256(
                request_id.encode("utf-8", errors="replace")
            ).hexdigest()[:32]

    canonical_kwargs = {
        key: value
        for key, value in api_kwargs.items()
        if key != "extra_headers" and not _is_transport_control(key, value)
    }
    canonical_kwargs["__destination__"] = effective_destination
    semantic_headers = _semantic_headers(headers)
    if semantic_headers:
        canonical_kwargs["extra_headers"] = semantic_headers

    try:
        canonical = json.dumps(
            _canonicalize(canonical_kwargs),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except Exception:
        api_kwargs["extra_headers"] = headers
        return api_kwargs

    if "idempotency-key" not in header_names:
        if identity_secret is None and callable(identity_secret_loader):
            try:
                identity_secret = identity_secret_loader()
            except Exception:
                identity_secret = None
        if not isinstance(identity_secret, str) or not identity_secret:
            api_kwargs["extra_headers"] = headers
            return api_kwargs
        try:
            material = request_id.encode("utf-8", errors="replace") + b"\0" + canonical
            hmac_key = hashlib.sha256(
                b"hermes-deepseek-idempotency-v1\0"
                + identity_secret.encode("utf-8", errors="strict")
            ).digest()
            headers["Idempotency-Key"] = "hermes-" + hmac.new(
                hmac_key, material, hashlib.sha256
            ).hexdigest()
        except Exception:
            api_kwargs["extra_headers"] = headers
            return api_kwargs
    api_kwargs["extra_headers"] = headers
    return api_kwargs
