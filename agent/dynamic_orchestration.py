"""Pure domain contracts for proposed dynamic multimodel orchestration.

This module intentionally has no runtime wiring, provider calls, persistence, or
credential-pool mutation. It establishes deterministic contracts that later
shadow-mode integration can consume.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import InitVar, asdict, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence, TypeVar
from urllib.parse import urlsplit, urlunsplit

ROUTE_CANONICALIZATION_VERSION = "route-v1"
QUALITY_COMPENSATION_SCHEMA_VERSION = "quality-compensation/v1"
_ROUTE_FIELDS = (
    "canonicalization_version",
    "provider",
    "product",
    "surface",
    "account_id",
    "entitlement_id",
    "billing_pool_id",
    "quota_pool_id",
    "model",
    "endpoint",
    "variant",
    "region",
)
_REQUIRED_ROUTE_FIELDS = (
    "provider",
    "product",
    "surface",
    "account_id",
    "billing_pool_id",
    "quota_pool_id",
    "model",
    "endpoint",
    "region",
)
_ASCII_WHITESPACE = " \t\r\n\f\v"
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")
_PROHIBITED_FIELD_NAMES = frozenset(
    {
        "prompt",
        "secret",
        "secrets",
        "client_secret",
        "credential",
        "credentials",
        "authorization",
        "api_key",
        "password",
        "passwd",
        "private_key",
        "token",
        "auth_token",
        "access_token",
        "refresh_token",
        "bearer_token",
        "raw_body",
        "raw_provider",
        "raw_provider_body",
        "raw_tool_result",
    }
)
_VERIFICATION_RANK = {f"V{level}": level for level in range(5)}
_ROUTE_ID_PATTERN = re.compile(r"^route-v1:[0-9a-f]{64}$")
_STABLE_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_STABLE_IDENTIFIERS = 32
_KNOWN_CAPABILITY_IDENTIFIERS = frozenset(
    {
        "browser.private",
        "filesystem.read",
        "filesystem.write",
        "network.read",
        "repository.read",
        "repository.write",
    }
)
_KNOWN_TOOL_IDENTIFIERS = frozenset(
    {
        "computer-use",
        "execute_code",
        "patch",
        "read_file",
        "search_files",
        "terminal",
        "web_extract",
        "web_search",
    }
)
_KNOWN_PERMISSION_IDENTIFIERS = frozenset(
    {
        "browser.private",
        "filesystem.read",
        "filesystem.write",
        "network.read",
        "repository.read",
        "repository.write",
    }
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9])bearer\s+\S+"
    r"|(?<![A-Za-z0-9])(?:sk-|gho_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+"
    r"|(?:password|passwd|secret|client[_-]?secret|api[_-]?key|prompt|credential|"
    r"authorization|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"private[_-]?key)\s*[:=]\s*\S+"
    r"|-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"
    r")",
    re.IGNORECASE,
)


class DomainValidationError(ValueError):
    """A stable, public validation error safe for deterministic tests and audit."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _validated_future_timestamp(
    value: object,
    field_name: str,
    *,
    reference_time: datetime | None = None,
) -> str:
    text = _task_text(value, field_name)
    if re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z",
        text,
    ) is None:
        raise DomainValidationError(
            "task.justification_expiry_invalid",
            f"{field_name} must be canonical UTC RFC 3339",
        )
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainValidationError(
            "task.justification_expiry_invalid",
            f"{field_name} must be an RFC 3339 timestamp",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DomainValidationError(
            "task.justification_expiry_invalid",
            f"{field_name} must include a timezone",
        )
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)
    elif reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise DomainValidationError(
            "task.reference_time_invalid",
            "reference_time must be timezone-aware",
        )
    if parsed <= reference_time:
        raise DomainValidationError(
            "task.justification_expired",
            f"{field_name} must be in the future",
        )
    return text


def _validated_mapping_keys(
    payload: object,
    *,
    code: str,
    location: str,
) -> set[str]:
    if not isinstance(payload, Mapping):
        raise DomainValidationError(code, f"{location} must be a mapping")
    if any(type(key) is not str for key in payload):
        raise DomainValidationError(code, f"{location} field names must be strings")
    return {str(key) for key in payload}


class TaskState(str, Enum):
    NEW = "NEW"
    PLANNED = "PLANNED"
    ROUTED = "ROUTED"
    DISPATCHED = "DISPATCHED"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"
    CANCELLED = "CANCELLED"


class AttemptState(str, Enum):
    CREATED = "CREATED"
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    RESULT_RECORDED = "RESULT_RECORDED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RouteState(str, Enum):
    DISCOVERED = "DISCOVERED"
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    COOLDOWN = "COOLDOWN"
    BREAKER_OPEN = "BREAKER_OPEN"
    RETIRED = "RETIRED"


class CredentialState(str, Enum):
    AVAILABLE = "AVAILABLE"
    EXHAUSTED = "EXHAUSTED"
    COOLDOWN = "COOLDOWN"
    DEAD = "DEAD"


class ReservationState(str, Enum):
    PENDING = "PENDING"
    HELD = "HELD"
    CONSUMED = "CONSUMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    RECONCILED = "RECONCILED"


