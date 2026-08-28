"""Canonical webhook route, provider, identity, and intake-envelope authority.

HTTP owns byte acquisition and response codes.  This module owns the domain
facts handed across that boundary: the provider/verifier binding, provider-
native retry identity, event type, body digest, and request trace. Provider
authority must be declared in configuration; request headers never select it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit


class WebhookContractError(ValueError):
    """Webhook configuration cannot be normalized without ambiguity."""


class WebhookRouteScopeError(WebhookContractError):
    """A route is not bound to the request-selected profile."""


class WebhookPayloadContractError(WebhookContractError):
    """Authenticated request payload metadata violates the wire contract."""


MAX_EVENT_TYPE_UTF8_BYTES = 1024
MAX_ROUTE_NAME_BYTES = 128
MAX_AUTHENTICATED_JSON_NESTING = 128
MAX_CUSTOM_SIGNATURE_HEADER_BYTES = 16_384
MAX_CUSTOM_SIGNATURE_TEMPLATE_BYTES = 1024
MAX_CUSTOM_SIGNATURE_TOLERANCE_SECONDS = 86_400
_ROUTE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HTTP_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SIGNATURE_TEMPLATE_FIELD_RE = re.compile(r"\{([^{}]*)\}")

_CUSTOM_SIGNATURE_KEYS = frozenset({
    "header",
    "signature_part",
    "signature_prefix",
    "timestamp_part",
    "timestamp_header",
    "template",
    "algorithm",
    "encoding",
    "timestamp_unit",
    "tolerance_seconds",
})
_CUSTOM_SIGNATURE_ALGORITHMS = MappingProxyType({
    "sha1": "sha1",
    "hmac_sha1": "sha1",
    "sha256": "sha256",
    "hmac_sha256": "sha256",
    "sha512": "sha512",
    "hmac_sha512": "sha512",
})
_CUSTOM_SIGNATURE_ENCODINGS = frozenset({"hex", "base64"})
_CUSTOM_SIGNATURE_TIMESTAMP_UNITS = MappingProxyType({
    "s": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "ms": "milliseconds",
    "millisecond": "milliseconds",
    "milliseconds": "milliseconds",
})


_GITHUB_PULL_REQUEST_ACTIONS = frozenset({
    "assigned",
    "auto_merge_disabled",
    "auto_merge_enabled",
    "closed",
    "converted_to_draft",
    "demilestoned",
    "dequeued",
    "edited",
    "enqueued",
    "labeled",
    "locked",
    "milestoned",
    "opened",
    "ready_for_review",
    "reopened",
    "review_request_removed",
    "review_requested",
    "synchronize",
    "unassigned",
    "unlabeled",
    "unlocked",
})
_GITHUB_ISSUES_ACTIONS = frozenset({
    "assigned",
    "closed",
    "deleted",
    "demilestoned",
    "edited",
    "labeled",
    "locked",
    "milestoned",
    "opened",
    "pinned",
    "reopened",
    "transferred",
    "typed",
    "unassigned",
    "unlabeled",
    "unlocked",
    "unpinned",
    "untyped",
})
_GITHUB_CHECK_RUN_ACTIONS = frozenset({
    "completed",
    "created",
    "rerequested",
    "requested_action",
})


def _is_json_object(value: Any) -> bool:
    return isinstance(value, Mapping)


def _is_json_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_json_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _github_ping_body(payload: Mapping[str, Any]) -> bool:
    return (
        _is_nonempty_json_string(payload.get("zen"))
        and _is_json_integer(payload.get("hook_id"))
        and _is_json_object(payload.get("hook"))
    )


def _github_pull_request_body(payload: Mapping[str, Any]) -> bool:
    pull_request = payload.get("pull_request")
    number = payload.get("number")
    return (
        payload.get("action") in _GITHUB_PULL_REQUEST_ACTIONS
        and _is_json_integer(number)
        and _is_json_object(pull_request)
        and _is_json_integer(pull_request.get("number"))
        and pull_request.get("number") == number
        and _is_json_object(payload.get("repository"))
        and _is_json_object(payload.get("sender"))
        # Neighboring GitHub events include the same complete pull-request
        # object. Their event-specific object is the conservative discriminator.
        and not any(key in payload for key in ("comment", "review", "thread"))
    )


def _github_push_body(payload: Mapping[str, Any]) -> bool:
    return (
        all(
            _is_nonempty_json_string(payload.get(key))
            for key in ("ref", "before", "after")
        )
        and all(
            isinstance(payload.get(key), bool)
            for key in ("created", "deleted", "forced")
        )
        and isinstance(payload.get("commits"), list)
        and _is_json_object(payload.get("repository"))
        and _is_json_object(payload.get("pusher"))
        and _is_json_object(payload.get("sender"))
    )


def _github_issues_body(payload: Mapping[str, Any]) -> bool:
    issue = payload.get("issue")
    return (
        payload.get("action") in _GITHUB_ISSUES_ACTIONS
        and _is_json_object(issue)
        and _is_json_integer(issue.get("number"))
        and _is_json_object(payload.get("repository"))
        and _is_json_object(payload.get("sender"))
        # Neighboring issue_comment, sub_issues, and issue_dependencies events
        # carry an ``issue`` too. Their event-specific object is authoritative.
        and not any(
            key in payload
            for key in (
                "blocked_by_issue",
                "blocked_issue",
                "blocking_issue",
                "comment",
                "dependency",
                "parent_issue",
                "sub_issue",
            )
        )
    )


def _github_check_run_body(payload: Mapping[str, Any]) -> bool:
    check_run = payload.get("check_run")
    return (
        payload.get("action") in _GITHUB_CHECK_RUN_ACTIONS
        and _is_json_object(check_run)
        and _is_json_integer(check_run.get("id"))
        and _is_nonempty_json_string(check_run.get("name"))
        and _is_nonempty_json_string(check_run.get("status"))
        and _is_json_object(payload.get("repository"))
        and _is_json_object(payload.get("sender"))
        # deployment_status may include a complete check_run object, while
        # check_suite has its own neighboring object and overlapping actions.
        and not any(
            key in payload for key in ("check_suite", "deployment", "deployment_status")
        )
    )


_GITHUB_EVENT_BODY_CLASSIFIERS: Mapping[str, Callable[[Mapping[str, Any]], bool]] = (
    MappingProxyType({
        "check_run": _github_check_run_body,
        "issues": _github_issues_body,
        "ping": _github_ping_body,
        "pull_request": _github_pull_request_body,
        "push": _github_push_body,
    })
)
GITHUB_AUTHENTICATED_EVENTS = frozenset(_GITHUB_EVENT_BODY_CLASSIFIERS)


@dataclass(frozen=True, slots=True)
class WebhookCustomSignatureSpec:
    """One immutable, route-selected HMAC wire contract.

    The request path never parses mutable route dictionaries. It receives this
    normalized authority, reads exactly the declared credential fields, and
    either verifies that one scheme or rejects the request.
    """

    header: str
    signature_part: Optional[str]
    signature_prefix: str
    timestamp_part: Optional[str]
    timestamp_header: Optional[str]
    template: str
    algorithm: str
    encoding: str
    timestamp_unit: Optional[str]
    tolerance_seconds: Optional[int]

    @property
    def uses_timestamp(self) -> bool:
        return self.timestamp_part is not None or self.timestamp_header is not None

    @classmethod
    def bind(
        cls,
        route_name: str,
        raw: Any,
    ) -> "WebhookCustomSignatureSpec":
        prefix = f"route {route_name!r} custom signature"
        if not isinstance(raw, Mapping):
            raise WebhookContractError(f"{prefix} must be an object")
        unknown = [
            key
            for key in raw
            if not isinstance(key, str) or key not in _CUSTOM_SIGNATURE_KEYS
        ]
        if unknown:
            raise WebhookContractError(f"{prefix} has unsupported field {unknown[0]!r}")

        def text_field(
            key: str,
            *,
            required: bool = False,
            default: Optional[str] = None,
            canonical_spacing: bool = True,
        ) -> Optional[str]:
            value = raw.get(key) if key in raw else default
            if value is None:
                if required:
                    raise WebhookContractError(f"{prefix} requires non-empty {key!r}")
                return None
            if not isinstance(value, str):
                raise WebhookContractError(f"{prefix} field {key!r} must be text")
            if canonical_spacing and value != value.strip():
                raise WebhookContractError(
                    f"{prefix} field {key!r} must not have surrounding whitespace"
                )
            if required and not value:
                raise WebhookContractError(f"{prefix} requires non-empty {key!r}")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise WebhookContractError(
                    f"{prefix} field {key!r} is not valid Unicode"
                ) from exc
            return value

        header = text_field("header", required=True)
        assert header is not None
        if len(header) > 128 or _HTTP_TOKEN_RE.fullmatch(header) is None:
            raise WebhookContractError(
                f"{prefix} field 'header' must be an HTTP field-name token"
            )
        header = header.lower()

        signature_part = text_field("signature_part")
        timestamp_part = text_field("timestamp_part")
        for key, value in (
            ("signature_part", signature_part),
            ("timestamp_part", timestamp_part),
        ):
            if value is not None and (
                not value or len(value) > 128 or _HTTP_TOKEN_RE.fullmatch(value) is None
            ):
                raise WebhookContractError(
                    f"{prefix} field {key!r} must be a non-empty header-part token"
                )
        if signature_part is not None and signature_part == timestamp_part:
            raise WebhookContractError(
                f"{prefix} signature_part and timestamp_part must differ"
            )

        timestamp_header = text_field("timestamp_header")
        if timestamp_header is not None:
            if (
                not timestamp_header
                or len(timestamp_header) > 128
                or _HTTP_TOKEN_RE.fullmatch(timestamp_header) is None
            ):
                raise WebhookContractError(
                    f"{prefix} field 'timestamp_header' must be an HTTP "
                    "field-name token"
                )
            timestamp_header = timestamp_header.lower()
        if timestamp_part is not None and timestamp_header is not None:
            raise WebhookContractError(
                f"{prefix} must choose timestamp_part or timestamp_header, not both"
            )
        if timestamp_header == header:
            raise WebhookContractError(
                f"{prefix} must use timestamp_part when both values share a header"
            )

        signature_prefix = text_field("signature_prefix", default="")
        assert signature_prefix is not None
        signature_prefix_bytes = signature_prefix.encode("utf-8")
        if len(signature_prefix_bytes) > 256 or any(
            byte < 0x20 or byte == 0x7F for byte in signature_prefix_bytes
        ):
            raise WebhookContractError(
                f"{prefix} field 'signature_prefix' contains invalid header text"
            )

        template = text_field(
            "template",
            required=True,
            default="{timestamp}.{body}",
            canonical_spacing=False,
        )
        assert template is not None
        template_bytes = template.encode("utf-8")
        if len(template_bytes) > MAX_CUSTOM_SIGNATURE_TEMPLATE_BYTES:
            raise WebhookContractError(
                f"{prefix} template exceeds "
                f"{MAX_CUSTOM_SIGNATURE_TEMPLATE_BYTES} UTF-8 bytes"
            )
        if template.count("{body}") != 1:
            raise WebhookContractError(
                f"{prefix} template must contain exactly one '{{body}}' marker"
            )
        fields = _SIGNATURE_TEMPLATE_FIELD_RE.findall(template)
        unknown_fields = sorted(set(fields) - {"body", "timestamp"})
        if unknown_fields:
            raise WebhookContractError(
                f"{prefix} template has unsupported marker {{{unknown_fields[0]}}}"
            )
        literal = template.replace("{body}", "").replace("{timestamp}", "")
        if "{" in literal or "}" in literal:
            raise WebhookContractError(f"{prefix} template contains an unmatched brace")
        uses_timestamp = "timestamp" in fields
        has_timestamp_source = (
            timestamp_part is not None or timestamp_header is not None
        )
        if uses_timestamp != has_timestamp_source:
            if uses_timestamp:
                raise WebhookContractError(
                    f"{prefix} template uses '{{timestamp}}' without a timestamp source"
                )
            raise WebhookContractError(
                f"{prefix} declares a timestamp source not covered by the template"
            )

        algorithm_raw = text_field("algorithm", default="sha256")
        assert algorithm_raw is not None
        algorithm = _CUSTOM_SIGNATURE_ALGORITHMS.get(_normalize_token(algorithm_raw))
        if algorithm is None:
            raise WebhookContractError(
                f"{prefix} algorithm must be sha1, sha256, or sha512"
            )

        encoding_raw = text_field("encoding", default="hex")
        assert encoding_raw is not None
        encoding = _normalize_token(encoding_raw)
        if encoding not in _CUSTOM_SIGNATURE_ENCODINGS:
            raise WebhookContractError(f"{prefix} encoding must be 'hex' or 'base64'")

        timestamp_unit: Optional[str] = None
        tolerance_seconds: Optional[int] = None
        if uses_timestamp:
            unit_raw = text_field("timestamp_unit", default="seconds")
            assert unit_raw is not None
            timestamp_unit = _CUSTOM_SIGNATURE_TIMESTAMP_UNITS.get(
                _normalize_token(unit_raw)
            )
            if timestamp_unit is None:
                raise WebhookContractError(
                    f"{prefix} timestamp_unit must be seconds or milliseconds"
                )
            tolerance = raw.get("tolerance_seconds", 300)
            if isinstance(tolerance, bool) or not isinstance(tolerance, int):
                raise WebhookContractError(
                    f"{prefix} tolerance_seconds must be an integer"
                )
            if not 1 <= tolerance <= MAX_CUSTOM_SIGNATURE_TOLERANCE_SECONDS:
                raise WebhookContractError(
                    f"{prefix} tolerance_seconds must be between 1 and "
                    f"{MAX_CUSTOM_SIGNATURE_TOLERANCE_SECONDS}"
                )
            tolerance_seconds = tolerance
        elif "timestamp_unit" in raw or "tolerance_seconds" in raw:
            raise WebhookContractError(
                f"{prefix} timestamp policy requires '{{timestamp}}' in the template"
            )

        return cls(
            header=header,
            signature_part=signature_part,
            signature_prefix=signature_prefix,
            timestamp_part=timestamp_part,
            timestamp_header=timestamp_header,
            template=template,
            algorithm=algorithm,
            encoding=encoding,
            timestamp_unit=timestamp_unit,
            tolerance_seconds=tolerance_seconds,
        )


@dataclass(frozen=True)
class WebhookProviderSpec:
    """Wire facts owned by one webhook provider namespace."""

    name: str
    aliases: tuple[str, ...] = ()
    delivery_id_headers: tuple[str, ...] = ()
    authenticated_delivery_id_headers: tuple[str, ...] = ()
    event_headers: tuple[str, ...] = ()
    route_bound_event_authority: bool = False
    legacy_detection_headers: tuple[str, ...] = ()
    payload_delivery_id_keys: tuple[str, ...] = ()
    payload_event_keys: tuple[str, ...] = ()
    authenticated_delivery_id_payload_keys: tuple[str, ...] = ()
    authenticated_event_payload_keys: tuple[str, ...] = ()
    authenticated_timestamp_payload_keys: tuple[str, ...] = ()
    payload_replay_tolerance_seconds: Optional[int] = None
    signature_modes: tuple[str, ...] = ()
    default_signature_mode: str = ""


_PROVIDER_SPECS = (
    WebhookProviderSpec(
        name="svix",
        aliases=("agentmail",),
        delivery_id_headers=("svix-id",),
        authenticated_delivery_id_headers=("svix-id",),
        legacy_detection_headers=("svix-id", "svix-signature", "svix-timestamp"),
        signature_modes=("svix",),
        default_signature_mode="svix",
    ),
    WebhookProviderSpec(
        name="github",
        aliases=("github_hmac_sha256",),
        delivery_id_headers=("X-GitHub-Delivery",),
        event_headers=("X-GitHub-Event",),
        route_bound_event_authority=True,
        legacy_detection_headers=(
            "X-Hub-Signature-256",
            "X-GitHub-Delivery",
            "X-GitHub-Event",
        ),
        signature_modes=("github",),
        default_signature_mode="github",
    ),
    WebhookProviderSpec(
        name="gitlab",
        aliases=("gitlab_token",),
        delivery_id_headers=(
            "X-Gitlab-Event-UUID",
            "X-Gitlab-Webhook-UUID",
            "X-Gitlab-Idempotency-Key",
            "Idempotency-Key",
        ),
        event_headers=("X-GitLab-Event",),
        route_bound_event_authority=True,
        legacy_detection_headers=(
            "X-Gitlab-Token",
            "X-Gitlab-Event-UUID",
            "X-Gitlab-Webhook-UUID",
            "X-Gitlab-Idempotency-Key",
            "X-GitLab-Event",
        ),
        payload_event_keys=("object_kind", "event_name"),
        signature_modes=("gitlab",),
        default_signature_mode="gitlab",
    ),
    WebhookProviderSpec(
        name="standard_webhooks",
        aliases=("gitlab_standard",),
        delivery_id_headers=("webhook-id", "Idempotency-Key"),
        authenticated_delivery_id_headers=("webhook-id",),
        legacy_detection_headers=("webhook-id", "webhook-signature"),
        signature_modes=("standard_webhooks",),
        default_signature_mode="standard_webhooks",
    ),
    WebhookProviderSpec(
        name="chatwoot",
        delivery_id_headers=("X-Chatwoot-Delivery",),
        legacy_detection_headers=("X-Chatwoot-Delivery",),
        payload_event_keys=("event",),
        signature_modes=("generic_v1", "generic_v2"),
    ),
    WebhookProviderSpec(
        name="linear",
        delivery_id_headers=("Linear-Delivery", "Linear-Delivery-ID"),
        event_headers=("Linear-Event",),
        legacy_detection_headers=("linear-signature",),
        signature_modes=("linear",),
        default_signature_mode="linear",
    ),
    WebhookProviderSpec(
        name="sentry",
        delivery_id_headers=("sentry-hook-request-id",),
        legacy_detection_headers=("sentry-hook-signature",),
        payload_delivery_id_keys=("id",),
        signature_modes=("sentry",),
        default_signature_mode="sentry",
    ),
    WebhookProviderSpec(
        name="pocket",
        aliases=("heypocket", "heypocketai"),
        legacy_detection_headers=(
            "X-HeyPocket-Signature",
            "X-HeyPocket-Timestamp",
        ),
        payload_delivery_id_keys=("id", "event_id"),
        signature_modes=("pocket",),
        default_signature_mode="pocket",
    ),
    WebhookProviderSpec(
        name="todoist",
        legacy_detection_headers=("X-Todoist-Hmac-SHA256",),
        payload_delivery_id_keys=("event_id", "id"),
        signature_modes=("todoist",),
        default_signature_mode="todoist",
    ),
    WebhookProviderSpec(
        name="juniper_mist",
        aliases=("mist",),
        legacy_detection_headers=("X-Mist-Signature-v2",),
        payload_delivery_id_keys=("id",),
        signature_modes=("juniper_mist",),
        default_signature_mode="juniper_mist",
    ),
    WebhookProviderSpec(
        name="fireflies",
        legacy_detection_headers=("X-Hub-Signature",),
        payload_event_keys=("event", "eventType"),
        signature_modes=("fireflies",),
        default_signature_mode="fireflies",
    ),
    WebhookProviderSpec(
        name="redmine",
        legacy_detection_headers=("X-Redmine-Signature-256",),
        payload_delivery_id_keys=("id",),
        signature_modes=("redmine",),
        default_signature_mode="redmine",
    ),
    WebhookProviderSpec(
        name="gitea",
        delivery_id_headers=("X-Gitea-Delivery",),
        event_headers=("X-Gitea-Event",),
        legacy_detection_headers=("X-Gitea-Signature",),
        payload_delivery_id_keys=("id",),
        signature_modes=("gitea",),
        default_signature_mode="gitea",
    ),
    WebhookProviderSpec(
        name="forgejo",
        delivery_id_headers=("X-Forgejo-Delivery",),
        event_headers=("X-Forgejo-Event",),
        legacy_detection_headers=("X-Forgejo-Signature",),
        payload_delivery_id_keys=("id",),
        signature_modes=("forgejo",),
        default_signature_mode="forgejo",
    ),
    WebhookProviderSpec(
        name="asana",
        legacy_detection_headers=("X-Hook-Signature",),
        payload_delivery_id_keys=("id",),
        signature_modes=("asana",),
        default_signature_mode="asana",
    ),
    WebhookProviderSpec(
        name="notion",
        legacy_detection_headers=("X-Notion-Signature",),
        signature_modes=("notion",),
        default_signature_mode="notion",
    ),
    WebhookProviderSpec(
        name="exit1",
        legacy_detection_headers=("X-Exit1-Signature",),
        signature_modes=("exit1",),
        default_signature_mode="exit1",
    ),
    WebhookProviderSpec(
        name="jira",
        aliases=("atlassian",),
        legacy_detection_headers=("X-Hub-Signature",),
        payload_event_keys=("webhookEvent",),
        signature_modes=("jira",),
        default_signature_mode="jira",
    ),
    WebhookProviderSpec(
        name="attio",
        legacy_detection_headers=("Attio-Signature", "X-Attio-Signature"),
        payload_delivery_id_keys=("id",),
        signature_modes=("attio", "attio_x"),
        default_signature_mode="attio",
    ),
    WebhookProviderSpec(
        name="plain_token",
        aliases=("shared_secret",),
        legacy_detection_headers=("X-Webhook-Secret",),
        signature_modes=("plain_token",),
        default_signature_mode="plain_token",
    ),
    WebhookProviderSpec(
        name="bearer_token",
        aliases=("bearer",),
        legacy_detection_headers=("Authorization",),
        signature_modes=("bearer_token",),
        default_signature_mode="bearer_token",
    ),
    WebhookProviderSpec(
        name="trello",
        legacy_detection_headers=("X-Trello-Webhook",),
        payload_delivery_id_keys=("action.id",),
        payload_event_keys=("action.type",),
        signature_modes=("trello",),
        default_signature_mode="trello",
    ),
    WebhookProviderSpec(
        name="hindsight",
        aliases=("hindsight_hmac_sha256",),
        legacy_detection_headers=("X-Hindsight-Signature",),
        signature_modes=("hindsight",),
        default_signature_mode="hindsight",
    ),
    WebhookProviderSpec(
        name="hermes",
        aliases=("hermes_agent",),
        delivery_id_headers=("X-Hermes-Delivery",),
        event_headers=("X-Hermes-Event",),
        legacy_detection_headers=(
            "X-Hermes-Signature-256",
            "X-Hermes-Delivery",
            "X-Hermes-Event",
        ),
        authenticated_delivery_id_payload_keys=("delivery_id",),
        authenticated_event_payload_keys=("hook_event_name",),
        authenticated_timestamp_payload_keys=("timestamp",),
        payload_replay_tolerance_seconds=300,
        signature_modes=("hermes",),
        default_signature_mode="hermes",
    ),
    WebhookProviderSpec(
        name="stripe",
        aliases=("stripe_signature",),
        legacy_detection_headers=("Stripe-Signature",),
        payload_delivery_id_keys=("id",),
        signature_modes=("stripe",),
        default_signature_mode="stripe",
    ),
    WebhookProviderSpec(
        name="custom",
        aliases=("custom_hmac",),
        signature_modes=("custom_hmac",),
        default_signature_mode="custom_hmac",
    ),
    WebhookProviderSpec(
        name="generic",
        aliases=("generic_v1", "generic_v2"),
        legacy_detection_headers=(
            "X-Webhook-Signature-V2",
            "X-Webhook-Signature",
        ),
        signature_modes=("generic_v1", "generic_v2"),
        default_signature_mode="generic_v2",
    ),
)

PROVIDER_REGISTRY: Mapping[str, WebhookProviderSpec] = MappingProxyType({
    spec.name: spec for spec in _PROVIDER_SPECS
})
_PROVIDER_ALIASES = MappingProxyType({
    alias: spec.name for spec in _PROVIDER_SPECS for alias in (spec.name, *spec.aliases)
})

# Configuration aliases normalize to the executable verifier names.  The
# verifier receives only these canonical values; it never interprets aliases.
_SIGNATURE_MODE_ALIASES = MappingProxyType({
    "agentmail": "svix",
    "svix": "svix",
    "github": "github",
    "github_hmac_sha256": "github",
    "gitlab": "gitlab",
    "gitlab_token": "gitlab",
    "standard_webhooks": "standard_webhooks",
    "gitlab_standard": "standard_webhooks",
    "linear": "linear",
    "sentry": "sentry",
    "pocket": "pocket",
    "heypocket": "pocket",
    "heypocketai": "pocket",
    "todoist": "todoist",
    "juniper_mist": "juniper_mist",
    "mist": "juniper_mist",
    "fireflies": "fireflies",
    "redmine": "redmine",
    "gitea": "gitea",
    "forgejo": "forgejo",
    "asana": "asana",
    "notion": "notion",
    "exit1": "exit1",
    "jira": "jira",
    "atlassian": "jira",
    "attio": "attio",
    "attio_x": "attio_x",
    "plain_token": "plain_token",
    "shared_secret": "plain_token",
    "bearer": "bearer_token",
    "bearer_token": "bearer_token",
    "trello": "trello",
    "hindsight": "hindsight",
    "hindsight_hmac_sha256": "hindsight",
    "hermes": "hermes",
    "hermes_agent": "hermes",
    "stripe": "stripe",
    "stripe_signature": "stripe",
    "custom": "custom_hmac",
    "custom_hmac": "custom_hmac",
    "generic": "generic_v2",
    "generic_v1": "generic_v1",
    "generic_v2": "generic_v2",
})
_SIGNATURE_MODE_TO_PROVIDER = MappingProxyType({
    "svix": "svix",
    "github": "github",
    "gitlab": "gitlab",
    "standard_webhooks": "standard_webhooks",
    "linear": "linear",
    "sentry": "sentry",
    "pocket": "pocket",
    "todoist": "todoist",
    "juniper_mist": "juniper_mist",
    "fireflies": "fireflies",
    "redmine": "redmine",
    "gitea": "gitea",
    "forgejo": "forgejo",
    "asana": "asana",
    "notion": "notion",
    "exit1": "exit1",
    "jira": "jira",
    "attio": "attio",
    "attio_x": "attio",
    "plain_token": "plain_token",
    "bearer_token": "bearer_token",
    "trello": "trello",
    "hindsight": "hindsight",
    "hermes": "hermes",
    "stripe": "stripe",
    "custom_hmac": "custom",
    "generic_v1": "generic",
    "generic_v2": "generic",
})


def _normalize_token(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _nonempty_scalar(value: Any) -> Optional[str]:
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (str, int)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _payload_value(payload: Mapping[str, Any], path: str) -> Any:
    """Resolve a bounded dotted provider field without list/index semantics."""

    current: Any = payload
    parts = path.split(".")
    if not parts or len(parts) > 8:
        return None
    for part in parts:
        if not part or not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _header(headers: Mapping[str, Any], name: str) -> str:
    """Read a case-insensitive HTTP header from real or test mappings."""

    direct = headers.get(name)
    if direct not in (None, ""):
        return str(direct)
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target and value not in (None, ""):
            return str(value)
    return ""


def canonical_provider(value: str) -> str:
    """Return the canonical provider namespace or fail closed."""

    normalized = _normalize_token(value)
    if not normalized:
        raise WebhookContractError("webhook provider must be non-empty")
    provider = _PROVIDER_ALIASES.get(normalized)
    if provider is None:
        raise WebhookContractError(f"unsupported webhook provider {value!r}")
    return provider


def canonical_signature_mode(value: str) -> str:
    """Return one executable verifier mode or fail closed."""

    normalized = _normalize_token(value)
    if not normalized:
        raise WebhookContractError("webhook signature mode must be non-empty")
    mode = _SIGNATURE_MODE_ALIASES.get(normalized)
    if mode is None:
        raise WebhookContractError(f"unsupported webhook signature mode {value!r}")
    return mode


@dataclass(frozen=True)
class WebhookRouteConfig:
    """Normalized route identity/security binding consumed by intake."""

    name: str
    profile: str
    provider: str
    provider_declared: bool
    signature_mode: str
    enabled: bool
    events: tuple[str, ...]
    signature_context: Optional[str]
    custom_signature: Optional[WebhookCustomSignatureSpec]

    @classmethod
    def bind(
        cls,
        name: str,
        route: Mapping[str, Any],
        *,
        headers: Mapping[str, Any],
        request_profile: Optional[str] = None,
    ) -> "WebhookRouteConfig":
        if not isinstance(route, Mapping):
            raise WebhookContractError("webhook route config must be an object")
        if not isinstance(name, str) or not _ROUTE_NAME_RE.fullmatch(name):
            raise WebhookContractError(
                "webhook route name must already be a canonical lowercase "
                f"URL slug of at most {MAX_ROUTE_NAME_BYTES} ASCII bytes"
            )
        route_name = name

        if "profile" in route:
            profile_value = route.get("profile")
            if not isinstance(profile_value, str) or not _PROFILE_NAME_RE.fullmatch(
                profile_value
            ):
                raise WebhookContractError(
                    f"route {route_name!r} has a non-canonical profile binding"
                )
            configured_profile = profile_value
        else:
            configured_profile = "default"

        effective_profile = request_profile or "default"
        if not isinstance(effective_profile, str) or not _PROFILE_NAME_RE.fullmatch(
            effective_profile
        ):
            raise WebhookRouteScopeError("request profile is not canonical")
        if configured_profile != effective_profile:
            raise WebhookRouteScopeError(
                f"route {route_name!r} is not bound to profile {effective_profile!r}"
            )

        provider_raw = route.get("provider")
        if provider_raw is not None and (
            not isinstance(provider_raw, str) or not provider_raw.strip()
        ):
            raise WebhookContractError(f"route {route_name!r} has malformed provider")
        signature_raw = route.get("signature_mode")
        if signature_raw is not None and (
            not isinstance(signature_raw, str) or not signature_raw.strip()
        ):
            raise WebhookContractError(
                f"route {route_name!r} has malformed signature_mode"
            )

        provider_declared = bool(provider_raw or signature_raw)
        if not provider_declared:
            raise WebhookContractError(
                f"route {route_name!r} requires an explicit provider or signature_mode"
            )
        if provider_raw:
            provider = canonical_provider(provider_raw)
        elif signature_raw:
            canonical_mode = canonical_signature_mode(signature_raw)
            provider = _SIGNATURE_MODE_TO_PROVIDER[canonical_mode]
        else:  # pragma: no cover - guarded by provider_declared above
            raise WebhookContractError("unreachable provider binding")

        spec = PROVIDER_REGISTRY[provider]
        if signature_raw:
            signature_mode = canonical_signature_mode(signature_raw)
            if signature_mode not in spec.signature_modes:
                raise WebhookContractError(
                    f"route {route_name!r} provider {provider!r} does not allow "
                    f"signature mode {signature_mode!r}"
                )
        else:
            # Some legacy configs put a verifier alias in ``provider`` (most
            # importantly ``generic_v1``). Preserve that declared strength
            # instead of silently replacing it with the provider default.
            provider_mode = (
                _SIGNATURE_MODE_ALIASES.get(_normalize_token(provider_raw))
                if provider_raw
                else None
            )
            if provider_mode in spec.signature_modes:
                signature_mode = provider_mode
            elif spec.default_signature_mode:
                signature_mode = spec.default_signature_mode
            else:
                raise WebhookContractError(
                    f"route {route_name!r} provider {provider!r} requires an "
                    "explicit signature_mode"
                )

        custom_signature: Optional[WebhookCustomSignatureSpec] = None
        if signature_mode == "custom_hmac":
            if "signature" not in route:
                raise WebhookContractError(
                    f"route {route_name!r} custom_hmac requires a 'signature' object"
                )
            custom_signature = WebhookCustomSignatureSpec.bind(
                route_name,
                route.get("signature"),
            )
        elif "signature" in route:
            raise WebhookContractError(
                f"route {route_name!r} may declare 'signature' only with "
                "signature_mode 'custom_hmac'"
            )

        enabled_raw = route.get("enabled", True)
        if not isinstance(enabled_raw, bool):
            raise WebhookContractError(
                f"route {route_name!r} enabled must be a boolean"
            )

        if "events" not in route:
            events: tuple[str, ...] = ()
        else:
            events_raw = route.get("events")
            if not isinstance(events_raw, (list, tuple)):
                raise WebhookContractError(
                    f"route {route_name!r} events must be a sequence"
                )
            normalized_events: list[str] = []
            for item in events_raw:
                if not isinstance(item, str) or not item.strip():
                    raise WebhookContractError(
                        f"route {route_name!r} events must contain non-empty strings"
                    )
                value = item.strip()
                try:
                    encoded_event = value.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise WebhookContractError(
                        f"route {route_name!r} event is not valid Unicode"
                    ) from exc
                if len(encoded_event) > MAX_EVENT_TYPE_UTF8_BYTES:
                    raise WebhookContractError(
                        f"route {route_name!r} event exceeds "
                        f"{MAX_EVENT_TYPE_UTF8_BYTES} UTF-8 bytes"
                    )
                if value not in normalized_events:
                    normalized_events.append(value)
            events = tuple(normalized_events)
        if spec.route_bound_event_authority and len(events) > 1:
            raise WebhookContractError(
                f"route {route_name!r} provider {provider!r} uses an unsigned "
                "event header and therefore permits at most one route-bound event"
            )
        if provider == "github" and events:
            unsupported = [
                event for event in events if event not in GITHUB_AUTHENTICATED_EVENTS
            ]
            if unsupported:
                supported = ", ".join(sorted(GITHUB_AUTHENTICATED_EVENTS))
                raise WebhookContractError(
                    f"route {route_name!r} provider 'github' cannot authenticate "
                    f"event body shape for {unsupported[0]!r}; supported events: "
                    f"{supported}"
                )

        signature_context: Optional[str] = None
        callback_url = route.get("callback_url")
        if provider == "trello":
            if not isinstance(callback_url, str) or not callback_url.strip():
                raise WebhookContractError(
                    f"route {route_name!r} provider 'trello' requires callback_url"
                )
            callback_url = callback_url.strip()
            try:
                parsed_callback = urlsplit(callback_url)
            except ValueError as exc:
                raise WebhookContractError(
                    f"route {route_name!r} has malformed callback_url"
                ) from exc
            if (
                parsed_callback.scheme not in {"http", "https"}
                or not parsed_callback.hostname
                or parsed_callback.username is not None
                or parsed_callback.password is not None
                or parsed_callback.fragment
            ):
                raise WebhookContractError(
                    f"route {route_name!r} has invalid Trello callback_url"
                )
            signature_context = callback_url
        elif callback_url is not None:
            raise WebhookContractError(
                f"route {route_name!r} callback_url is valid only for Trello"
            )

        return cls(
            name=route_name,
            profile=configured_profile,
            provider=provider,
            provider_declared=provider_declared,
            signature_mode=signature_mode,
            enabled=enabled_raw,
            events=events,
            signature_context=signature_context,
            custom_signature=custom_signature,
        )

    @property
    def provider_spec(self) -> WebhookProviderSpec:
        return PROVIDER_REGISTRY[self.provider]

    @property
    def hmac_algorithm(self) -> Optional[str]:
        """Digest defining verifier-equivalent HMAC key material, if any."""

        if self.signature_mode in {"gitlab", "plain_token", "bearer_token"}:
            return None
        if self.signature_mode == "trello":
            return "sha1"
        if self.signature_mode == "custom_hmac":
            return (
                self.custom_signature.algorithm
                if self.custom_signature is not None
                else None
            )
        return "sha256"


@dataclass(frozen=True)
class WebhookDeliveryIdentity:
    provider: str
    value: str

    @property
    def namespaced(self) -> str:
        return f"{self.provider}:{self.value}"


class WebhookReplayIdentityKind(str, Enum):
    """Provenance of the value used to fence repeated execution."""

    AUTHENTICATED_DELIVERY = "authenticated_delivery"
    AUTHENTICATED_TIMESTAMP_BODY_SHA256 = "authenticated_timestamp_body_sha256"
    AUTHENTICATED_BODY_SHA256 = "authenticated_body_sha256"
    CREDENTIAL_OBSERVED_BODY_SHA256 = "credential_observed_body_sha256"
    LOCAL_BYPASS_BODY_SHA256 = "local_bypass_body_sha256"


@dataclass(frozen=True)
class WebhookReplayIdentity:
    """Durable replay key, distinct from provider delivery diagnostics."""

    provider: str
    kind: WebhookReplayIdentityKind
    value: str

    @property
    def storage_key(self) -> str:
        if self.kind is WebhookReplayIdentityKind.AUTHENTICATED_DELIVERY:
            # Provider IDs may be attacker-controlled HTTP/payload strings.
            # Persist only a fixed-width collision-resistant projection while
            # retaining the verified raw value in ``delivery_identity`` for
            # provider-facing diagnostics.
            digest = hashlib.sha256(self.value.encode("utf-8")).hexdigest()
            return f"{self.kind.value}_sha256:{digest}"
        return f"{self.kind.value}:{self.value}"

    @property
    def namespaced(self) -> str:
        return f"{self.provider}:{self.storage_key}"


def resolve_delivery_identity(
    route: WebhookRouteConfig,
    verified_headers: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    observed_headers: Optional[Mapping[str, Any]] = None,
    allow_authenticated_payload: bool = True,
) -> Optional[WebhookDeliveryIdentity]:
    """Return only a provider-native identity authenticated by the verifier."""

    spec = route.provider_spec
    transport_headers = observed_headers or verified_headers
    if allow_authenticated_payload and spec.authenticated_delivery_id_payload_keys:
        payload_identity = next(
            (
                candidate
                for key in spec.authenticated_delivery_id_payload_keys
                if (candidate := _nonempty_scalar(_payload_value(payload, key)))
                is not None
            ),
            None,
        )
        header_identity = next(
            (
                candidate
                for header_name in spec.delivery_id_headers
                if (
                    candidate := _nonempty_scalar(
                        _header(transport_headers, header_name)
                    )
                )
                is not None
            ),
            None,
        )
        if payload_identity is None or header_identity != payload_identity:
            raise WebhookContractError(
                f"provider {route.provider!r} delivery metadata does not match "
                "the authenticated payload"
            )
        return WebhookDeliveryIdentity(route.provider, payload_identity)

    for header_name in spec.authenticated_delivery_id_headers:
        candidate = _nonempty_scalar(_header(verified_headers, header_name))
        if candidate is not None:
            return WebhookDeliveryIdentity(route.provider, candidate)

    if allow_authenticated_payload and route.provider_declared:
        for key in spec.payload_delivery_id_keys:
            candidate = _nonempty_scalar(_payload_value(payload, key))
            if candidate is not None:
                return WebhookDeliveryIdentity(route.provider, candidate)
    return None


def resolve_event_type(
    route: WebhookRouteConfig,
    verified_headers: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    observed_headers: Optional[Mapping[str, Any]] = None,
) -> str:
    """Resolve event authority without promoting an unsigned provider header."""

    def authenticated_event(value: str) -> str:
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WebhookPayloadContractError(
                "authenticated webhook event type is not valid Unicode"
            ) from exc
        if len(encoded) > MAX_EVENT_TYPE_UTF8_BYTES:
            raise WebhookPayloadContractError(
                "authenticated webhook event type exceeds "
                f"{MAX_EVENT_TYPE_UTF8_BYTES} UTF-8 bytes"
            )
        return value

    spec = route.provider_spec
    transport_headers = observed_headers or verified_headers
    if spec.route_bound_event_authority:
        if not route.events:
            return "unknown"
        event_authority = route.events[0]
        observed_event = next(
            (
                candidate
                for header_name in spec.event_headers
                if (
                    candidate := _nonempty_scalar(
                        _header(transport_headers, header_name)
                    )
                )
                is not None
            ),
            None,
        )
        if observed_event is None:
            raise WebhookContractError(
                f"provider {route.provider!r} is missing the route-bound "
                "event metadata header"
            )
        if observed_event != event_authority:
            raise WebhookContractError(
                f"provider {route.provider!r} observed event metadata does not "
                "match the route-bound event authority"
            )
        return event_authority

    if spec.authenticated_event_payload_keys:
        payload_event = next(
            (
                candidate
                for key in spec.authenticated_event_payload_keys
                if (candidate := _nonempty_scalar(_payload_value(payload, key)))
                is not None
            ),
            None,
        )
        header_event = next(
            (
                candidate
                for header_name in spec.event_headers
                if (
                    candidate := _nonempty_scalar(
                        _header(transport_headers, header_name)
                    )
                )
                is not None
            ),
            None,
        )
        if payload_event is None or header_event != payload_event:
            raise WebhookContractError(
                f"provider {route.provider!r} event metadata does not match "
                "the authenticated payload"
            )
        return authenticated_event(payload_event)

    for key in (*spec.payload_event_keys, "event_type", "type"):
        value = _nonempty_scalar(_payload_value(payload, key))
        if value is not None:
            return authenticated_event(value)
    return "unknown"


def validate_authenticated_event_body(
    route: WebhookRouteConfig,
    event_type: str,
    payload: Mapping[str, Any],
) -> None:
    """Require body-authenticated GitHub payloads to prove their event class.

    GitHub's HMAC covers the raw request body, not ``X-GitHub-Event``. A
    route/header equality check alone would therefore let a captured signed
    ping or neighboring pull-request event be relabeled for a configured
    ``pull_request`` route. Only explicitly registered, conservative body
    shapes can supply route-bound GitHub event authority.
    """

    if route.provider != "github" or event_type == "unknown":
        return
    matches = tuple(
        candidate
        for candidate, classifier in _GITHUB_EVENT_BODY_CLASSIFIERS.items()
        if classifier(payload)
    )
    if matches != (event_type,):
        raise WebhookContractError(
            "provider 'github' authenticated payload shape does not match "
            f"route-bound event {event_type!r}"
        )


def _resolve_observed_delivery_id(
    route: WebhookRouteConfig,
    observed_headers: Mapping[str, Any],
) -> Optional[str]:
    """Return an untrusted provider transport ID for diagnostics only."""

    return next(
        (
            candidate
            for header_name in route.provider_spec.delivery_id_headers
            if (candidate := _nonempty_scalar(_header(observed_headers, header_name)))
            is not None
        ),
        None,
    )


def _resolve_observed_event_type(
    route: WebhookRouteConfig,
    observed_headers: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Optional[str]:
    """Return provider event metadata without granting it dispatch authority."""

    spec = route.provider_spec
    header_event = next(
        (
            candidate
            for header_name in spec.event_headers
            if (candidate := _nonempty_scalar(_header(observed_headers, header_name)))
            is not None
        ),
        None,
    )
    if header_event is not None:
        return header_event
    return next(
        (
            candidate
            for key in (*spec.payload_event_keys, "event_type", "type")
            if (candidate := _nonempty_scalar(_payload_value(payload, key))) is not None
        ),
        None,
    )


def validate_payload_replay_policy(
    route: WebhookRouteConfig,
    payload: Mapping[str, Any],
    *,
    received_at: Optional[float] = None,
) -> None:
    """Enforce provider-declared freshness over authenticated body metadata."""

    spec = route.provider_spec
    tolerance = spec.payload_replay_tolerance_seconds
    if tolerance is None:
        return
    timestamp = next(
        (
            candidate
            for key in spec.authenticated_timestamp_payload_keys
            if (candidate := _nonempty_scalar(payload.get(key))) is not None
        ),
        None,
    )
    if timestamp is None:
        raise WebhookContractError(
            f"provider {route.provider!r} payload is missing authenticated timestamp"
        )
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is required")
        timestamp_seconds = parsed.timestamp()
    except (OverflowError, ValueError):
        raise WebhookContractError(
            f"provider {route.provider!r} payload has malformed authenticated timestamp"
        ) from None
    now = time.time() if received_at is None else float(received_at)
    if not math.isfinite(now):
        raise WebhookContractError("received_at must be a finite timestamp")
    if abs(now - timestamp_seconds) > tolerance:
        raise WebhookContractError(
            f"provider {route.provider!r} authenticated timestamp is outside replay window"
        )


def _parse_authenticated_payload(raw_body: bytes, media_type: str) -> dict[str, Any]:
    """Reconstruct the payload from the exact bytes covered by authentication."""

    normalized_media_type = str(media_type or "").split(";", 1)[0].strip().lower()
    if normalized_media_type == "application/json" or normalized_media_type.endswith(
        "+json"
    ):

        def reject_duplicate_keys(
            pairs: list[tuple[str, Any]],
        ) -> dict[str, Any]:
            decoded: dict[str, Any] = {}
            for key, item in pairs:
                if key in decoded:
                    raise ValueError(f"duplicate JSON key {key!r}")
                decoded[key] = item
            return decoded

        try:
            value = json.loads(
                bytes(raw_body),
                parse_constant=lambda constant: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number {constant}")
                ),
                object_pairs_hook=reject_duplicate_keys,
            )
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise WebhookContractError(
                "authenticated JSON body cannot be parsed"
            ) from exc
        if not isinstance(value, dict):
            raise WebhookContractError("authenticated webhook body must be an object")
        stack: list[tuple[Any, int]] = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if depth > MAX_AUTHENTICATED_JSON_NESTING:
                raise WebhookPayloadContractError(
                    "authenticated webhook JSON nesting is too deep"
                )
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise WebhookPayloadContractError(
                        "authenticated webhook JSON contains invalid Unicode"
                    ) from exc
            elif isinstance(item, Mapping):
                stack.extend((key, depth) for key in item.keys())
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, (list, tuple)):
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, float) and not math.isfinite(item):
                # ``json.loads`` routes the non-standard NaN/Infinity tokens
                # through ``parse_constant``, but an otherwise-valid JSON
                # exponent such as ``1e9999`` overflows directly to infinity.
                # Reject both forms before authenticated values reach policy,
                # rendering, or the durable ledger.
                raise WebhookPayloadContractError(
                    "authenticated webhook JSON contains a non-finite number"
                )
        return value
    raise WebhookContractError(
        "authenticated webhook media type must use JSON semantics"
    )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({
            str(key): _freeze_json(item) for key, item in value.items()
        })
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class WebhookAuthProvenance:
    provider: str
    signature_mode: str
    coverage: str
    provider_declared: bool
    verified: bool
    bypass: Optional[str] = None

    @property
    def compatibility_inferred(self) -> bool:
        return not self.provider_declared


@dataclass(frozen=True, init=False)
class WebhookEnvelope:
    """Immutable domain object handed across the HTTP intake boundary."""

    route: WebhookRouteConfig
    authority_profile: str
    auth: WebhookAuthProvenance
    event_type: str
    delivery_identity: Optional[WebhookDeliveryIdentity]
    replay_identity: WebhookReplayIdentity
    observed_delivery_id: Optional[str]
    observed_event_type: Optional[str]
    trace_id: str
    body_sha256: str
    payload: Mapping[str, Any]

    @classmethod
    def from_receipt(
        cls,
        receipt: Any,
        *,
        raw_body: bytes,
        media_type: str,
        trace_id: Optional[str] = None,
        authority_profile: Optional[str] = None,
    ) -> "WebhookEnvelope":
        from gateway.platforms.webhook_auth import (
            WebhookLocalBypassReceipt,
            WebhookSignatureVerificationReceipt,
            WebhookVerificationCoverage,
        )

        if not isinstance(
            receipt,
            (WebhookSignatureVerificationReceipt, WebhookLocalBypassReceipt),
        ):
            raise WebhookContractError(
                "webhook envelope requires an exact verification receipt"
            )
        if not isinstance(raw_body, (bytes, bytearray)):
            raise WebhookContractError("raw_body must be bytes")
        body_hash = hashlib.sha256(bytes(raw_body)).hexdigest()
        if body_hash != receipt.body_sha256:
            raise WebhookContractError(
                "verification receipt does not cover the supplied raw body"
            )

        authenticated_payload = _parse_authenticated_payload(
            bytes(raw_body), media_type
        )
        route = receipt.route
        physical_profile = (
            route.profile if authority_profile is None else authority_profile
        )
        if not isinstance(physical_profile, str) or not _PROFILE_NAME_RE.fullmatch(
            physical_profile
        ):
            raise WebhookContractError("webhook authority profile must be canonical")
        verified_headers = receipt.verified_headers
        observed_headers = receipt.observed_headers
        transport_headers = dict(observed_headers)
        transport_headers.update(verified_headers)
        validate_payload_replay_policy(
            route,
            authenticated_payload,
            received_at=receipt.verified_at,
        )
        body_authenticated = receipt.coverage in {
            WebhookVerificationCoverage.BODY_MAC,
            WebhookVerificationCoverage.TIMESTAMP_BODY_MAC,
            WebhookVerificationCoverage.ID_TIMESTAMP_BODY_MAC,
        }
        delivery_identity = resolve_delivery_identity(
            route,
            verified_headers,
            authenticated_payload,
            observed_headers=observed_headers,
            allow_authenticated_payload=body_authenticated,
        )
        event_type = resolve_event_type(
            route,
            verified_headers,
            authenticated_payload,
            observed_headers=observed_headers,
        )
        if body_authenticated:
            validate_authenticated_event_body(
                route,
                event_type,
                authenticated_payload,
            )
        observed_delivery_id = _resolve_observed_delivery_id(
            route,
            transport_headers,
        )
        observed_event_type = _resolve_observed_event_type(
            route,
            transport_headers,
            authenticated_payload,
        )
        if delivery_identity is not None:
            replay_identity = WebhookReplayIdentity(
                provider=route.provider,
                kind=WebhookReplayIdentityKind.AUTHENTICATED_DELIVERY,
                value=delivery_identity.value,
            )
        elif (
            body_authenticated
            and receipt.coverage is WebhookVerificationCoverage.TIMESTAMP_BODY_MAC
            and receipt.signed_timestamp
        ):
            timestamp_body_digest = hashlib.sha256(
                receipt.signed_timestamp.encode("utf-8")
                + b"."
                + body_hash.encode("ascii")
            ).hexdigest()
            replay_identity = WebhookReplayIdentity(
                provider=route.provider,
                kind=(WebhookReplayIdentityKind.AUTHENTICATED_TIMESTAMP_BODY_SHA256),
                value=timestamp_body_digest,
            )
        elif body_authenticated:
            replay_identity = WebhookReplayIdentity(
                provider=route.provider,
                kind=WebhookReplayIdentityKind.AUTHENTICATED_BODY_SHA256,
                value=body_hash,
            )
        elif receipt.coverage is WebhookVerificationCoverage.CREDENTIAL_ONLY:
            replay_identity = WebhookReplayIdentity(
                provider=route.provider,
                kind=WebhookReplayIdentityKind.CREDENTIAL_OBSERVED_BODY_SHA256,
                value=body_hash,
            )
        elif receipt.coverage is WebhookVerificationCoverage.LOCAL_BYPASS:
            replay_identity = WebhookReplayIdentity(
                provider=route.provider,
                kind=WebhookReplayIdentityKind.LOCAL_BYPASS_BODY_SHA256,
                value=body_hash,
            )
        else:  # pragma: no cover - exhaustive over the closed coverage enum
            raise WebhookContractError("unsupported webhook verification coverage")
        trace = _nonempty_scalar(trace_id) or uuid.uuid4().hex
        verified = isinstance(receipt, WebhookSignatureVerificationReceipt)
        envelope = object.__new__(cls)
        object.__setattr__(envelope, "route", route)
        object.__setattr__(envelope, "authority_profile", physical_profile)
        object.__setattr__(
            envelope,
            "auth",
            WebhookAuthProvenance(
                provider=route.provider,
                signature_mode=route.signature_mode,
                coverage=receipt.coverage.value,
                provider_declared=route.provider_declared,
                verified=verified,
                bypass=None if verified else "insecure_local_test",
            ),
        )
        object.__setattr__(envelope, "event_type", event_type)
        object.__setattr__(envelope, "delivery_identity", delivery_identity)
        object.__setattr__(envelope, "replay_identity", replay_identity)
        object.__setattr__(
            envelope,
            "observed_delivery_id",
            observed_delivery_id,
        )
        object.__setattr__(
            envelope,
            "observed_event_type",
            observed_event_type,
        )
        object.__setattr__(envelope, "trace_id", trace)
        object.__setattr__(
            envelope,
            "body_sha256",
            body_hash,
        )
        try:
            frozen_payload = _freeze_json(authenticated_payload)
        except RecursionError as exc:  # defense if the bound changes later
            raise WebhookPayloadContractError(
                "authenticated webhook JSON nesting is too deep"
            ) from exc
        object.__setattr__(envelope, "payload", frozen_payload)
        return envelope

    @property
    def delivery_id(self) -> str:
        """Provider delivery ID when stable, otherwise this request trace."""

        if self.delivery_identity is not None:
            return self.delivery_identity.value
        return self.trace_id

    @property
    def replay_id(self) -> str:
        """Storage-safe replay identity used by durable admission."""

        return self.replay_identity.storage_key

    @property
    def idempotency_scope(self) -> tuple[str, str, str, str]:
        return (
            self.authority_profile,
            self.route.name,
            self.replay_identity.provider,
            self.replay_identity.storage_key,
        )

    @property
    def idempotency_key(self) -> str:
        return ":".join(self.idempotency_scope)

    @property
    def session_key(self) -> str:
        """Execution identity, deliberately distinct from provider delivery ID."""

        return ":".join((
            "webhook",
            self.authority_profile,
            self.route.name,
            self.route.provider,
            self.trace_id,
        ))

    def mutable_payload(self) -> dict[str, Any]:
        """Return a detached projection for filters and configured transforms."""

        return _thaw_json(self.payload)
