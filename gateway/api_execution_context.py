"""Safe durable execution context for detached API completion turns.

The API server accepts request-scoped model routing controls that are not part
of the transcript.  A detached subagent completion must replay those controls
instead of silently falling back to the API server's virtual/default model.

Only non-secret, execution-relevant values are admitted here.  Provider
credentials, raw base URLs, arbitrary model-option blobs, and ephemeral
system prompts are deliberately excluded.  Credential-free transport
semantics are retained only as stable digests.  An API turn with a non-empty
ephemeral prompt is therefore not eligible for detached delivery and must run
its delegation synchronously.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


SCHEMA = "hermes.api-detached-execution-context.v1"
MAX_SERIALIZED_BYTES = 8 * 1024
MAX_SESSION_KEY_CHARS = 256
MAX_MODEL_CHARS = 512
MAX_PROVIDER_CHARS = 80
MAX_ROUTE_SOURCE_CHARS = 64
MAX_ALIAS_CHARS = 256
MAX_OPTION_CHARS = 32
MAX_API_MODE_CHARS = 80
MAX_BASE_URL_CHARS = 2_048

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_API_MODE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,79}")
_SUPPORTED_API_MODES = frozenset(
    {
        "chat_completions",
        "codex_responses",
        "anthropic_messages",
        "bedrock_converse",
        "codex_app_server",
    }
)
_PROVIDER_SLUG_RE = re.compile(
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"|custom:[a-z0-9]+(?:[._-][a-z0-9]+)*)"
)
_SESSION_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_.:()+-]{0,255}")
_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_PRIORITY_SERVICE_TIER_ALIASES = frozenset({"fast", "priority", "on"})
_DEFAULT_SERVICE_TIER_ALIASES = frozenset(
    {"normal", "default", "standard", "off", "none"}
)
_ROUTE_SOURCES = frozenset(
    {
        "global",
        "model_routes",
        "raw_request",
        "session_model_lock",
        "session_model_override",
    }
)
_ALLOWED_KEYS = frozenset(
    {
        "schema",
        "gateway_session_key",
        "request_model",
        "request_provider",
        "model_options",
        "route_alias",
        "route_model",
        "route_provider",
        "route_semantic_sha256",
        "session_model",
        "confirmed_runtime_lock",
        "requested_runtime",
        "route_source",
        "effective_model",
        "effective_provider",
        "effective_transport_sha256",
    }
)


class ApiExecutionContextError(ValueError):
    """Raised when detached API execution metadata is unsafe or malformed."""


def _clean_string(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_empty: bool = True,
) -> str:
    if value is None and allow_empty:
        return ""
    if not isinstance(value, str):
        raise ApiExecutionContextError(f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned and not allow_empty:
        raise ApiExecutionContextError(f"{field} cannot be empty")
    if len(cleaned) > max_chars:
        raise ApiExecutionContextError(f"{field} is too long")
    if _CONTROL_RE.search(cleaned):
        raise ApiExecutionContextError(f"{field} contains control characters")
    return cleaned


def _clean_durable_string(
    value: Any,
    *,
    field: str,
    max_chars: int,
    allow_empty: bool = True,
) -> str:
    """Normalize a durable string and reject centrally-known secret shapes.

    This is an admission boundary, not an egress redaction boundary.  Silently
    persisting the redacted replacement would make the replay contract differ
    from the originating turn, while retaining the input would smuggle a
    credential into the durable completion ledger.  A value that Hermes'
    authoritative forced redactor would change is therefore ineligible.
    """

    cleaned = _clean_string(
        value,
        field=field,
        max_chars=max_chars,
        allow_empty=allow_empty,
    )
    if not cleaned:
        return cleaned

    # Import lazily so this narrow schema module does not make agent startup or
    # SessionDB import the broader redaction module until it admits a value.
    from agent.redact import redact_sensitive_text

    if redact_sensitive_text(cleaned, force=True) != cleaned:
        raise ApiExecutionContextError(
            f"{field} contains secret-like material"
        )
    return cleaned


def normalize_model_identifier(
    value: Any,
    *,
    field: str = "model",
    allow_empty: bool = True,
) -> str:
    """Return a replay-safe provider model id without narrowing vendor syntax."""

    return _clean_durable_string(
        value,
        field=field,
        max_chars=MAX_MODEL_CHARS,
        allow_empty=allow_empty,
    )


def normalize_provider_slug(
    value: Any,
    *,
    field: str = "provider",
    allow_empty: bool = True,
) -> str:
    """Return a canonical provider slug, including ``custom:<slug>``."""

    cleaned = _clean_durable_string(
        value,
        field=field,
        max_chars=MAX_PROVIDER_CHARS,
        allow_empty=allow_empty,
    )
    canonical = cleaned.lower()
    if canonical and _PROVIDER_SLUG_RE.fullmatch(canonical) is None:
        raise ApiExecutionContextError(f"{field} has invalid provider syntax")
    return canonical


def normalize_session_provider_identifier(
    value: Any,
    *,
    field: str = "provider",
    allow_empty: bool = True,
) -> str:
    """Normalize the broader provider identities used by model switching.

    Named custom providers may contain endpoint-derived punctuation such as
    ``custom:local-(127.0.0.1:4141)``. They are safe for session replay only
    after exact binding to the freshly resolved provider endpoint, but must not
    be rejected merely because the narrower detached-API slug grammar cannot
    represent them.
    """

    cleaned = _clean_durable_string(
        value,
        field=field,
        max_chars=256,
        allow_empty=allow_empty,
    )
    canonical = cleaned.lower()
    if canonical and _SESSION_PROVIDER_RE.fullmatch(canonical) is None:
        raise ApiExecutionContextError(
            f"{field} has invalid session-provider syntax"
        )
    return canonical


def normalize_durable_slug(
    value: Any,
    *,
    field: str = "durable_slug",
    allow_empty: bool = True,
) -> str:
    """Return one replay-safe canonical label with bounded slug syntax."""

    cleaned = _clean_durable_string(
        value,
        field=field,
        max_chars=MAX_API_MODE_CHARS,
        allow_empty=allow_empty,
    )
    canonical = cleaned.lower()
    if canonical and _API_MODE_RE.fullmatch(canonical) is None:
        raise ApiExecutionContextError(f"{field} is unsupported")
    return canonical


def normalize_api_mode(
    value: Any,
    *,
    field: str = "api_mode",
    allow_empty: bool = True,
) -> str:
    """Return one supported Hermes wire-protocol mode."""

    canonical = normalize_durable_slug(
        value,
        field=field,
        allow_empty=allow_empty,
    )
    if canonical and canonical not in _SUPPORTED_API_MODES:
        raise ApiExecutionContextError(f"{field} is unsupported")
    return canonical


def validate_nonsecret_durable_metadata(
    value: Any,
    *,
    field: str = "durable_metadata",
) -> Any:
    """Reject secret-shaped material anywhere in JSON metadata.

    Unlike transcript content, model/runtime metadata is replayed as host
    configuration.  Persisting an opaque credential under an otherwise
    unknown key would therefore bypass the field-specific identifier guards.
    Serialize the whole value and apply the authoritative forced redactor so
    sensitive JSON key names (for example ``api_key``) are covered as well as
    known token prefixes.  The original value is returned unchanged after
    validation so unrelated, JSON-safe lineage/config fields remain intact.
    """

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ApiExecutionContextError(
            f"{field} must be JSON serializable"
        ) from exc

    from agent.redact import redact_sensitive_text

    if redact_sensitive_text(encoded, force=True) != encoded:
        raise ApiExecutionContextError(
            f"{field} contains secret-like material"
        )
    return value


def normalize_service_tier(
    value: Any,
    *,
    field: str = "service_tier",
) -> Optional[str]:
    """Canonicalize service-tier aliases to ``"priority"`` or ``None``."""

    cleaned = _clean_durable_string(
        value,
        field=field,
        max_chars=MAX_OPTION_CHARS,
    )
    if not cleaned:
        return None
    canonical = cleaned.lower()
    if canonical in _PRIORITY_SERVICE_TIER_ALIASES:
        return "priority"
    if canonical in _DEFAULT_SERVICE_TIER_ALIASES:
        return None
    raise ApiExecutionContextError(f"{field} is unsupported")


def normalize_route_source(
    value: Any,
    *,
    field: str = "route_source",
    default: str = "global",
) -> str:
    """Return one canonical provenance enum for a durable route."""

    candidate = default if value is None or value == "" else value
    cleaned = _clean_durable_string(
        candidate,
        field=field,
        max_chars=MAX_ROUTE_SOURCE_CHARS,
        allow_empty=False,
    )
    canonical = cleaned.lower()
    if canonical not in _ROUTE_SOURCES:
        raise ApiExecutionContextError(f"{field} is unsupported")
    return canonical


def _clean_sha256(
    value: Any,
    *,
    field: str,
    allow_empty: bool = True,
) -> str:
    digest = _clean_string(
        value,
        field=field,
        max_chars=64,
        allow_empty=allow_empty,
    )
    if digest and _SHA256_RE.fullmatch(digest) is None:
        raise ApiExecutionContextError(f"{field} must be a SHA-256 digest")
    return digest


def canonicalize_transport_base_url(value: Any) -> str:
    """Return a credential-free canonical HTTP(S) endpoint.

    Userinfo, query strings, and fragments can carry credentials or other
    request-local authority.  Detached delivery cannot safely persist even a
    digest derived from them, so those forms make the turn ineligible.
    """

    raw = _clean_durable_string(
        value,
        field="transport.base_url",
        max_chars=MAX_BASE_URL_CHARS,
    )
    if not raw:
        return ""
    if "\\" in raw or any(char.isspace() for char in raw):
        raise ApiExecutionContextError(
            "transport.base_url contains unsafe characters"
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ApiExecutionContextError(
            "transport.base_url is malformed"
        ) from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiExecutionContextError(
            "transport.base_url must be an absolute HTTP(S) URL"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ApiExecutionContextError(
            "transport.base_url contains request-local credentials or state"
        )
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ApiExecutionContextError(
            "transport.base_url must include a host"
        )
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    netloc = (
        rendered_host
        if port is None or port == default_port
        else f"{rendered_host}:{port}"
    )
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def canonicalize_session_endpoint(value: Any) -> str:
    """Canonicalize a credential-free endpoint for exact replay binding.

    Session model overrides can legitimately target virtual transports
    (``acp://``/``moa://``) and Azure endpoints whose non-secret query is part
    of the configured route. Unlike detached API metadata, this value is never
    authoritative by itself: replay requires exact canonical equality with the
    freshly resolved provider endpoint before credentials are attached.
    """

    raw = _clean_durable_string(
        value,
        field="session endpoint",
        max_chars=MAX_BASE_URL_CHARS,
    )
    if not raw:
        return ""
    if "\\" in raw or any(char.isspace() for char in raw):
        raise ApiExecutionContextError(
            "session endpoint contains unsafe characters"
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ApiExecutionContextError("session endpoint is malformed") from exc
    scheme = parsed.scheme.lower()
    if (
        not scheme
        or _API_MODE_RE.fullmatch(scheme) is None
        or not parsed.netloc
    ):
        raise ApiExecutionContextError(
            "session endpoint must be an absolute transport URL"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ApiExecutionContextError(
            "session endpoint contains credentials or a fragment"
        )
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ApiExecutionContextError("session endpoint must include a host")
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443 if scheme == "https" else None
    netloc = (
        rendered_host
        if port is None or port == default_port
        else f"{rendered_host}:{port}"
    )
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def transport_semantic_digest(
    *,
    model: Any,
    provider: Any,
    base_url: Any,
    api_mode: Any,
) -> str:
    """Fingerprint effective transport semantics without credential bytes."""

    normalized_model = normalize_model_identifier(
        model,
        field="transport.model",
        allow_empty=False,
    )
    normalized_provider = normalize_provider_slug(
        provider,
        field="transport.provider",
    )
    normalized_mode = normalize_api_mode(
        api_mode,
        field="transport.api_mode",
    )
    canonical = json.dumps(
        {
            "api_mode": normalized_mode,
            "base_url": canonicalize_transport_base_url(base_url),
            "model": normalized_model,
            "provider": normalized_provider,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def normalize_model_options(value: Any) -> Dict[str, Any]:
    """Return the exact safe subset consumed by API agent construction."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ApiExecutionContextError("model_options must be an object")

    out: Dict[str, Any] = {}
    reasoning = value.get("reasoning")
    effort = value.get("reasoning_effort")
    enabled: Any = None
    if isinstance(reasoning, Mapping):
        enabled = reasoning.get("enabled")
        effort = reasoning.get("effort", effort)
    elif reasoning is not None:
        raise ApiExecutionContextError("model_options.reasoning must be an object")

    effort_norm = ""
    if effort is not None:
        effort_norm = _clean_string(
            str(effort).lower(),
            field="model_options.reasoning.effort",
            max_chars=MAX_OPTION_CHARS,
        )
        if effort_norm not in _REASONING_EFFORTS:
            raise ApiExecutionContextError(
                "model_options reasoning effort is unsupported"
            )
    canonical_reasoning: Dict[str, Any] = {}
    if enabled is False or effort_norm == "none":
        canonical_reasoning["enabled"] = False
    elif effort_norm:
        canonical_reasoning["enabled"] = True
        canonical_reasoning["effort"] = effort_norm
    elif enabled is True:
        canonical_reasoning["enabled"] = True
    elif enabled not in (None, False):
        raise ApiExecutionContextError(
            "model_options.reasoning.enabled must be a boolean"
        )
    if canonical_reasoning:
        out["reasoning"] = canonical_reasoning

    if "service_tier" in value:
        out["service_tier"] = normalize_service_tier(
            value.get("service_tier"),
            field="model_options.service_tier",
        )
    elif "fast" in value:
        fast = value.get("fast")
        if not isinstance(fast, bool):
            raise ApiExecutionContextError("model_options.fast must be a boolean")
        # Canonicalize the compatibility flag to the value the API adapter
        # actually applies to AIAgent.service_tier.
        out["service_tier"] = "priority" if fast else None
    return out


def normalize_requested_runtime(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ApiExecutionContextError("requested_runtime must be an object")
    return {
        "model": normalize_model_identifier(
            value.get("model"),
            field="requested_runtime.model",
        ),
        "provider": normalize_provider_slug(
            value.get("provider"),
            field="requested_runtime.provider",
        ),
    }


def normalize_api_execution_context(
    value: Any,
    *,
    allow_none: bool = True,
) -> Optional[Dict[str, Any]]:
    """Validate a versioned detached-API context at every trust boundary."""

    if value is None:
        if allow_none:
            return None
        raise ApiExecutionContextError("API execution context is required")
    if not isinstance(value, Mapping):
        raise ApiExecutionContextError("API execution context must be an object")
    unknown = set(value) - _ALLOWED_KEYS
    if unknown:
        raise ApiExecutionContextError(
            "API execution context contains unsupported fields: "
            + ", ".join(sorted(str(key) for key in unknown))
        )
    if value.get("schema") != SCHEMA:
        raise ApiExecutionContextError("unsupported API execution context schema")

    confirmed_runtime_lock = value.get("confirmed_runtime_lock", False)
    if type(confirmed_runtime_lock) is not bool:
        raise ApiExecutionContextError(
            "confirmed_runtime_lock must be a boolean"
        )
    route_source = normalize_route_source(value.get("route_source"))
    out: Dict[str, Any] = {
        "schema": SCHEMA,
        "gateway_session_key": _clean_durable_string(
            value.get("gateway_session_key"),
            field="gateway_session_key",
            max_chars=MAX_SESSION_KEY_CHARS,
        ),
        "request_model": normalize_model_identifier(
            value.get("request_model"),
            field="request_model",
        ),
        "request_provider": normalize_provider_slug(
            value.get("request_provider"),
            field="request_provider",
        ),
        "model_options": normalize_model_options(value.get("model_options")),
        "route_alias": _clean_durable_string(
            value.get("route_alias"),
            field="route_alias",
            max_chars=MAX_ALIAS_CHARS,
        ),
        "route_model": normalize_model_identifier(
            value.get("route_model"),
            field="route_model",
        ),
        "route_provider": normalize_provider_slug(
            value.get("route_provider"),
            field="route_provider",
        ),
        "route_semantic_sha256": _clean_sha256(
            value.get("route_semantic_sha256"),
            field="route_semantic_sha256",
        ),
        "session_model": normalize_model_identifier(
            value.get("session_model"),
            field="session_model",
        ),
        "confirmed_runtime_lock": confirmed_runtime_lock,
        "requested_runtime": normalize_requested_runtime(
            value.get("requested_runtime")
        ),
        "route_source": route_source,
        "effective_model": normalize_model_identifier(
            value.get("effective_model"),
            field="effective_model",
        ),
        "effective_provider": normalize_provider_slug(
            value.get("effective_provider"),
            field="effective_provider",
        ),
        "effective_transport_sha256": _clean_sha256(
            value.get("effective_transport_sha256"),
            field="effective_transport_sha256",
            allow_empty=False,
        ),
    }
    if bool(out["route_alias"]) != bool(out["route_model"]):
        raise ApiExecutionContextError(
            "route_alias and route_model must be supplied together"
        )
    if not out["effective_model"]:
        raise ApiExecutionContextError(
            "effective_model is required for detached API delivery"
        )
    if bool(out["route_alias"]) != bool(out["route_semantic_sha256"]):
        raise ApiExecutionContextError(
            "aliased routes require a semantic transport digest"
        )
    encoded = json.dumps(
        out,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > MAX_SERIALIZED_BYTES:
        raise ApiExecutionContextError("API execution context is too large")
    return out


def execution_context_digest(value: Any) -> str:
    """Return a stable digest without exposing the context on HTTP headers."""

    normalized = normalize_api_execution_context(value)
    canonical = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ApiExecutionContextError",
    "SCHEMA",
    "canonicalize_transport_base_url",
    "execution_context_digest",
    "normalize_api_execution_context",
    "normalize_model_identifier",
    "normalize_model_options",
    "normalize_provider_slug",
    "normalize_requested_runtime",
    "normalize_route_source",
    "normalize_service_tier",
    "transport_semantic_digest",
]