class ReviewState(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PASSED = "PASSED"
    FAILED = "FAILED"
    HUMAN_APPROVED = "HUMAN_APPROVED"
    HUMAN_REJECTED = "HUMAN_REJECTED"


class ErrorKind(str, Enum):
    CAPACITY_EXHAUSTED = "capacity_exhausted"


class DecisionRelation(str, Enum):
    INITIAL = "INITIAL"
    FALLBACK = "FALLBACK"
    REPLAN = "REPLAN"
    WAITING = "WAITING"


class EscalationAction(str, Enum):
    BLOCK_DISPATCH = "BLOCK_DISPATCH"
    HUMAN_GATE = "HUMAN_GATE"


class EligibilityDisposition(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


_ELIGIBILITY_GATES = (
    "identity_policy",
    "privacy_permission",
    "capability_tool",
    "context",
    "freshness_confidence",
    "budget",
    "breaker_cooldown",
    "concurrency_reservation",
)


def _ascii_trimmed_nfc(value: object, *, field_name: str, code: str) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(code, f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value.strip(_ASCII_WHITESPACE))
    if not normalized:
        raise DomainValidationError(code, f"{field_name} is required")
    return normalized


def _normalized_text(
    value: object,
    *,
    required: bool = False,
    field_name: str = "value",
) -> str | None:
    if value is None:
        if required:
            raise DomainValidationError("route.identity_required", f"{field_name} is required")
        return None
    if not isinstance(value, str):
        raise DomainValidationError("route.value_invalid", f"{field_name} must be a string")
    normalized = unicodedata.normalize("NFC", value.strip(_ASCII_WHITESPACE)).casefold()
    if not normalized:
        if required:
            raise DomainValidationError("route.identity_required", f"{field_name} is required")
        return None
    if any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in normalized):
        raise DomainValidationError(
            "route.value_invalid",
            f"{field_name} must not contain control, format, or surrogate characters",
        )
    return normalized


def _normalize_percent_path(path: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(path):
        character = path[index]
        if character != "%":
            output.append(character)
            index += 1
            continue
        if index + 2 >= len(path) or path[index + 1] not in _HEX or path[index + 2] not in _HEX:
            raise DomainValidationError(
                "route.endpoint_invalid",
                "endpoint path contains malformed percent-encoding",
            )
        byte = int(path[index + 1 : index + 3], 16)
        decoded = chr(byte)
        output.append(decoded if decoded in _UNRESERVED else f"%{byte:02X}")
        index += 3
    return "".join(output)


def _remove_dot_segments(path: str) -> str:
    """Apply RFC 3986 §5.2.4 while preserving non-dot empty segments."""

    input_buffer = path
    output_buffer = ""
    while input_buffer:
        if input_buffer.startswith("../"):
            input_buffer = input_buffer[3:]
        elif input_buffer.startswith("./"):
            input_buffer = input_buffer[2:]
        elif input_buffer.startswith("/./"):
            input_buffer = "/" + input_buffer[3:]
        elif input_buffer == "/.":
            input_buffer = "/"
        elif input_buffer.startswith("/../"):
            input_buffer = "/" + input_buffer[4:]
            output_buffer = output_buffer.rsplit("/", 1)[0]
        elif input_buffer == "/..":
            input_buffer = "/"
            output_buffer = output_buffer.rsplit("/", 1)[0]
        elif input_buffer in {".", ".."}:
            input_buffer = ""
        else:
            segment_end = (
                input_buffer.find("/", 1)
                if input_buffer.startswith("/")
                else input_buffer.find("/")
            )
            if segment_end == -1:
                segment_end = len(input_buffer)
            output_buffer += input_buffer[:segment_end]
            input_buffer = input_buffer[segment_end:]
    return output_buffer


def _normalize_host(host: str) -> str:
    normalized = unicodedata.normalize("NFC", host).casefold()
    if ":" in normalized:
        try:
            return ipaddress.IPv6Address(normalized).compressed
        except ValueError as exc:
            raise DomainValidationError(
                "route.endpoint_invalid",
                "endpoint contains an invalid IPv6 host",
            ) from exc
    normalized = normalized.rstrip(".")
    if not normalized:
        raise DomainValidationError(
            "route.endpoint_invalid",
            "endpoint host is required",
        )
    try:
        return normalized.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise DomainValidationError(
            "route.endpoint_invalid",
            "endpoint contains an invalid internationalized host",
        ) from exc


def _normalize_endpoint(value: object) -> str:
    raw = _ascii_trimmed_nfc(
        value,
        field_name="endpoint",
        code="route.endpoint_invalid",
    )
    authority = raw.partition("://")[2].partition("/")[0]
    if (
        "\\" in raw
        or "%" in authority
        or any(
            character.isspace()
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            or character in {'"', "<", ">", "{", "}", "|", "^", "`"}
            for character in raw
        )
    ):
        raise DomainValidationError(
            "route.endpoint_invalid",
            "endpoint contains forbidden URL characters or malformed authority encoding",
        )
    if "?" in raw or "#" in raw:
        raise DomainValidationError(
            "route.endpoint_invalid",
            "endpoint must not contain a query or fragment",
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
        host = parsed.hostname
    except ValueError as exc:
        raise DomainValidationError("route.endpoint_invalid", "endpoint URL is malformed") from exc

    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        raise DomainValidationError(
            "route.endpoint_invalid",
            "endpoint must be an absolute http(s) URL without credentials, query or fragment",
        )

    normalized_host = _normalize_host(host)
    rendered_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    default_port = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    netloc = rendered_host if port is None or default_port else f"{rendered_host}:{port}"

    percent_normalized = _normalize_percent_path(unicodedata.normalize("NFC", parsed.path or "/"))
    normalized_path = _remove_dot_segments(percent_normalized)
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    if normalized_path == "/.":
        normalized_path = "/"
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")

    return urlunsplit((scheme, netloc, normalized_path, "", ""))


@dataclass(frozen=True)
class RouteV1:
    provider: str
    product: str
    surface: str
    account_id: str
    billing_pool_id: str
    quota_pool_id: str
    model: str
    endpoint: str
    region: str
    entitlement_id: str | None = None
    variant: str | None = None
    canonicalization_version: str = ROUTE_CANONICALIZATION_VERSION

    def __post_init__(self) -> None:
        for name in _REQUIRED_ROUTE_FIELDS:
            value = getattr(self, name)
            normalized = (
                _normalize_endpoint(value)
                if name == "endpoint"
                else _normalized_text(value, required=True, field_name=name)
            )
            object.__setattr__(self, name, normalized)
        for name in ("entitlement_id", "variant"):
            object.__setattr__(
                self,
                name,
                _normalized_text(getattr(self, name), field_name=name),
            )
        if self.canonicalization_version != ROUTE_CANONICALIZATION_VERSION:
            raise DomainValidationError(
                "route.canonicalization_unknown",
                "canonicalization_version must be route-v1",
            )
        _reject_sensitive(self, "route")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "RouteV1":
        payload_keys = _validated_mapping_keys(
            payload,
            code="route.unexpected_field",
            location="route",
        )
        _reject_sensitive(payload, "route")
        unknown = payload_keys - set(_ROUTE_FIELDS)
        if unknown:
            raise DomainValidationError(
                "route.unexpected_field",
                f"unexpected route fields: {sorted(unknown)}",
            )
        values = {
            key: payload.get(key)
            for key in _ROUTE_FIELDS
            if key != "canonicalization_version"
        }
        if "canonicalization_version" in payload:
            values["canonicalization_version"] = payload["canonicalization_version"]
        return cls(**values)  # type: ignore[arg-type]

    def canonical_object(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in _ROUTE_FIELDS}

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            self.canonical_object(),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )

    @property
    def route_id(self) -> str:
        digest = hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()
        return f"{ROUTE_CANONICALIZATION_VERSION}:{digest}"


def _revalidated_route(value: object, location: str) -> RouteV1:
    if type(value) is not RouteV1:
        raise DomainValidationError(
            "route.context_invalid",
            f"{location} requires a validated RouteV1",
        )
    return RouteV1.from_mapping(value.canonical_object())


def _revalidated_route_registry(
    value: Mapping[str, RouteV1] | None,
    *,
    code: str,
    location: str,
) -> dict[str, RouteV1]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DomainValidationError(code, f"{location} must be a mapping")
    validated: dict[str, RouteV1] = {}
    for key, route in value.items():
        if type(key) is not str or type(route) is not RouteV1:
            raise DomainValidationError(code, f"{location} requires string keys and RouteV1 values")
        validated_route = _revalidated_route(route, location)
        if key != validated_route.route_id:
            raise DomainValidationError(code, f"{location} keys must match route identities")
        validated[key] = validated_route
    return validated


@dataclass(frozen=True)
class RouteEligibilityFactsV1:
    route: RouteV1
    identity_policy: EligibilityDisposition
    privacy_permission: EligibilityDisposition
    capability_tool: EligibilityDisposition
    context: EligibilityDisposition
    freshness_confidence: EligibilityDisposition
    budget: EligibilityDisposition
    breaker_cooldown: EligibilityDisposition
    concurrency_reservation: EligibilityDisposition

    def __post_init__(self) -> None:
        if type(self.route) is not RouteV1:
            raise DomainValidationError(
                "eligibility.invalid",
                "eligibility facts require a validated RouteV1",
            )
        object.__setattr__(self, "route", _revalidated_route(self.route, "eligibility facts"))
        for gate in _ELIGIBILITY_GATES:
            if type(getattr(self, gate)) is not EligibilityDisposition:
                raise DomainValidationError(
                    "eligibility.invalid",
                    f"{gate} requires a typed EligibilityDisposition",
                )


def _task_text(value: object, field_name: str) -> str:
    return _ascii_trimmed_nfc(
        value,
        field_name=field_name,
        code="task.scalar_invalid",
    )


def _validated_route_id(value: object, field_name: str) -> str:
    route_id = _task_text(value, field_name)
    if _ROUTE_ID_PATTERN.fullmatch(route_id) is None:
        raise DomainValidationError(
            "route.identity_invalid",
            f"{field_name} must be route-v1:<lowercase SHA-256>",
        )
    return route_id


def _immutable_string_collection(
    value: object,
    field_name: str,
    *,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise DomainValidationError(
            "task.collection_invalid",
            f"{field_name} must be a collection of strings",
        )
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise DomainValidationError(
                "task.collection_invalid",
                f"{field_name} must contain only strings",
            )
        text = unicodedata.normalize("NFC", item.strip(_ASCII_WHITESPACE))
        if not text:
            raise DomainValidationError(
                "task.collection_invalid",
                f"{field_name} entries must be non-empty",
            )
        normalized.append(text)
    if require_nonempty and not normalized:
        raise DomainValidationError(
            "task.collection_invalid",
            f"{field_name} must not be empty",
        )
    return tuple(normalized)


def _stable_identifier_collection(
    value: object,
    field_name: str,
    *,
    code: str,
    require_nonempty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise DomainValidationError(code, f"{field_name} must be a collection of identifiers")
    if len(value) > _MAX_STABLE_IDENTIFIERS:
        raise DomainValidationError(
            code,
            f"{field_name} must contain at most {_MAX_STABLE_IDENTIFIERS} identifiers",
        )
    identifiers = tuple(value)
    if require_nonempty and not identifiers:
        raise DomainValidationError(code, f"{field_name} must not be empty")
    try:
        _reject_sensitive({field_name: identifiers}, "stable identifiers")
    except DomainValidationError as exc:
        raise DomainValidationError(
            code,
            f"{field_name} must not contain sensitive values",
        ) from exc
    if any(
        type(identifier) is not str
        or _STABLE_IDENTIFIER_PATTERN.fullmatch(identifier) is None
        for identifier in identifiers
    ):
        raise DomainValidationError(
            code,
            f"{field_name} entries must be lowercase ASCII identifiers",
        )
    if len(set(identifiers)) != len(identifiers):
        raise DomainValidationError(code, f"{field_name} must not contain duplicates")
    return identifiers  # type: ignore[return-value]


def _finite_float(value: object, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainValidationError(code, "score must be finite numeric")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise DomainValidationError(code, "score must be finite numeric") from exc
    if not math.isfinite(normalized):
        raise DomainValidationError(code, "score must be finite numeric")
    return normalized


def _normalized_policy_identifiers(
    value: object,
    field_name: str,
    known: frozenset[str],
) -> tuple[str, ...]:
    normalized = tuple(
        item.casefold()
        for item in _immutable_string_collection(value, field_name)
    )
    unknown = sorted(set(normalized) - known)
    if unknown:
        raise DomainValidationError(
            "task.unknown_policy_identifier",
            f"unknown {field_name}: {unknown}",
        )
    return normalized


@dataclass(frozen=True)
class AuditedModelJustification:
    policy_version: str
    reason: str
    evidence_refs: tuple[str, ...]
    author: str
    expires_at: str
    identity_claims: tuple[tuple[str, str], ...] = ()
    validation_time: InitVar[datetime | None] = None

    def __post_init__(self, validation_time: datetime | None) -> None:
        for name in ("policy_version", "reason", "author"):
            object.__setattr__(self, name, _task_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "expires_at",
            _validated_future_timestamp(
                self.expires_at,
                "expires_at",
                reference_time=validation_time,
            ),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _immutable_string_collection(
                self.evidence_refs,
                "evidence_refs",
                require_nonempty=True,
            ),
        )
        claims: list[tuple[str, str]] = []
        for claim in self.identity_claims:
            if (
                not isinstance(claim, Sequence)
                or isinstance(claim, (str, bytes, bytearray))
                or len(claim) != 2
                or not all(isinstance(part, str) for part in claim)
            ):
                raise DomainValidationError(
                    "task.justification_invalid",
                    "identity_claims must contain audited field/value pairs",
                )
            claims.append(
                (
                    _task_text(claim[0], "identity claim field"),
                    _task_text(claim[1], "identity claim value"),
                )
            )
        object.__setattr__(self, "identity_claims", tuple(sorted(claims)))
        _reject_sensitive(self, "task.justification")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        reference_time: datetime | None = None,
    ) -> "AuditedModelJustification":
        expected = {field_.name for field_ in fields(cls)}
        unknown = _validated_mapping_keys(
            payload,
            code="task.unexpected_field",
            location="audited justification",
        ) - expected
        if unknown:
            raise DomainValidationError(
                "task.unexpected_field",
                f"unexpected audited justification fields: {sorted(unknown)}",
            )
        return cls(
            policy_version=payload.get("policy_version"),  # type: ignore[arg-type]
            reason=payload.get("reason"),  # type: ignore[arg-type]
            evidence_refs=payload.get("evidence_refs", ()),  # type: ignore[arg-type]
            author=payload.get("author"),  # type: ignore[arg-type]
            expires_at=payload.get("expires_at"),  # type: ignore[arg-type]
            identity_claims=payload.get("identity_claims", ()),  # type: ignore[arg-type]
            validation_time=reference_time,
        )


@dataclass(frozen=True)
class TaskContextV1:
    classification: str
    max_tokens: int | None
    allowed_sources: tuple[str, ...]
    token_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "classification", _task_text(self.classification, "context.classification"))
        object.__setattr__(
            self,
            "allowed_sources",
            _immutable_string_collection(self.allowed_sources, "context.allowed_sources"),
        )
        for name in ("max_tokens", "token_count"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise DomainValidationError(
                    "task.context_bounds_invalid",
                    f"context.{name} must be a non-negative integer or unknown",
                )
        if (
            self.max_tokens is not None
            and self.max_tokens == 0
        ):
            raise DomainValidationError(
                "task.context_bounds_invalid",
                "context.max_tokens must be positive when supplied",
            )
        if (
            self.max_tokens is not None
            and self.token_count is not None
            and self.token_count > self.max_tokens
        ):
            raise DomainValidationError(
                "task.context_bounds_invalid",
                "context.token_count must not exceed context.max_tokens",
            )
        _reject_sensitive(self, "task.context")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TaskContextV1":
        return _dto_from_mapping(cls, payload, "task.context")


@dataclass(frozen=True)
class TaskPrivacyV1:
    classification: str
    outbound_allowed: bool
    retention: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "classification", _task_text(self.classification, "privacy.classification"))
        object.__setattr__(self, "retention", _task_text(self.retention, "privacy.retention"))
        if not isinstance(self.outbound_allowed, bool):
            raise DomainValidationError("task.privacy_invalid", "privacy.outbound_allowed must be boolean")
        _reject_sensitive(self, "task.privacy")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TaskPrivacyV1":
        return _dto_from_mapping(cls, payload, "task.privacy")


@dataclass(frozen=True)
class TaskRiskV1:
    level: str
    reversibility: str
    impact: str

    def __post_init__(self) -> None:
        for name in ("level", "reversibility", "impact"):
            object.__setattr__(self, name, _task_text(getattr(self, name), f"risk.{name}"))
        _reject_sensitive(self, "task.risk")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TaskRiskV1":
        return _dto_from_mapping(cls, payload, "task.risk")


@dataclass(frozen=True)
class TaskBudgetV1:
    currency: str
    paid_allowed: bool
    soft_cap: float | None = None
    hard_cap: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", _task_text(self.currency, "budget.currency"))
        if not isinstance(self.paid_allowed, bool):
            raise DomainValidationError("task.budget_invalid", "budget.paid_allowed must be boolean")
        for name in ("soft_cap", "hard_cap"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value < 0
            ):
                raise DomainValidationError("task.budget_invalid", f"budget.{name} must be finite and non-negative")
        if self.soft_cap is not None and self.hard_cap is not None and self.soft_cap > self.hard_cap:
            raise DomainValidationError("task.budget_invalid", "budget.soft_cap must not exceed hard_cap")
        _reject_sensitive(self, "task.budget")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TaskBudgetV1":
        return _dto_from_mapping(cls, payload, "task.budget")


@dataclass(frozen=True)
class TaskVerificationV1:
    minimum: str
    independent_required: bool
    human_gate_required: bool

    def __post_init__(self) -> None:
        if self.minimum not in _VERIFICATION_RANK:
            raise DomainValidationError("task.verification_invalid", "verification.minimum must be V0 through V4")
        if not isinstance(self.independent_required, bool) or not isinstance(self.human_gate_required, bool):
            raise DomainValidationError("task.verification_invalid", "verification flags must be boolean")
        _reject_sensitive(self, "task.verification")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "TaskVerificationV1":
        return _dto_from_mapping(cls, payload, "task.verification")


def _dto_from_mapping(dto: type[Any], payload: Mapping[str, object], location: str) -> Any:
    if not isinstance(payload, Mapping):
        raise DomainValidationError("task.schema_invalid", f"{location} must be a mapping")
    expected = {field_.name for field_ in fields(dto)}
    unknown = _validated_mapping_keys(
        payload,
        code="task.unexpected_field",
        location=location,
    ) - expected
    if unknown:
        raise DomainValidationError("task.unexpected_field", f"unexpected {location} fields: {sorted(unknown)}")
    try:
        return dto(**payload)
    except TypeError as exc:
        raise DomainValidationError("task.schema_invalid", f"malformed {location}") from exc


@dataclass(frozen=True)
class TaskEnvelope:
    schema_version: str
    task_id: str
    objective: str
    deliverables: tuple[str, ...]
    capabilities_required: tuple[str, ...]
    tools_allowed: tuple[str, ...]
    permissions_required: tuple[str, ...]
    context: TaskContextV1
    privacy: TaskPrivacyV1
    risk: TaskRiskV1
    effort: str
    budget: TaskBudgetV1
    verification: TaskVerificationV1
    policy_version: str
    root_task_id: str | None = None
    audited_model_justification: AuditedModelJustification | None = None
    validation_time: InitVar[datetime | None] = None

    def __post_init__(self, validation_time: datetime | None) -> None:
        if self.schema_version != "task-envelope/v1":
            raise DomainValidationError("task.schema_invalid", "schema_version must be task-envelope/v1")
        for name in ("task_id", "objective", "policy_version"):
            object.__setattr__(self, name, _task_text(getattr(self, name), name))
        if self.root_task_id is not None:
            object.__setattr__(self, "root_task_id", _task_text(self.root_task_id, "root_task_id"))
        object.__setattr__(
            self,
            "deliverables",
            _immutable_string_collection(self.deliverables, "deliverables", require_nonempty=True),
        )
        for name, known in (
            ("capabilities_required", _KNOWN_CAPABILITY_IDENTIFIERS),
            ("tools_allowed", _KNOWN_TOOL_IDENTIFIERS),
            ("permissions_required", _KNOWN_PERMISSION_IDENTIFIERS),
        ):
            object.__setattr__(
                self,
                name,
                _normalized_policy_identifiers(getattr(self, name), name, known),
            )
        expected_dtos = (
            ("context", TaskContextV1),
            ("privacy", TaskPrivacyV1),
            ("risk", TaskRiskV1),
            ("budget", TaskBudgetV1),
            ("verification", TaskVerificationV1),
        )
        for name, dto_type in expected_dtos:
            value = getattr(self, name)
            if type(value) is not dto_type:
                raise DomainValidationError("task.schema_invalid", f"{name} must be an immutable validated DTO")
            object.__setattr__(self, name, dto_type.from_mapping(asdict(value)))
        if self.effort not in {f"E{level}" for level in range(5)}:
            raise DomainValidationError("task.effort_invalid", "effort must be E0 through E4")
        justification = self.audited_model_justification
        if justification is not None:
            if type(justification) is not AuditedModelJustification:
                raise DomainValidationError(
                    "task.justification_invalid", "invalid audited_model_justification"
                )
            validated_justification = AuditedModelJustification.from_mapping(
                asdict(justification),
                reference_time=validation_time,
            )
            if validated_justification.policy_version != self.policy_version:
                raise DomainValidationError(
                    "task.justification_policy_mismatch",
                    "audited model justification must match task policy_version",
                )
            object.__setattr__(
                self,
                "audited_model_justification",
                validated_justification,
            )
        _reject_sensitive(self, "task")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        reference_time: datetime | None = None,
    ) -> "TaskEnvelope":
        if not isinstance(payload, Mapping):
            raise DomainValidationError("task.schema_invalid", "task envelope must be a mapping")
        _validated_mapping_keys(
            payload,
            code="task.unexpected_field",
            location="task",
        )
        _reject_sensitive(payload, "task")
        identity_fields = {
            key
            for key in payload
            if isinstance(key, str)
            and key.casefold() in {"model", "provider", "route_id", "selected_route_id"}
        }
        justification_payload = payload.get("audited_model_justification")
        if identity_fields and not isinstance(justification_payload, Mapping):
            raise DomainValidationError(
                "task.unaudited_model_identity",
                "model/provider/route identity requires audited_model_justification",
            )
        safe_payload = {key: value for key, value in payload.items() if key not in identity_fields}
        allowed = {field_.name for field_ in fields(cls)}
        unknown = set(safe_payload) - allowed
        if unknown:
            raise DomainValidationError("task.unexpected_field", f"unexpected task fields: {sorted(unknown)}")
        values = dict(safe_payload)
        converters = {
            "context": TaskContextV1,
            "privacy": TaskPrivacyV1,
            "risk": TaskRiskV1,
            "budget": TaskBudgetV1,
            "verification": TaskVerificationV1,
        }
        for name, dto_type in converters.items():
            value = values.get(name)
            if isinstance(value, Mapping):
                values[name] = dto_type.from_mapping(value)
        justification = values.get("audited_model_justification")
        if isinstance(justification, Mapping):
            audited_payload = dict(justification)
            if identity_fields:
                supplied_claims_raw = audited_payload.get("identity_claims", ())
                supplied_claims: dict[str, str] = {}
                if supplied_claims_raw:
                    try:
                        supplied_claims = {
                            str(name): str(value) for name, value in supplied_claims_raw
                        }
                    except (TypeError, ValueError) as exc:
                        raise DomainValidationError(
                            "task.justification_invalid",
                            "identity_claims must contain field/value pairs",
                        ) from exc
                if any(
                    name in supplied_claims and supplied_claims[name] != str(payload[name])
                    for name in identity_fields
                ):
                    raise DomainValidationError(
                        "task.unaudited_model_identity",
                        "audited identity claims must match the requested identity",
                    )
                supplied_claims.update(
                    {name: str(payload[name]) for name in identity_fields}
                )
                audited_payload["identity_claims"] = tuple(sorted(supplied_claims.items()))
            values["audited_model_justification"] = AuditedModelJustification.from_mapping(
                audited_payload,
                reference_time=reference_time,
            )
        try:
            return cls(**values, validation_time=reference_time)
        except TypeError as exc:
            raise DomainValidationError("task.schema_invalid", "malformed task envelope") from exc


@dataclass(frozen=True)
class InitialSelectionTriggerV1:
    schema_version: str
    kind: str
    source: str
    evaluated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != "initial-selection-trigger/v1" or self.kind != "initial_selection":
            raise DomainValidationError(
                "decision.trigger_invalid",
                "initial trigger must be initial-selection-trigger/v1",
            )
        for name in ("source", "evaluated_at"):
            object.__setattr__(self, name, _task_text(getattr(self, name), name))
        _reject_sensitive(self, "initial trigger")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "InitialSelectionTriggerV1":
        expected = {field_.name for field_ in fields(cls)}
        if _validated_mapping_keys(
            payload,
            code="decision.trigger_invalid",
            location="initial trigger",
        ) != expected:
            raise DomainValidationError(
                "decision.trigger_invalid",
                "initial trigger must contain the exact v1 fields",
            )
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise DomainValidationError("decision.trigger_invalid", "malformed initial trigger") from exc


@dataclass(frozen=True)
class RuntimeErrorClassificationV1:
    kind: ErrorKind
    source: str
    attempted_route_id: str
    quota_pool_id: str
    classified_at: str
    billing_pool_id: str | None = None
    evidence_code: str | None = None

    def __post_init__(self) -> None:
        if self.kind is not ErrorKind.CAPACITY_EXHAUSTED:
            raise DomainValidationError(
                "classification.capacity_scope_required",
                "capacity exhaustion classification kind is required",
            )
        for name in ("attempted_route_id", "quota_pool_id"):
            raw = getattr(self, name)
            if not isinstance(raw, str) or not raw.strip(_ASCII_WHITESPACE):
                raise DomainValidationError(
                    "classification.capacity_scope_required",
                    "capacity exhaustion requires attempted_route_id and quota_pool_id",
                )
            normalized = unicodedata.normalize("NFC", raw.strip(_ASCII_WHITESPACE))
            if name == "quota_pool_id":
                normalized = normalized.casefold()
            object.__setattr__(self, name, normalized)
        object.__setattr__(
            self,
            "attempted_route_id",
            _validated_route_id(self.attempted_route_id, "attempted_route_id"),
        )
        if self.billing_pool_id is not None:
            normalized = _normalized_text(
                self.billing_pool_id,
                required=True,
                field_name="billing_pool_id",
            )
            object.__setattr__(self, "billing_pool_id", normalized)
        if self.evidence_code is not None:
            object.__setattr__(
                self,
                "evidence_code",
                _task_text(self.evidence_code, "evidence_code"),
            )
        object.__setattr__(self, "source", _task_text(self.source, "classification source"))
        object.__setattr__(
            self,
            "classified_at",
            _task_text(self.classified_at, "classification timestamp"),
        )
        _reject_sensitive(self, "classification")

    @classmethod
    def from_mapping(
        cls, payload: Mapping[str, object]
    ) -> "RuntimeErrorClassificationV1":
        if not isinstance(payload, Mapping):
            raise DomainValidationError(
                "classification.capacity_scope_required",
                "capacity classification must be a mapping with route and pool scope",
            )
        if (
            not isinstance(payload.get("source"), str)
            or not str(payload.get("source", "")).strip(_ASCII_WHITESPACE)
            or not isinstance(payload.get("classified_at"), str)
            or not str(payload.get("classified_at", "")).strip(_ASCII_WHITESPACE)
            or not isinstance(payload.get("attempted_route_id"), str)
            or not str(payload.get("attempted_route_id", "")).strip(_ASCII_WHITESPACE)
            or not isinstance(payload.get("quota_pool_id"), str)
            or not str(payload.get("quota_pool_id", "")).strip(_ASCII_WHITESPACE)
            or "provider" in payload
            or "model" in payload
        ):
            raise DomainValidationError(
                "classification.capacity_scope_required",
                "provider/model labels cannot replace attempted_route_id and quota_pool_id",
            )
        _reject_sensitive(payload, "classification")
        allowed = {field_.name for field_ in fields(cls)}
        unknown = _validated_mapping_keys(
            payload,
            code="classification.unexpected_field",
            location="classification",
        ) - allowed
        if unknown:
            raise DomainValidationError(
                "classification.unexpected_field",
                f"unexpected classification fields: {sorted(unknown)}",
            )
        try:
            raw_kind = payload.get("kind", "")
            kind = raw_kind if type(raw_kind) is ErrorKind else ErrorKind(str(raw_kind))
        except ValueError as exc:
            raise DomainValidationError(
                "classification.capacity_scope_required",
                "typed capacity exhaustion classification is required",
            ) from exc
        return cls(
            kind=kind,
            source=str(payload["source"]),
            attempted_route_id=str(payload["attempted_route_id"]),
            quota_pool_id=str(payload["quota_pool_id"]),
            classified_at=str(payload["classified_at"]),
            billing_pool_id=(
                None
                if payload.get("billing_pool_id") is None
                else str(payload["billing_pool_id"])
            ),
            evidence_code=(
                None
                if payload.get("evidence_code") is None
                else str(payload["evidence_code"])
            ),
        )


@dataclass(frozen=True, init=False)
class CandidateEvaluation:
    route_id: str
    deterministic_status: str
    rejection_codes: tuple[str, ...]
    score: float | None
    score_factors: tuple[str, ...]

    def __init__(
        self,
        route_or_id: RouteV1 | str,
        deterministic_status: bool | str,
        rejection_codes: Sequence[str] = (),
        score: float | None = None,
        score_factors: Sequence[str] = (),
    ) -> None:
        route_context = route_or_id if type(route_or_id) is RouteV1 else None
        route_id = (
            route_context.route_id
            if route_context is not None
            else _validated_route_id(route_or_id, "route_id")
        )
        status = (
            "ELIGIBLE" if deterministic_status is True else "REJECTED"
            if deterministic_status is False
            else _task_text(deterministic_status, "deterministic_status").upper()
        )
        if status not in {"ELIGIBLE", "REJECTED"}:
            raise DomainValidationError(
                "candidate.invalid",
                "deterministic_status must be ELIGIBLE or REJECTED",
            )
        normalized_rejections = _stable_identifier_collection(
            rejection_codes,
            "rejection_codes",
            code="candidate.invalid",
        )
        normalized_factors = _stable_identifier_collection(
            score_factors,
            "score_factors",
            code="candidate.invalid",
        )
        if status == "REJECTED" and not normalized_rejections:
            raise DomainValidationError(
                "candidate.invalid",
                "rejected candidate requires at least one rejection code",
            )
        if status == "ELIGIBLE" and normalized_rejections:
            raise DomainValidationError(
                "candidate.invalid",
                "eligible candidate must not contain rejection codes",
            )
        if status == "REJECTED" and score is not None:
            raise DomainValidationError("candidate.invalid", "rejected candidate must not be scored")
        normalized_score = (
            None if score is None else _finite_float(score, code="candidate.invalid")
        )
        object.__setattr__(self, "route_id", route_id)
        object.__setattr__(self, "deterministic_status", status)
        object.__setattr__(self, "rejection_codes", normalized_rejections)
        object.__setattr__(self, "score", normalized_score)
        object.__setattr__(self, "score_factors", normalized_factors)
        object.__setattr__(self, "_route_context", route_context)
        _reject_sensitive(
            {
                "rejection_codes": normalized_rejections,
                "score_factors": normalized_factors,
            },
            "candidate",
        )

    @property
    def eligible(self) -> bool:
        return self.deterministic_status == "ELIGIBLE"

    @property
    def route(self) -> RouteV1:
        route_context = getattr(self, "_route_context", None)
        if type(route_context) is not RouteV1:
            raise DomainValidationError(
                "candidate.route_context_required",
                "route context is required for policy recomputation",
            )
        return route_context

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        route_context: RouteV1 | None = None,
    ) -> "CandidateEvaluation":
        expected = {
            "route_id",
            "deterministic_status",
            "rejection_codes",
            "score",
            "score_factors",
        }
        payload_keys = _validated_mapping_keys(
            payload,
            code="candidate.invalid",
            location="candidate",
        )
        unknown = payload_keys - expected
        required = {"route_id", "deterministic_status", "rejection_codes", "score_factors"}
        if unknown or not required.issubset(payload_keys):
            raise DomainValidationError(
                "candidate.invalid",
                "candidate mapping must match CandidateEvaluation v1",
            )
        persisted_route_id = _validated_route_id(payload["route_id"], "route_id")
        if route_context is not None and route_context.route_id != persisted_route_id:
            raise DomainValidationError(
                "candidate.route_binding_invalid",
                "trusted route context does not match persisted candidate route_id",
            )
        return cls(
            route_context or persisted_route_id,
            str(payload["deterministic_status"]),
            payload["rejection_codes"],  # type: ignore[arg-type]
            payload.get("score"),  # type: ignore[arg-type]
            payload["score_factors"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class CandidateScoreV1:
    score: float
    score_factors: tuple[str, ...]

    def __post_init__(self) -> None:
        score = _finite_float(self.score, code="score.invalid")
        factors = _stable_identifier_collection(
            self.score_factors,
            "score_factors",
            code="score.invalid",
            require_nonempty=True,
        )
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "score_factors", factors)
        _reject_sensitive({"score_factors": factors}, "score")


def evaluate_route_eligibility(
    facts: Iterable[RouteEligibilityFactsV1],
) -> tuple[CandidateEvaluation, ...]:
    try:
        facts_items = tuple(facts)
    except TypeError as exc:
        raise DomainValidationError(
            "eligibility.invalid",
            "eligibility facts must be iterable",
        ) from exc

    validated: list[RouteEligibilityFactsV1] = []
    for item in facts_items:
        if type(item) is not RouteEligibilityFactsV1:
            raise DomainValidationError(
                "eligibility.invalid",
                "every eligibility item must be RouteEligibilityFactsV1",
            )
        validated.append(
            RouteEligibilityFactsV1(
                route=item.route,
                **{gate: getattr(item, gate) for gate in _ELIGIBILITY_GATES},
            )
        )

    route_ids = tuple(item.route.route_id for item in validated)
    if len(set(route_ids)) != len(route_ids):
        raise DomainValidationError(
            "eligibility.duplicate_route",
            "eligibility facts must contain unique route IDs",
        )

    evaluations: list[CandidateEvaluation] = []
    for item in sorted(validated, key=lambda candidate: candidate.route.route_id):
        rejection_codes = tuple(
            f"{gate}_{'rejected' if disposition is EligibilityDisposition.REJECT else 'unknown'}"
            for gate in _ELIGIBILITY_GATES
            if (disposition := getattr(item, gate)) is not EligibilityDisposition.PASS
        )
        evaluations.append(
            CandidateEvaluation(
                item.route,
                not rejection_codes,
                rejection_codes,
            )
        )
    return tuple(evaluations)


def score_eligible_candidates(
    candidates: Sequence[CandidateEvaluation],
    scores_by_route_id: Mapping[str, CandidateScoreV1],
) -> tuple[CandidateEvaluation, ...]:
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise DomainValidationError("score.invalid", "candidates must be a sequence")
    if not isinstance(scores_by_route_id, Mapping):
        raise DomainValidationError("score.invalid", "scores must be keyed by route ID")

    validated_candidates: list[CandidateEvaluation] = []
    for candidate in candidates:
        if type(candidate) is not CandidateEvaluation:
            raise DomainValidationError(
                "score.invalid",
                "every candidate must be CandidateEvaluation",
            )
        try:
            candidate_route = _revalidated_route(candidate.route, "candidate scoring")
            validated_candidate = CandidateEvaluation(
                candidate_route,
                candidate.deterministic_status,
                candidate.rejection_codes,
                candidate.score,
                candidate.score_factors,
            )
        except DomainValidationError as exc:
            raise DomainValidationError(
                "score.invalid",
                "candidate must retain a trusted RouteV1 binding",
            ) from exc
        if validated_candidate.score is not None or validated_candidate.score_factors:
            raise DomainValidationError(
                "score.invalid",
                "candidate must be unscored before score assignment",
            )
        validated_candidates.append(validated_candidate)

    candidate_ids = tuple(candidate.route_id for candidate in validated_candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise DomainValidationError(
            "score.duplicate_candidate",
            "candidates must contain unique route IDs",
        )

    validated_scores: dict[str, CandidateScoreV1] = {}
    for route_id, candidate_score in scores_by_route_id.items():
        if type(route_id) is not str or type(candidate_score) is not CandidateScoreV1:
            raise DomainValidationError(
                "score.invalid",
                "scores require string route IDs and validated CandidateScoreV1 values",
            )
        try:
            _validated_route_id(route_id, "score route_id")
            validated_scores[route_id] = CandidateScoreV1(
                candidate_score.score,
                candidate_score.score_factors,
            )
        except DomainValidationError as exc:
            raise DomainValidationError("score.invalid", "malformed candidate score") from exc

    eligible_ids = {
        candidate.route_id for candidate in validated_candidates if candidate.eligible
    }
    if set(validated_scores) != eligible_ids:
        raise DomainValidationError(
            "score.invalid",
            "scores must contain exactly one entry for every eligible route and no others",
        )

    scored: list[CandidateEvaluation] = []
    for candidate in sorted(validated_candidates, key=lambda item: item.route_id):
        if not candidate.eligible:
            scored.append(candidate)
            continue
        candidate_score = validated_scores[candidate.route_id]
        scored.append(
            CandidateEvaluation(
                candidate.route,
                True,
                score=candidate_score.score,
                score_factors=candidate_score.score_factors,
            )
        )
    return tuple(scored)


@dataclass(frozen=True)
class AcceptanceThresholdV1:
    metric: str
    operator: str
    value: str | int | float
    unit: str | None = None
    evidence_required: bool = True
    evidence_ref: str | None = None
    met: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", _task_text(self.metric, "metric"))
        if self.operator not in {">", ">=", "<", "<=", "==", "!="}:
            raise DomainValidationError(
                "quality.threshold_invalid",
                "threshold operator is not supported",
            )
        if isinstance(self.value, bool) or not isinstance(self.value, (str, int, float)):
            raise DomainValidationError(
                "quality.threshold_invalid",
                "threshold value must be a finite scalar",
            )
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise DomainValidationError(
                "quality.threshold_invalid",
                "threshold value must be finite",
            )
        if isinstance(self.value, str) and not self.value.strip(_ASCII_WHITESPACE):
            raise DomainValidationError(
                "quality.threshold_invalid",
                "threshold value must not be empty",
            )
        if self.unit is not None:
            object.__setattr__(self, "unit", _task_text(self.unit, "unit"))
        if not isinstance(self.evidence_required, bool):
            raise DomainValidationError(
                "quality.threshold_invalid",
                "evidence_required must be boolean",
            )
        if self.evidence_ref is not None:
            object.__setattr__(
                self,
                "evidence_ref",
                _task_text(self.evidence_ref, "evidence_ref"),
            )
        if self.met is not None and not isinstance(self.met, bool):
            raise DomainValidationError("quality.threshold_invalid", "met must be boolean or unknown")
        _reject_sensitive(self, "quality.threshold")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "AcceptanceThresholdV1":
        expected = {field_.name for field_ in fields(cls)}
        if _validated_mapping_keys(
            payload,
            code="quality.threshold_invalid",
            location="acceptance threshold",
        ) - expected:
            raise DomainValidationError("quality.threshold_invalid", "unexpected threshold fields")
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise DomainValidationError("quality.threshold_invalid", "malformed threshold") from exc


@dataclass(frozen=True)
class CompensationEscalationV1:
    on_unmet: EscalationAction
    owner: str
    deadline: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.on_unmet, str):
            try:
                object.__setattr__(self, "on_unmet", EscalationAction(self.on_unmet))
            except ValueError as exc:
                raise DomainValidationError(
                    "quality.escalation_invalid",
                    "escalation action must block dispatch or require a human gate",
                ) from exc
        if type(self.on_unmet) is not EscalationAction:
            raise DomainValidationError("quality.escalation_invalid", "invalid escalation action")
        object.__setattr__(self, "owner", _task_text(self.owner, "owner"))
        if self.deadline is not None:
            object.__setattr__(self, "deadline", _task_text(self.deadline, "deadline"))
        _reject_sensitive(self, "quality.escalation")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "CompensationEscalationV1":
        expected = {field_.name for field_ in fields(cls)}
        if _validated_mapping_keys(
            payload,
            code="quality.escalation_invalid",
            location="compensation escalation",
        ) - expected:
            raise DomainValidationError("quality.escalation_invalid", "unexpected escalation fields")
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise DomainValidationError("quality.escalation_invalid", "malformed escalation") from exc


@dataclass(frozen=True)
class IndependentReviewAttestationV1:
    reviewer: str
    route_id: str
    quota_pool_id: str
    billing_pool_id: str
    reviewed_execution_id: str
    execution_id: str
    evidence_ref: str

    def __post_init__(self) -> None:
        for name in (
            "reviewer",
            "route_id",
            "quota_pool_id",
            "billing_pool_id",
            "reviewed_execution_id",
            "execution_id",
            "evidence_ref",
        ):
            object.__setattr__(self, name, _task_text(getattr(self, name), name))
        object.__setattr__(self, "route_id", _validated_route_id(self.route_id, "route_id"))
        _reject_sensitive(self, "quality.review")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
    ) -> "IndependentReviewAttestationV1":
        expected = {field_.name for field_ in fields(cls)}
        if _validated_mapping_keys(
            payload,
            code="quality.independence_invalid",
            location="review attestation",
        ) != expected:
            raise DomainValidationError(
                "quality.independence_invalid",
                "review attestation must contain the exact v1 identity fields",
            )
        try:
            return cls(**payload)  # type: ignore[arg-type]
        except TypeError as exc:
            raise DomainValidationError(
                "quality.independence_invalid",
                "malformed independent review attestation",
            ) from exc


@dataclass(frozen=True)
class QualityCompensationPlanV1:
    plan_id: str
    decision_id: str
    prior_route_id: str
    selected_route_id: str
    prior_quota_pool_id: str
    prior_billing_pool_id: str
    selected_quota_pool_id: str
    selected_billing_pool_id: str
    trigger_kind: str
    quality_delta_codes: tuple[str, ...]
    required_verification: str
    independence_required: bool
    required_reviewers: tuple[str, ...]
    review_attestations: tuple[IndependentReviewAttestationV1, ...]
    acceptance_thresholds: tuple[AcceptanceThresholdV1, ...]
    escalation: CompensationEscalationV1
    evidence_refs: tuple[str, ...]
    created_at: str
    policy_version: str
    schema_version: str = QUALITY_COMPENSATION_SCHEMA_VERSION
    human_approval_ref: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != QUALITY_COMPENSATION_SCHEMA_VERSION:
            raise DomainValidationError(
                "quality.schema_invalid",
                "schema_version must be quality-compensation/v1",
            )
        for name in (
            "plan_id",
            "decision_id",
            "prior_route_id",
            "selected_route_id",
            "prior_quota_pool_id",
            "prior_billing_pool_id",
            "selected_quota_pool_id",
            "selected_billing_pool_id",
            "trigger_kind",
            "created_at",
            "policy_version",
        ):
            object.__setattr__(self, name, _task_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "prior_route_id",
            _validated_route_id(self.prior_route_id, "prior_route_id"),
        )
        object.__setattr__(
            self,
            "selected_route_id",
            _validated_route_id(self.selected_route_id, "selected_route_id"),
        )
        for name in (
            "prior_quota_pool_id",
            "prior_billing_pool_id",
            "selected_quota_pool_id",
            "selected_billing_pool_id",
        ):
            object.__setattr__(self, name, getattr(self, name).casefold())
        for name, nonempty in (
            ("quality_delta_codes", True),
            ("required_reviewers", False),
            ("evidence_refs", True),
        ):
            object.__setattr__(
                self,
                name,
                _immutable_string_collection(
                    getattr(self, name),
                    name,
                    require_nonempty=nonempty,
                ),
            )
        if self.required_verification not in _VERIFICATION_RANK:
            raise DomainValidationError(
                "quality.verification_invalid",
                "required_verification must be V0 through V4",
            )
        if not isinstance(self.independence_required, bool):
            raise DomainValidationError(
                "quality.independence_invalid",
                "independence_required must be boolean",
            )
        if isinstance(self.acceptance_thresholds, (str, bytes)) or not isinstance(
            self.acceptance_thresholds,
            Sequence,
        ):
            raise DomainValidationError(
                "quality.threshold_invalid",
                "acceptance_thresholds must be a collection",
            )
        thresholds = tuple(self.acceptance_thresholds)
        if not thresholds or any(type(item) is not AcceptanceThresholdV1 for item in thresholds):
            raise DomainValidationError(
                "quality.threshold_invalid",
                "at least one validated acceptance threshold is required",
            )
        object.__setattr__(self, "acceptance_thresholds", thresholds)
        if isinstance(self.review_attestations, (str, bytes)) or not isinstance(
            self.review_attestations,
            Sequence,
        ):
            raise DomainValidationError(
                "quality.independence_invalid",
                "review_attestations must be a collection",
            )
        attestations = tuple(self.review_attestations)
        if any(type(item) is not IndependentReviewAttestationV1 for item in attestations):
            raise DomainValidationError(
                "quality.independence_invalid",
                "review_attestations must contain immutable validated attestations",
            )
        object.__setattr__(self, "review_attestations", attestations)
        if type(self.escalation) is not CompensationEscalationV1:
            raise DomainValidationError(
                "quality.escalation_invalid",
                "validated escalation is required",
            )
        if self.human_approval_ref is not None:
            object.__setattr__(
                self,
                "human_approval_ref",
                _task_text(self.human_approval_ref, "human_approval_ref"),
            )
        _reject_sensitive(self, "quality_compensation")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "QualityCompensationPlanV1":
        payload_keys = _validated_mapping_keys(
            payload,
            code="quality.schema_invalid",
            location="quality plan",
        )
        expected = {field_.name for field_ in fields(cls)}
        unknown = payload_keys - expected
        if unknown:
            raise DomainValidationError(
                "quality.schema_invalid",
                f"unexpected quality plan fields: {sorted(unknown)}",
            )
        values = dict(payload)
        thresholds = values.get("acceptance_thresholds", ())
        if isinstance(thresholds, (str, bytes)) or not isinstance(thresholds, Sequence):
            raise DomainValidationError("quality.threshold_invalid", "malformed thresholds")
        values["acceptance_thresholds"] = tuple(
            AcceptanceThresholdV1.from_mapping(asdict(item))
            if type(item) is AcceptanceThresholdV1
            else AcceptanceThresholdV1.from_mapping(item)
            if isinstance(item, Mapping)
            else _raise_quality_threshold()
            for item in thresholds
        )
        escalation = values.get("escalation")
        if type(escalation) is CompensationEscalationV1:
            values["escalation"] = CompensationEscalationV1.from_mapping(asdict(escalation))
        elif isinstance(escalation, Mapping):
            values["escalation"] = CompensationEscalationV1.from_mapping(escalation)
        attestations = values.get("review_attestations", ())
        if isinstance(attestations, (str, bytes)) or not isinstance(attestations, Sequence):
            raise DomainValidationError("quality.independence_invalid", "malformed review attestations")
        values["review_attestations"] = tuple(
            IndependentReviewAttestationV1.from_mapping(asdict(item))
            if type(item) is IndependentReviewAttestationV1
            else IndependentReviewAttestationV1.from_mapping(item)
            if isinstance(item, Mapping)
            else _raise_review_attestation()
            for item in attestations
        )
        try:
            return cls(**values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise DomainValidationError("quality.schema_invalid", "malformed quality plan") from exc

    def valid_for(
        self,
        *,
        decision_id: str,
        prior_route_id: str,
        selected_route_id: str,
        attempt_id: str,
        task_verification_minimum: str,
        task_independence_required: bool,
        task_human_gate_required: bool,
        decision_policy_version: str,
        decision_verification: str,
        trusted_reviewer_routes: Mapping[str, RouteV1] | None,
        trusted_human_approval_refs: frozenset[str],
        trusted_execution_routes: Mapping[str, RouteV1],
        trusted_execution_evidence: Mapping[str, frozenset[str]],
        trusted_evidence_refs: frozenset[str],
        trusted_threshold_results: Mapping[str, bool],
    ) -> bool:
        if task_verification_minimum not in _VERIFICATION_RANK:
            return False
        if (
            self.decision_id != decision_id
            or self.prior_route_id != prior_route_id
            or self.selected_route_id != selected_route_id
            or self.trigger_kind != ErrorKind.CAPACITY_EXHAUSTED.value
            or self.policy_version != decision_policy_version
            or _VERIFICATION_RANK[self.required_verification]
            < _VERIFICATION_RANK[task_verification_minimum]
            or decision_verification not in _VERIFICATION_RANK
            or _VERIFICATION_RANK[decision_verification]
            < _VERIFICATION_RANK[self.required_verification]
            or (task_independence_required and not self.independence_required)
            or (
                self.human_approval_ref is not None
                and self.human_approval_ref not in trusted_human_approval_refs
            )
            or (
                task_human_gate_required
                and (
                    self.human_approval_ref is None
                    or self.human_approval_ref not in trusted_human_approval_refs
                )
            )
            or not set(self.evidence_refs).issubset(trusted_evidence_refs)
        ):
            return False
        if _VERIFICATION_RANK[self.required_verification] >= 2 and not self.independence_required:
            return False
        if self.independence_required:
            if not self.required_reviewers:
                return False
            attestations = {item.reviewer: item for item in self.review_attestations}
            if set(attestations) != set(self.required_reviewers):
                return False
            for attestation in attestations.values():
                reviewer_route = (
                    trusted_reviewer_routes.get(attestation.route_id)
                    if trusted_reviewer_routes is not None
                    else None
                )
                if (
                    type(reviewer_route) is not RouteV1
                    or reviewer_route.route_id != attestation.route_id
                    or reviewer_route.quota_pool_id != attestation.quota_pool_id
                    or reviewer_route.billing_pool_id != attestation.billing_pool_id
                    or attestation.reviewed_execution_id != attempt_id
                    or trusted_execution_routes.get(attestation.execution_id)
                    != reviewer_route
                    or attestation.evidence_ref
                    not in trusted_execution_evidence.get(
                        attestation.execution_id,
                        frozenset(),
                    )
                    or attestation.evidence_ref not in trusted_evidence_refs
                    or attestation.route_id in {prior_route_id, selected_route_id}
                    or attestation.quota_pool_id.casefold()
                    in {
                        self.prior_quota_pool_id.casefold(),
                        self.selected_quota_pool_id.casefold(),
                    }
                    or attestation.billing_pool_id.casefold()
                    in {
                        self.prior_billing_pool_id.casefold(),
                        self.selected_billing_pool_id.casefold(),
                    }
                    or attestation.execution_id == attempt_id
                    or attestation.execution_id == attestation.reviewed_execution_id
                    or attestation.evidence_ref not in self.evidence_refs
                ):
                    return False
        for threshold in self.acceptance_thresholds:
            if threshold.evidence_required and (
                not threshold.evidence_ref
                or threshold.evidence_ref not in self.evidence_refs
                or threshold.evidence_ref not in trusted_evidence_refs
                or trusted_threshold_results.get(threshold.evidence_ref) is not True
            ):
                return False
        evidence = set(self.evidence_refs)
        for threshold in self.acceptance_thresholds:
            if not threshold.evidence_required or threshold.met is not True:
                return False
            if not threshold.evidence_ref or threshold.evidence_ref not in evidence:
                return False
        # This pure contract has no trusted approval authority. HUMAN_GATE is
        # therefore fail-closed even when caller-supplied references exist.
        if (
            self.escalation.on_unmet is EscalationAction.HUMAN_GATE
            and (
                self.human_approval_ref is None
                or self.human_approval_ref not in trusted_human_approval_refs
            )
        ):
            return False
        return True

    def matches_route_context(self, prior_route: RouteV1, selected_route: RouteV1) -> bool:
        return (
            self.prior_route_id == prior_route.route_id
            and self.selected_route_id == selected_route.route_id
            and self.prior_quota_pool_id == prior_route.quota_pool_id
            and self.prior_billing_pool_id == prior_route.billing_pool_id
            and self.selected_quota_pool_id == selected_route.quota_pool_id
            and self.selected_billing_pool_id == selected_route.billing_pool_id
        )


def _raise_candidate() -> CandidateEvaluation:
    raise DomainValidationError("candidate.invalid", "malformed candidate")


def _raise_quality_threshold() -> AcceptanceThresholdV1:
    raise DomainValidationError("quality.threshold_invalid", "malformed threshold")


def _raise_review_attestation() -> IndependentReviewAttestationV1:
    raise DomainValidationError("quality.independence_invalid", "malformed review attestation")


def _require_scored_eligible_candidates(
    candidates: Sequence[CandidateEvaluation],
    *,
    code: str,
) -> None:
    if any(
        candidate.eligible
        and (candidate.score is None or not candidate.score_factors)
        for candidate in candidates
    ):
        raise DomainValidationError(
            code,
            "every eligible candidate requires a finite score and at least one score factor",
        )


def _validated_decision_candidates(
    raw_candidates: object,
    *,
    route_registry: Mapping[str, RouteV1],
    effort: str,
    selected_route_id: str | None,
    prior_route_id: str | None,
    relation: DecisionRelation,
) -> tuple[tuple[CandidateEvaluation, ...], tuple[CandidateEvaluation, ...]]:
    if isinstance(raw_candidates, (str, bytes)) or not isinstance(raw_candidates, Sequence):
        raise DomainValidationError("decision.candidates_invalid", "candidates must be a collection")
    candidates = tuple(raw_candidates)
    if any(type(item) is not CandidateEvaluation for item in candidates):
        raise DomainValidationError("decision.candidates_invalid", "candidate DTO required")

    validated_candidates: list[CandidateEvaluation] = []
    for candidate in candidates:
        validated_route_context = route_registry.get(candidate.route_id)
        if validated_route_context is None:
            raise DomainValidationError(
                "decision.trusted_route_context_required",
                "every candidate requires trusted RouteV1 registry binding",
            )
        validated_candidates.append(
            CandidateEvaluation(
                validated_route_context,
                candidate.deterministic_status,
                candidate.rejection_codes,
                candidate.score,
                candidate.score_factors,
            )
        )
    candidates = tuple(validated_candidates)

    if effort == "E0" and (
        candidates
        or selected_route_id is not None
        or prior_route_id is not None
        or relation is not DecisionRelation.INITIAL
    ):
        raise DomainValidationError(
            "decision.effort_route_prohibited",
            "E0 decisions cannot evaluate or select model routes",
        )
    if effort != "E0":
        _require_scored_eligible_candidates(
            candidates,
            code="decision.candidate_score_required",
        )
    candidate_ids = tuple(candidate.route_id for candidate in candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise DomainValidationError("decision.candidates_invalid", "candidate route IDs must be unique")

    selected_candidates = tuple(
        candidate
        for candidate in candidates
        if candidate.eligible and candidate.route_id == selected_route_id
    )
    if selected_route_id is not None and len(selected_candidates) != 1:
        raise DomainValidationError(
            "decision.selected_candidate_ineligible",
            "selected route must be exactly one eligible candidate",
        )
    eligible_ranked = sorted(
        (candidate for candidate in candidates if candidate.eligible),
        key=lambda candidate: (
            -candidate.score,  # type: ignore[operator]
            candidate.route_id,
        ),
    )
    if (
        selected_route_id is not None
        and eligible_ranked
        and selected_route_id != eligible_ranked[0].route_id
    ):
        raise DomainValidationError(
            "decision.selection_rank_invalid",
            "selected route must be the highest-ranked eligible candidate",
        )
    if (
        relation is DecisionRelation.INITIAL
        and effort != "E0"
        and selected_route_id is None
    ):
        raise DomainValidationError(
            "decision.selected_candidate_required",
            "initial decision requires an eligible selected candidate",
        )
    return candidates, selected_candidates


@dataclass(frozen=True)
class _DecisionAuthorities:
    task: TaskEnvelope
    route_registry: Mapping[str, RouteV1]
    reviewer_routes: Mapping[str, RouteV1]
    approval_refs: frozenset[str]
    execution_routes: Mapping[str, RouteV1]
    execution_evidence: Mapping[str, frozenset[str]]
    evidence_refs: frozenset[str]
    threshold_results: Mapping[str, bool]


def _validated_decision_authorities(
    decision: "RouteDecisionV1",
    *,
    trusted_task: TaskEnvelope | None,
    trusted_routes: Mapping[str, RouteV1] | None,
    trusted_reviewer_routes: Mapping[str, RouteV1] | None,
    trusted_human_approval_refs: Mapping[str, str] | None,
    trusted_execution_routes: Mapping[str, RouteV1] | None,
    trusted_execution_evidence: Mapping[str, Sequence[str]] | None,
    trusted_evidence_refs: Sequence[str] | None,
    trusted_threshold_results: Mapping[str, bool] | None,
) -> _DecisionAuthorities:
    if type(trusted_task) is not TaskEnvelope:
        raise DomainValidationError(
            "decision.trusted_task_context_required",
            "route decisions require trusted TaskEnvelope context",
        )
    validated_task = TaskEnvelope.from_mapping(asdict(trusted_task))
    if (
        decision.task_id != validated_task.task_id
        or decision.policy_version != validated_task.policy_version
        or decision.effort != validated_task.effort
        or _VERIFICATION_RANK[decision.verification]
        < _VERIFICATION_RANK[validated_task.verification.minimum]
    ):
        raise DomainValidationError(
            "decision.trusted_task_mismatch",
            "decision identity, policy, effort, and verification must match trusted task authority",
        )
    if not isinstance(trusted_routes, Mapping):
        raise DomainValidationError(
            "decision.trusted_route_context_required",
            "route decisions require a trusted RouteV1 registry",
        )
    route_registry = _revalidated_route_registry(
        trusted_routes,
        code="decision.trusted_route_context_invalid",
        location="trusted route registry",
    )
    reviewer_routes = _revalidated_route_registry(
        trusted_reviewer_routes,
        code="decision.trusted_reviewer_context_invalid",
        location="trusted reviewer registry",
    )

    if trusted_human_approval_refs is not None and not isinstance(
        trusted_human_approval_refs,
        Mapping,
    ):
        raise DomainValidationError(
            "decision.trusted_human_approval_invalid",
            "trusted human approvals must map approval reference to task ID",
        )
    approval_refs = frozenset(
        _task_text(approval_ref, "human_approval_ref")
        for approval_ref, approved_task_id in (trusted_human_approval_refs or {}).items()
        if _task_text(approved_task_id, "approved_task_id") == validated_task.task_id
    )

    execution_routes: dict[str, RouteV1] = {}
    if trusted_execution_routes is not None:
        if not isinstance(trusted_execution_routes, Mapping):
            raise DomainValidationError(
                "decision.trusted_execution_context_invalid",
                "trusted execution registry must be a mapping",
            )
        for execution_id, execution_route in trusted_execution_routes.items():
            normalized_execution_id = _task_text(execution_id, "execution_id")
            execution_routes[normalized_execution_id] = _revalidated_route(
                execution_route,
                "trusted execution route",
            )

    execution_evidence: dict[str, frozenset[str]] = {}
    if trusted_execution_evidence is not None:
        if not isinstance(trusted_execution_evidence, Mapping):
            raise DomainValidationError(
                "decision.trusted_execution_context_invalid",
                "trusted execution evidence must be a mapping",
            )
        for execution_id, execution_refs in trusted_execution_evidence.items():
            normalized_execution_id = _task_text(execution_id, "execution_id")
            execution_evidence[normalized_execution_id] = frozenset(
                _immutable_string_collection(
                    execution_refs,
                    "execution_evidence_refs",
                    require_nonempty=True,
                )
            )

    evidence_refs = frozenset(
        _immutable_string_collection(
            trusted_evidence_refs or (),
            "trusted_evidence_refs",
        )
    )
    threshold_results: dict[str, bool] = {}
    if trusted_threshold_results is not None:
        if not isinstance(trusted_threshold_results, Mapping):
            raise DomainValidationError(
                "decision.trusted_threshold_context_invalid",
                "trusted threshold results must be a mapping",
            )
        for evidence_ref, result in trusted_threshold_results.items():
            normalized_ref = _task_text(evidence_ref, "threshold_evidence_ref")
            if type(result) is not bool:
                raise DomainValidationError(
                    "decision.trusted_threshold_context_invalid",
                    "trusted threshold results must be boolean",
                )
            threshold_results[normalized_ref] = result
    return _DecisionAuthorities(
        task=validated_task,
        route_registry=route_registry,
        reviewer_routes=reviewer_routes,
        approval_refs=approval_refs,
        execution_routes=execution_routes,
        execution_evidence=execution_evidence,
        evidence_refs=evidence_refs,
        threshold_results=threshold_results,
    )


def _apply_decision_policy(
    decision: "RouteDecisionV1",
    *,
    authorities: _DecisionAuthorities,
    validated_prior_route: RouteV1 | None,
    selected_candidates: Sequence[CandidateEvaluation],
) -> None:
    if decision.relation in {DecisionRelation.FALLBACK, DecisionRelation.REPLAN}:
        if (
            not decision.selected_route_id
            or not decision.prior_route_id
            or not decision.parent_decision_id
        ):
            raise DomainValidationError(
                "decision.route_identity_required",
                "fallback/replan requires parent, prior, and selected route identity",
            )
        plan = _coerce_quality_plan(decision.quality_compensation_plan)
        object.__setattr__(decision, "quality_compensation_plan", plan)
        valid = (
            _VERIFICATION_RANK[decision.verification]
            >= _VERIFICATION_RANK[authorities.task.verification.minimum]
            and type(plan) is QualityCompensationPlanV1
            and validated_prior_route is not None
            and len(selected_candidates) == 1
            and getattr(selected_candidates[0], "_route_context", None) is not None
            and plan.matches_route_context(
                validated_prior_route,
                selected_candidates[0].route,
            )
            and plan.valid_for(
                decision_id=decision.decision_id,
                prior_route_id=decision.prior_route_id,
                selected_route_id=decision.selected_route_id,
                attempt_id=decision.attempt_id,
                task_verification_minimum=authorities.task.verification.minimum,
                task_independence_required=(
                    authorities.task.verification.independent_required
                ),
                task_human_gate_required=(
                    authorities.task.verification.human_gate_required
                ),
                decision_policy_version=decision.policy_version,
                decision_verification=decision.verification,
                trusted_reviewer_routes=authorities.reviewer_routes,
                trusted_human_approval_refs=authorities.approval_refs,
                trusted_execution_routes=authorities.execution_routes,
                trusted_execution_evidence=authorities.execution_evidence,
                trusted_evidence_refs=authorities.evidence_refs,
                trusted_threshold_results=authorities.threshold_results,
            )
        )
        if valid:
            object.__setattr__(decision, "policy_status", "AUTHORIZED")
            return
        object.__setattr__(
            decision,
            "policy_status",
            "ACTIVATION_BLOCKED_QUALITY_COMPENSATION",
        )
        object.__setattr__(
            decision,
            "activation_block_reason",
            "quality_compensation_insufficient",
        )
        if "quality_compensation_insufficient" not in decision.reason_codes:
            object.__setattr__(
                decision,
                "reason_codes",
                decision.reason_codes + ("quality_compensation_insufficient",),
            )
        if decision.reservation_id is not None:
            raise DomainValidationError(
                "decision.blocked_reservation",
                "activation-blocked fallback must not claim a reservation",
            )
        return

    if decision.relation is DecisionRelation.WAITING:
        if decision.selected_route_id is not None:
            raise DomainValidationError(
                "decision.waiting_selected_route",
                "waiting must not select a route",
            )
        if not decision.recheck_evidence:
            raise DomainValidationError(
                "replan.recheck_evidence_required",
                "waiting requires recheck evidence",
            )
        if any(candidate.eligible for candidate in decision.candidates):
            raise DomainValidationError(
                "decision.waiting_eligible_candidate",
                "waiting requires every retained candidate to be ineligible",
            )
        if decision.reservation_id is not None:
            raise DomainValidationError(
                "decision.waiting_reservation",
                "waiting must not hold a reservation",
            )
        object.__setattr__(decision, "policy_status", "WAITING_FOR_CAPACITY")
        return

    if decision.effort == "E0":
        object.__setattr__(decision, "policy_status", "NO_ROUTE_REQUIRED")
    elif authorities.task.verification.independent_required:
        object.__setattr__(
            decision,
            "policy_status",
            "ACTIVATION_BLOCKED_INDEPENDENT_REVIEW",
        )
        object.__setattr__(
            decision,
            "activation_block_reason",
            "independent_review_required",
        )
    elif (
        authorities.task.verification.human_gate_required
        and not authorities.approval_refs
    ):
        object.__setattr__(decision, "policy_status", "ACTIVATION_BLOCKED_HUMAN_GATE")
        object.__setattr__(decision, "activation_block_reason", "human_gate_required")
        if "human_gate_required" not in decision.reason_codes:
            object.__setattr__(
                decision,
                "reason_codes",
                decision.reason_codes + ("human_gate_required",),
            )
    else:
        object.__setattr__(decision, "policy_status", "AUTHORIZED")
    if not decision.dispatchable and decision.reservation_id is not None:
        raise DomainValidationError(
            "decision.blocked_reservation",
            "non-dispatchable initial decision must not claim a reservation",
        )


@dataclass(frozen=True)
class RouteDecisionV1:
    decision_id: str
    task_id: str
    attempt_id: str
    created_at: str
    policy_version: str
    router_version: str
    capacity_view_id: str
    effort: str
    verification: str
    fallback: bool
    relation: DecisionRelation
    candidates: tuple[CandidateEvaluation, ...]
    selected_route_id: str | None
    trigger: InitialSelectionTriggerV1 | RuntimeErrorClassificationV1
    reason_codes: tuple[str, ...]
    prior_route_id: str | None = None
    parent_decision_id: str | None = None
    reservation_id: str | None = None
    quality_compensation_plan: QualityCompensationPlanV1 | None = None
    recheck_evidence: tuple[str, ...] = ()
    schema_version: str = "route-decision/v1"
    trusted_task: InitVar[TaskEnvelope | None] = None
    trusted_routes: InitVar[Mapping[str, RouteV1] | None] = None
    trusted_reviewer_routes: InitVar[Mapping[str, RouteV1] | None] = None
    trusted_prior_route: InitVar[RouteV1 | None] = None
    trusted_human_approval_refs: InitVar[Mapping[str, str] | None] = None
    trusted_execution_routes: InitVar[Mapping[str, RouteV1] | None] = None
    trusted_execution_evidence: InitVar[Mapping[str, Sequence[str]] | None] = None
    trusted_evidence_refs: InitVar[Sequence[str] | None] = None
    trusted_threshold_results: InitVar[Mapping[str, bool] | None] = None
    policy_status: str = field(default="", init=False)
    activation_block_reason: str | None = field(default=None, init=False)

    def __post_init__(
        self,
        trusted_task: TaskEnvelope | None,
        trusted_routes: Mapping[str, RouteV1] | None,
        trusted_reviewer_routes: Mapping[str, RouteV1] | None,
        trusted_prior_route: RouteV1 | None,
        trusted_human_approval_refs: Mapping[str, str] | None,
        trusted_execution_routes: Mapping[str, RouteV1] | None,
        trusted_execution_evidence: Mapping[str, Sequence[str]] | None,
        trusted_evidence_refs: Sequence[str] | None,
        trusted_threshold_results: Mapping[str, bool] | None,
    ) -> None:
        if self.schema_version != "route-decision/v1":
            raise DomainValidationError("decision.schema_invalid", "schema_version must be route-decision/v1")
        for name in (
            "decision_id",
            "task_id",
            "attempt_id",
            "created_at",
            "policy_version",
            "router_version",
            "capacity_view_id",
        ):
            object.__setattr__(self, name, _task_text(getattr(self, name), name))
        if self.selected_route_id is not None:
            object.__setattr__(
                self,
                "selected_route_id",
                _validated_route_id(self.selected_route_id, "selected_route_id"),
            )
        if self.prior_route_id is not None:
            object.__setattr__(
                self,
                "prior_route_id",
                _validated_route_id(self.prior_route_id, "prior_route_id"),
            )
        for name in ("parent_decision_id", "reservation_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _task_text(value, name))
        if self.effort not in {f"E{level}" for level in range(5)}:
            raise DomainValidationError("decision.effort_invalid", "effort must be E0 through E4")
        if self.verification not in _VERIFICATION_RANK:
            raise DomainValidationError("decision.verification_invalid", "verification must be V0 through V4")
        authorities = _validated_decision_authorities(
            self,
            trusted_task=trusted_task,
            trusted_routes=trusted_routes,
            trusted_reviewer_routes=trusted_reviewer_routes,
            trusted_human_approval_refs=trusted_human_approval_refs,
            trusted_execution_routes=trusted_execution_routes,
            trusted_execution_evidence=trusted_execution_evidence,
            trusted_evidence_refs=trusted_evidence_refs,
            trusted_threshold_results=trusted_threshold_results,
        )
        route_registry = authorities.route_registry
        if not isinstance(self.fallback, bool) or self.fallback is not (
            self.relation is DecisionRelation.FALLBACK
        ):
            raise DomainValidationError("decision.fallback_invalid", "fallback must exactly match FALLBACK relation")
        if type(self.relation) is not DecisionRelation:
            raise DomainValidationError("decision.relation_invalid", "invalid decision relation")
        expected_trigger_type = (
            InitialSelectionTriggerV1
            if self.relation is DecisionRelation.INITIAL
            else RuntimeErrorClassificationV1
        )
        if type(self.trigger) is not expected_trigger_type:
            raise DomainValidationError(
                "decision.trigger_invalid",
                "trigger type must match the decision relation",
            )
        validated_trigger = (
            InitialSelectionTriggerV1.from_mapping(asdict(self.trigger))
            if type(self.trigger) is InitialSelectionTriggerV1
            else RuntimeErrorClassificationV1.from_mapping(asdict(self.trigger))
        )
        object.__setattr__(self, "trigger", validated_trigger)
        validated_prior_route: RouteV1 | None = None
        if self.relation in {
            DecisionRelation.FALLBACK,
            DecisionRelation.REPLAN,
            DecisionRelation.WAITING,
        }:
            if type(trusted_prior_route) is not RouteV1:
                raise DomainValidationError(
                    "decision.trusted_prior_route_required",
                    "dynamic decisions require trusted prior RouteV1 context",
                )
            validated_prior_route = _revalidated_route(
                trusted_prior_route,
                "trusted prior route",
            )
        if (
            self.relation
            in {DecisionRelation.FALLBACK, DecisionRelation.REPLAN, DecisionRelation.WAITING}
            and type(self.trigger) is RuntimeErrorClassificationV1
            and (
                self.trigger.attempted_route_id != self.prior_route_id
                or validated_prior_route is None
                or self.prior_route_id != validated_prior_route.route_id
                or self.trigger.quota_pool_id != validated_prior_route.quota_pool_id
                or self.trigger.billing_pool_id != validated_prior_route.billing_pool_id
            )
        ):
            raise DomainValidationError(
                "decision.trigger_route_mismatch",
                "fallback/replan/waiting trigger must identify the prior attempted route",
            )
        candidates, selected_candidates = _validated_decision_candidates(
            self.candidates,
            route_registry=route_registry,
            effort=self.effort,
            selected_route_id=self.selected_route_id,
            prior_route_id=self.prior_route_id,
            relation=self.relation,
        )
        object.__setattr__(self, "candidates", candidates)
        for name in ("reason_codes", "recheck_evidence"):
            object.__setattr__(
                self,
                name,
                _immutable_string_collection(getattr(self, name), name),
            )

        _reject_sensitive(
            {
                "reason_codes": self.reason_codes,
                "recheck_evidence": self.recheck_evidence,
            },
            "decision",
        )

        _apply_decision_policy(
            self,
            authorities=authorities,
            validated_prior_route=validated_prior_route,
            selected_candidates=selected_candidates,
        )
        _reject_sensitive(self, "decision")

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, object],
        *,
        trusted_routes: Mapping[str, RouteV1] | None = None,
        trusted_task: TaskEnvelope | None = None,
        trusted_reviewer_routes: Mapping[str, RouteV1] | None = None,
        trusted_human_approval_refs: Mapping[str, str] | None = None,
        trusted_execution_routes: Mapping[str, RouteV1] | None = None,
        trusted_execution_evidence: Mapping[str, Sequence[str]] | None = None,
        trusted_evidence_refs: Sequence[str] | None = None,
        trusted_threshold_results: Mapping[str, bool] | None = None,
    ) -> "RouteDecisionV1":
        if not isinstance(payload, Mapping):
            raise DomainValidationError(
                "decision.schema_invalid", "route decision must be a mapping"
            )
        _reject_sensitive(payload, "decision")
        allowed = {field_.name for field_ in fields(cls)}
        payload_keys = _validated_mapping_keys(
            payload,
            code="decision.unexpected_field",
            location="route decision",
        )
        unknown = payload_keys - allowed
        if unknown:
            raise DomainValidationError(
                "decision.unexpected_field",
                f"unexpected decision fields: {sorted(unknown)}",
            )
        required_derived = {"policy_status", "activation_block_reason"}
        if not required_derived.issubset(payload):
            raise DomainValidationError(
                "decision.persisted_status_required",
                "persisted decision requires policy_status and activation_block_reason",
            )
        values = dict(payload)
        persisted_policy_status = values.pop("policy_status")
        persisted_block_reason = values.pop("activation_block_reason")
        try:
            relation = values.get("relation")
            if not isinstance(relation, DecisionRelation):
                values["relation"] = DecisionRelation(str(relation))
            resolved_relation = values["relation"]
            prior_route: RouteV1 | None = None
            dynamic_relations = {
                DecisionRelation.FALLBACK,
                DecisionRelation.REPLAN,
                DecisionRelation.WAITING,
            }
            if type(trusted_task) is not TaskEnvelope:
                raise DomainValidationError(
                    "decision.trusted_task_context_required",
                    "persisted route decision requires trusted TaskEnvelope context",
                )
            validated_task = TaskEnvelope.from_mapping(asdict(trusted_task))
            if (
                values.get("task_id") != validated_task.task_id
                or values.get("policy_version") != validated_task.policy_version
                or values.get("effort") != validated_task.effort
                or values.get("verification") not in _VERIFICATION_RANK
                or _VERIFICATION_RANK[str(values.get("verification"))]
                < _VERIFICATION_RANK[validated_task.verification.minimum]
            ):
                raise DomainValidationError(
                    "decision.trusted_task_context_invalid",
                    "persisted decision must satisfy trusted task identity and policy",
                )
            if not isinstance(trusted_routes, Mapping):
                raise DomainValidationError(
                    "decision.trusted_route_context_required",
                    "persisted route decision requires trusted route registry rehydration",
                )
            validated_registry: dict[str, RouteV1] = {}
            for key, route in trusted_routes.items():
                if type(key) is not str or type(route) is not RouteV1:
                    raise DomainValidationError(
                        "decision.trusted_route_context_invalid",
                        "trusted route registry requires string keys and RouteV1 values",
                    )
                validated_route = _revalidated_route(route, "trusted route registry")
                if key != validated_route.route_id:
                    raise DomainValidationError(
                        "decision.trusted_route_context_invalid",
                        "trusted route registry keys must match validated RouteV1 identities",
                    )
                validated_registry[key] = validated_route
            trusted_routes = validated_registry
            trigger = values.get("trigger")
            if isinstance(trigger, Mapping):
                if trigger.get("schema_version") == "initial-selection-trigger/v1":
                    values["trigger"] = InitialSelectionTriggerV1.from_mapping(trigger)
                else:
                    values["trigger"] = RuntimeErrorClassificationV1.from_mapping(trigger)
            plan = values.get("quality_compensation_plan")
            if isinstance(plan, Mapping):
                values["quality_compensation_plan"] = QualityCompensationPlanV1.from_mapping(
                    plan
                )
            raw_candidates = values.get("candidates", ())
            if isinstance(raw_candidates, (str, bytes, bytearray)) or not isinstance(
                raw_candidates,
                Sequence,
            ):
                raise TypeError("candidates")
            candidates: list[CandidateEvaluation] = []
            for item in raw_candidates:
                if type(item) is CandidateEvaluation:
                    if resolved_relation in DecisionRelation:
                        route_context = trusted_routes.get(item.route_id) if trusted_routes else None
                        if route_context is None:
                            raise DomainValidationError(
                                "decision.trusted_route_context_required",
                                "every persisted fallback candidate requires trusted RouteV1 context",
                            )
                        candidate = CandidateEvaluation(
                            route_context,
                            item.deterministic_status,
                            item.rejection_codes,
                            item.score,
                            item.score_factors,
                        )
                    else:
                        candidate = item
                elif isinstance(item, Mapping):
                    persisted_route_id = _validated_route_id(item.get("route_id"), "route_id")
                    route_context = (
                        trusted_routes.get(persisted_route_id)
                        if trusted_routes is not None
                        else None
                    )
                    if (
                        resolved_relation in DecisionRelation
                        and route_context is None
                    ):
                        raise DomainValidationError(
                            "decision.trusted_route_context_required",
                            "every persisted fallback candidate requires trusted RouteV1 context",
                        )
                    candidate = CandidateEvaluation.from_mapping(
                        item,
                        route_context=route_context,
                    )
                else:
                    candidate = _raise_candidate()
                candidates.append(candidate)
            values["candidates"] = tuple(candidates)
            if resolved_relation in dynamic_relations:
                prior_route_id = _validated_route_id(values.get("prior_route_id"), "prior_route_id")
                prior_route = trusted_routes.get(prior_route_id) if trusted_routes else None
                selected_route = None
                if resolved_relation in {DecisionRelation.FALLBACK, DecisionRelation.REPLAN}:
                    selected_route_id = _validated_route_id(
                        values.get("selected_route_id"),
                        "selected_route_id",
                    )
                    selected_route = trusted_routes.get(selected_route_id) if trusted_routes else None
                if prior_route is None or (
                    resolved_relation in {DecisionRelation.FALLBACK, DecisionRelation.REPLAN}
                    and selected_route is None
                ):
                    raise DomainValidationError(
                        "decision.trusted_route_context_required",
                        "prior and selected routes require trusted RouteV1 context",
                    )
                parsed_trigger = values.get("trigger")
                if (
                    type(parsed_trigger) is not RuntimeErrorClassificationV1
                    or parsed_trigger.attempted_route_id != prior_route.route_id
                    or parsed_trigger.quota_pool_id != prior_route.quota_pool_id
                    or parsed_trigger.billing_pool_id != prior_route.billing_pool_id
                ):
                    raise DomainValidationError(
                        "decision.trigger_route_mismatch",
                        "persisted trigger identity and pools must match the trusted prior route",
                    )
                parsed_plan = values.get("quality_compensation_plan")
                if (
                    type(parsed_plan) is QualityCompensationPlanV1
                    and type(selected_route) is RouteV1
                    and not parsed_plan.matches_route_context(prior_route, selected_route)
                ):
                    raise DomainValidationError(
                        "quality.route_binding_invalid",
                        "persisted compensation pool identities do not match trusted routes",
                    )
            for collection_name in ("reason_codes", "recheck_evidence"):
                if collection_name in values:
                    raw_collection = values[collection_name]
                    if isinstance(raw_collection, (str, bytes, bytearray)) or not isinstance(
                        raw_collection, Sequence
                    ):
                        raise TypeError(collection_name)
                    values[collection_name] = tuple(raw_collection)
            values["trusted_task"] = validated_task
            values["trusted_routes"] = trusted_routes
            values["trusted_reviewer_routes"] = trusted_reviewer_routes
            values["trusted_human_approval_refs"] = trusted_human_approval_refs
            values["trusted_execution_routes"] = trusted_execution_routes
            values["trusted_execution_evidence"] = trusted_execution_evidence
            values["trusted_evidence_refs"] = trusted_evidence_refs
            values["trusted_threshold_results"] = trusted_threshold_results
            values["trusted_prior_route"] = (
                prior_route if resolved_relation in dynamic_relations else None
            )
            decision = cls(**values)  # type: ignore[arg-type]
            if (
                persisted_policy_status != decision.policy_status
                or persisted_block_reason != decision.activation_block_reason
            ):
                raise DomainValidationError(
                    "decision.persisted_status_mismatch",
                    "persisted derived decision status does not match contract validation",
                )
            return decision
        except (TypeError, ValueError) as exc:
            if isinstance(exc, DomainValidationError):
                raise
            raise DomainValidationError(
                "decision.schema_invalid", "route decision payload is malformed"
            ) from exc

    @property
    def dispatchable(self) -> bool:
        return bool(self.selected_route_id) and self.policy_status == "AUTHORIZED"


def _coerce_quality_plan(
    value: QualityCompensationPlanV1 | Mapping[str, object] | None,
) -> QualityCompensationPlanV1 | None:
    if value is None:
        return None
    if type(value) is QualityCompensationPlanV1:
        try:
            return QualityCompensationPlanV1.from_mapping(asdict(value))
        except (DomainValidationError, TypeError, ValueError):
            return None
    if isinstance(value, Mapping):
        try:
            return QualityCompensationPlanV1.from_mapping(value)
        except DomainValidationError:
            return None
    return None


def replan_after_capacity_exhaustion(
    *,
    trusted_task: TaskEnvelope,
    task_id: str,
    attempt_id: str,
    failed_route: RouteV1,
    classification: RuntimeErrorClassificationV1,
    candidates: Sequence[CandidateEvaluation],
    decision_id: str,
    parent_decision_id: str,
    created_at: str,
    policy_version: str,
    router_version: str,
    capacity_view_id: str,
    effort: str,
    verification: str,
    quality_compensation_plan: QualityCompensationPlanV1 | Mapping[str, object] | None = None,
    trusted_reviewer_routes: Mapping[str, RouteV1] | None = None,
    trusted_human_approval_refs: Mapping[str, str] | None = None,
    trusted_execution_routes: Mapping[str, RouteV1] | None = None,
    trusted_execution_evidence: Mapping[str, Sequence[str]] | None = None,
    trusted_evidence_refs: Sequence[str] | None = None,
    trusted_threshold_results: Mapping[str, bool] | None = None,
    recheck_evidence: Sequence[str] = (),
) -> RouteDecisionV1:
    if type(trusted_task) is not TaskEnvelope:
        raise DomainValidationError(
            "replan.trusted_task_required",
            "capacity replan requires trusted TaskEnvelope authority",
        )
    trusted_task = TaskEnvelope.from_mapping(asdict(trusted_task))
    if trusted_task.effort == "E0":
        raise DomainValidationError(
            "replan.effort_route_prohibited",
            "E0 tasks cannot enter route-scoped capacity replanning",
        )
    if (
        task_id != trusted_task.task_id
        or policy_version != trusted_task.policy_version
        or effort != trusted_task.effort
        or verification not in _VERIFICATION_RANK
        or _VERIFICATION_RANK[verification]
        < _VERIFICATION_RANK[trusted_task.verification.minimum]
    ):
        raise DomainValidationError(
            "replan.trusted_task_mismatch",
            "replan task, policy, effort, and verification must satisfy trusted task",
        )
    failed_route = _revalidated_route(failed_route, "failed route")
    if type(classification) is not RuntimeErrorClassificationV1:
        raise DomainValidationError(
            "classification.capacity_scope_required",
            "validated runtime classification is required",
        )
    classification = RuntimeErrorClassificationV1.from_mapping(asdict(classification))
    if (
        classification.attempted_route_id != failed_route.route_id
        or classification.quota_pool_id.casefold() != failed_route.quota_pool_id
        or (
            classification.billing_pool_id is not None
            and classification.billing_pool_id != failed_route.billing_pool_id
        )
    ):
        raise DomainValidationError(
            "classification.capacity_scope_required",
            "classification must match attempted route and quota/billing pools",
        )
    if classification.billing_pool_id is None:
        classification = RuntimeErrorClassificationV1(
            kind=classification.kind,
            source=classification.source,
            attempted_route_id=classification.attempted_route_id,
            quota_pool_id=classification.quota_pool_id,
            billing_pool_id=failed_route.billing_pool_id,
            classified_at=classification.classified_at,
            evidence_code=classification.evidence_code,
        )
    evaluated_candidates: list[CandidateEvaluation] = []
    for candidate in candidates:
        if type(candidate) is not CandidateEvaluation:
            raise DomainValidationError("decision.candidates_invalid", "candidate DTO required")
        candidate_route = _revalidated_route(candidate.route, "candidate route")
        validated_candidate = CandidateEvaluation(
            candidate_route,
            candidate.deterministic_status,
            candidate.rejection_codes,
            candidate.score,
            candidate.score_factors,
        )
        if candidate_route.quota_pool_id == failed_route.quota_pool_id:
            rejection_codes = validated_candidate.rejection_codes
            if "same_exhausted_quota_pool" not in rejection_codes:
                rejection_codes += ("same_exhausted_quota_pool",)
            evaluated_candidates.append(
                CandidateEvaluation(
                    candidate_route,
                    False,
                    rejection_codes,
                )
            )
        else:
            evaluated_candidates.append(validated_candidate)
    _require_scored_eligible_candidates(
        evaluated_candidates,
        code="replan.candidate_score_required",
    )
    eligible = sorted(
        (candidate for candidate in evaluated_candidates if candidate.eligible),
        key=lambda candidate: (
            -candidate.score,  # type: ignore[operator]
            candidate.route_id,
        ),
    )
    trusted_route_registry = {
        failed_route.route_id: failed_route,
        **{candidate.route_id: candidate.route for candidate in evaluated_candidates},
    }
    if eligible:
        selected = eligible[0]
        plan = _coerce_quality_plan(quality_compensation_plan)
        if plan is not None and not plan.matches_route_context(failed_route, selected.route):
            plan = None
        return RouteDecisionV1(
            decision_id=decision_id,
            task_id=task_id,
            attempt_id=attempt_id,
            created_at=created_at,
            policy_version=policy_version,
            router_version=router_version,
            capacity_view_id=capacity_view_id,
            effort=effort,
            verification=verification,
            fallback=True,
            relation=DecisionRelation.FALLBACK,
            candidates=tuple(evaluated_candidates),
            selected_route_id=selected.route_id,
            trigger=classification,
            reason_codes=("route_capacity_exhausted",),
            prior_route_id=failed_route.route_id,
            parent_decision_id=parent_decision_id,
            quality_compensation_plan=plan,
            trusted_task=trusted_task,
            trusted_routes=trusted_route_registry,
            trusted_reviewer_routes=trusted_reviewer_routes,
            trusted_prior_route=failed_route,
            trusted_human_approval_refs=trusted_human_approval_refs,
            trusted_execution_routes=trusted_execution_routes,
            trusted_execution_evidence=trusted_execution_evidence,
            trusted_evidence_refs=trusted_evidence_refs,
            trusted_threshold_results=trusted_threshold_results,
        )
    return RouteDecisionV1(
        decision_id=decision_id,
        task_id=task_id,
        attempt_id=attempt_id,
        created_at=created_at,
        policy_version=policy_version,
        router_version=router_version,
        capacity_view_id=capacity_view_id,
        effort=effort,
        verification=verification,
        fallback=False,
        relation=DecisionRelation.WAITING,
        candidates=tuple(evaluated_candidates),
        selected_route_id=None,
        trigger=classification,
        reason_codes=("no_eligible_routes",),
        prior_route_id=failed_route.route_id,
        parent_decision_id=parent_decision_id,
        quality_compensation_plan=None,
        recheck_evidence=tuple(recheck_evidence),
        trusted_task=trusted_task,
        trusted_routes=trusted_route_registry,
        trusted_reviewer_routes=trusted_reviewer_routes,
        trusted_prior_route=failed_route,
        trusted_human_approval_refs=trusted_human_approval_refs,
        trusted_execution_routes=trusted_execution_routes,
        trusted_execution_evidence=trusted_execution_evidence,
        trusted_evidence_refs=trusted_evidence_refs,
        trusted_threshold_results=trusted_threshold_results,
    )


E = TypeVar("E")
S = TypeVar("S", bound=Enum)


def _validate_audit_event(
    event: object,
    state_type: type[Enum],
    from_state: object,
    to_state: object,
    identity_field: str,
) -> None:
    if type(from_state) is not state_type or type(to_state) is not state_type:
        raise DomainValidationError(
            "state.domain_mismatch",
            f"transition states must both be {state_type.__name__}",
        )
    for name in (identity_field, "actor", "timestamp", "reason", "correlation_id"):
        value = getattr(event, name)
        if not isinstance(value, str) or not value.strip(_ASCII_WHITESPACE):
            raise DomainValidationError(
                "state.audit_metadata_required",
                f"{name} is required for state transitions",
            )
        object.__setattr__(
            event,
            name,
            unicodedata.normalize("NFC", value.strip(_ASCII_WHITESPACE)),
        )
    _reject_sensitive(event, "state_event")


def _ensure_legal_transition(
    from_state: S,
    to_state: S,
    allowed: Mapping[S, frozenset[S]],
) -> None:
    if to_state not in allowed.get(from_state, frozenset()):
        raise DomainValidationError(
            "state.transition_illegal",
            f"illegal {type(from_state).__name__} transition {from_state.value}->{to_state.value}",
        )


@dataclass(frozen=True)
class TaskStateEvent:
    task_id: str
    from_state: TaskState
    to_state: TaskState
    actor: str
    timestamp: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        _validate_audit_event(self, TaskState, self.from_state, self.to_state, "task_id")
        _ensure_legal_transition(self.from_state, self.to_state, _TASK_TRANSITIONS)


@dataclass(frozen=True)
class AttemptStateEvent:
    attempt_id: str
    from_state: AttemptState
    to_state: AttemptState
    actor: str
    timestamp: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        _validate_audit_event(self, AttemptState, self.from_state, self.to_state, "attempt_id")
        _ensure_legal_transition(self.from_state, self.to_state, _ATTEMPT_TRANSITIONS)


@dataclass(frozen=True)
class RouteStateEvent:
    route_id: str
    from_state: RouteState
    to_state: RouteState
    actor: str
    timestamp: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        _validate_audit_event(self, RouteState, self.from_state, self.to_state, "route_id")
        _ensure_legal_transition(self.from_state, self.to_state, _ROUTE_TRANSITIONS)


@dataclass(frozen=True)
class CredentialStateEvent:
    credential_id: str
    from_state: CredentialState
    to_state: CredentialState
    actor: str
    timestamp: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        _validate_audit_event(
            self, CredentialState, self.from_state, self.to_state, "credential_id"
        )
        _ensure_legal_transition(self.from_state, self.to_state, _CREDENTIAL_TRANSITIONS)


@dataclass(frozen=True)
class ReservationStateEvent:
    reservation_id: str
    from_state: ReservationState
    to_state: ReservationState
    actor: str
    timestamp: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        _validate_audit_event(
            self, ReservationState, self.from_state, self.to_state, "reservation_id"
        )
        _ensure_legal_transition(self.from_state, self.to_state, _RESERVATION_TRANSITIONS)


@dataclass(frozen=True)
class ReviewStateEvent:
    review_id: str
    from_state: ReviewState
    to_state: ReviewState
    actor: str
    timestamp: str
    reason: str
    correlation_id: str

    def __post_init__(self) -> None:
        _validate_audit_event(self, ReviewState, self.from_state, self.to_state, "review_id")
        _ensure_legal_transition(self.from_state, self.to_state, _REVIEW_TRANSITIONS)


_TASK_TRANSITIONS = {
    TaskState.NEW: frozenset({TaskState.PLANNED, TaskState.CANCELLED}),
    TaskState.PLANNED: frozenset(
        {TaskState.ROUTED, TaskState.WAITING_FOR_CAPACITY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.ROUTED: frozenset(
        {TaskState.DISPATCHED, TaskState.WAITING_FOR_CAPACITY, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.DISPATCHED: frozenset(
        {TaskState.VERIFYING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.VERIFYING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.WAITING_FOR_CAPACITY,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING_FOR_CAPACITY: frozenset(
        {TaskState.PLANNED, TaskState.ROUTED, TaskState.FAILED, TaskState.CANCELLED}
    ),
}
_ATTEMPT_TRANSITIONS = {
    AttemptState.CREATED: frozenset(
        {AttemptState.RESERVED, AttemptState.FAILED, AttemptState.CANCELLED}
    ),
    AttemptState.RESERVED: frozenset(
        {AttemptState.DISPATCHED, AttemptState.FAILED, AttemptState.CANCELLED}
    ),
    AttemptState.DISPATCHED: frozenset(
        {AttemptState.RUNNING, AttemptState.FAILED, AttemptState.CANCELLED}
    ),
    AttemptState.RUNNING: frozenset(
        {AttemptState.RESULT_RECORDED, AttemptState.FAILED, AttemptState.CANCELLED}
    ),
    AttemptState.RESULT_RECORDED: frozenset(
        {AttemptState.VERIFIED, AttemptState.FAILED, AttemptState.CANCELLED}
    ),
}
_ROUTE_TRANSITIONS = {
    RouteState.DISCOVERED: frozenset(
        {
            RouteState.ELIGIBLE,
            RouteState.INELIGIBLE,
            RouteState.COOLDOWN,
            RouteState.BREAKER_OPEN,
            RouteState.RETIRED,
        }
    ),
    RouteState.ELIGIBLE: frozenset(
        {RouteState.INELIGIBLE, RouteState.COOLDOWN, RouteState.BREAKER_OPEN, RouteState.RETIRED}
    ),
    RouteState.INELIGIBLE: frozenset({RouteState.ELIGIBLE, RouteState.RETIRED}),
    RouteState.COOLDOWN: frozenset(
        {RouteState.ELIGIBLE, RouteState.INELIGIBLE, RouteState.BREAKER_OPEN, RouteState.RETIRED}
    ),
    RouteState.BREAKER_OPEN: frozenset(
        {RouteState.COOLDOWN, RouteState.ELIGIBLE, RouteState.RETIRED}
    ),
}
_CREDENTIAL_TRANSITIONS = {
    CredentialState.AVAILABLE: frozenset(
        {CredentialState.EXHAUSTED, CredentialState.COOLDOWN, CredentialState.DEAD}
    ),
    CredentialState.EXHAUSTED: frozenset(
        {CredentialState.AVAILABLE, CredentialState.COOLDOWN, CredentialState.DEAD}
    ),
    CredentialState.COOLDOWN: frozenset(
        {CredentialState.AVAILABLE, CredentialState.EXHAUSTED, CredentialState.DEAD}
    ),
}
_RESERVATION_TRANSITIONS = {
    ReservationState.PENDING: frozenset(
        {ReservationState.HELD, ReservationState.RELEASED, ReservationState.EXPIRED}
    ),
    ReservationState.HELD: frozenset(
        {
            ReservationState.CONSUMED,
            ReservationState.RELEASED,
            ReservationState.EXPIRED,
            ReservationState.RECONCILED,
        }
    ),
    ReservationState.CONSUMED: frozenset({ReservationState.RECONCILED}),
    ReservationState.RELEASED: frozenset({ReservationState.RECONCILED}),
    ReservationState.EXPIRED: frozenset({ReservationState.RECONCILED}),
}
_REVIEW_TRANSITIONS = {
    ReviewState.PENDING: frozenset(
        {ReviewState.IN_PROGRESS, ReviewState.HUMAN_APPROVED, ReviewState.HUMAN_REJECTED}
    ),
    ReviewState.IN_PROGRESS: frozenset(
        {
            ReviewState.PASSED,
            ReviewState.FAILED,
            ReviewState.HUMAN_APPROVED,
            ReviewState.HUMAN_REJECTED,
        }
    ),
}


def _validated_transition(
    event_type: Callable[..., E],
    allowed: Mapping[S, frozenset[S]],
    *,
    from_state: S,
    to_state: S,
    **audit_metadata: str,
) -> E:
    event = event_type(from_state=from_state, to_state=to_state, **audit_metadata)
    _ensure_legal_transition(from_state, to_state, allowed)
    return event


def validate_task_transition(
    *, from_state: TaskState, to_state: TaskState, **audit_metadata: str
) -> TaskStateEvent:
    return _validated_transition(
        TaskStateEvent,
        _TASK_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        **audit_metadata,
    )


def validate_attempt_transition(
    *, from_state: AttemptState, to_state: AttemptState, **audit_metadata: str
) -> AttemptStateEvent:
    return _validated_transition(
        AttemptStateEvent,
        _ATTEMPT_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        **audit_metadata,
    )


def validate_route_transition(
    *, from_state: RouteState, to_state: RouteState, **audit_metadata: str
) -> RouteStateEvent:
    return _validated_transition(
        RouteStateEvent,
        _ROUTE_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        **audit_metadata,
    )


def validate_credential_transition(
    *, from_state: CredentialState, to_state: CredentialState, **audit_metadata: str
) -> CredentialStateEvent:
    return _validated_transition(
        CredentialStateEvent,
        _CREDENTIAL_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        **audit_metadata,
    )


def validate_reservation_transition(
    *, from_state: ReservationState, to_state: ReservationState, **audit_metadata: str
) -> ReservationStateEvent:
    return _validated_transition(
        ReservationStateEvent,
        _RESERVATION_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        **audit_metadata,
    )


def validate_review_transition(
    *, from_state: ReviewState, to_state: ReviewState, **audit_metadata: str
) -> ReviewStateEvent:
    return _validated_transition(
        ReviewStateEvent,
        _REVIEW_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        **audit_metadata,
    )


def _reject_sensitive(value: object, location: str) -> None:
    def walk(item: object, path: tuple[str, ...] = ()) -> None:
        key = path[-1].casefold() if path else ""
        if key in _PROHIBITED_FIELD_NAMES:
            raise DomainValidationError(
                "decision.sensitive_field_prohibited",
                f"sensitive field prohibited in {location}",
            )
        if is_dataclass(item) and not isinstance(item, type):
            for dataclass_field in fields(item):
                walk(
                    getattr(item, dataclass_field.name),
                    path + (dataclass_field.name,),
                )
        elif isinstance(item, Mapping):
            for child_key, child_value in item.items():
                walk(child_value, path + (str(child_key),))
        elif isinstance(item, (list, tuple, set, frozenset)):
            for child in item:
                walk(child, path)
        elif isinstance(item, (str, bytes, bytearray)):
            text = (
                item
                if isinstance(item, str)
                else bytes(item).decode("utf-8", errors="replace")
            )
            if _SENSITIVE_VALUE_PATTERN.search(text):
                raise DomainValidationError(
                    "decision.sensitive_field_prohibited",
                    f"sensitive value prohibited in {location}",
                )

    walk(value)
