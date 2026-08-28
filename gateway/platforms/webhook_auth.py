"""Explicit webhook signature verification.

The normalized route binding selects one canonical verifier mode before this
module is called.  Verifiers inspect only their own wire contract and never
infer or fall through to a different provider from request headers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiohttp import web

    from gateway.platforms.webhook_contract import WebhookRouteConfig


logger = logging.getLogger("gateway.platforms.webhook")


def _hmac_str_equal(provided: str, expected: str) -> bool:
    """Timing-safe string equality that rejects hostile non-ASCII cleanly."""

    try:
        provided_bytes = provided.encode("utf-8")
        expected_bytes = expected.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return hmac.compare_digest(provided_bytes, expected_bytes)


DEFAULT_REPLAY_TOLERANCE_SECONDS = 300

# These are executable verifier names, not configuration aliases.  Aliases are
# normalized exactly once by webhook_contract.WebhookRouteConfig.bind().
SIGNATURE_MODES = frozenset({
    "github",
    "gitlab",
    "standard_webhooks",
    "hindsight",
    "hermes",
    "linear",
    "stripe",
    "svix",
    "generic_v2",
    "generic_v1",
})


class WebhookVerificationCoverage(str, Enum):
    """Exact material authenticated by the selected verifier.

    A successful credential comparison is deliberately distinct from a MAC of
    the request body.  Likewise, a body MAC does not authenticate adjacent
    provider metadata headers.  Consumers must use this type instead of
    treating "signature accepted" as proof over the whole HTTP request.
    """

    BODY_MAC = "body_mac"
    TIMESTAMP_BODY_MAC = "timestamp_body_mac"
    ID_TIMESTAMP_BODY_MAC = "id_timestamp_body_mac"
    CREDENTIAL_ONLY = "credential_only"
    LOCAL_BYPASS = "local_bypass"


def _header_from_mapping(headers, name: str) -> str:
    def valid_text(value: object) -> str:
        text = str(value)
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return ""
        return text

    get_all = getattr(headers, "getall", None)
    if callable(get_all):
        try:
            values = list(get_all(name, ()))
        except (KeyError, TypeError):
            values = []
        if values:
            # Multiple physical occurrences are ambiguous even when their text
            # matches. Authentication must never depend on which duplicate an
            # HTTP stack or intermediary happens to select.
            if len(values) != 1:
                return ""
            return valid_text(values[0]) if values[0] not in (None, "") else ""
    direct = headers.get(name, "")
    if direct:
        return valid_text(direct)
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target and value:
            return valid_text(value)
    return ""


def _snapshot_observed_claims(
    route: "WebhookRouteConfig",
    headers,
    *,
    excluding: tuple[tuple[str, str], ...] = (),
) -> tuple[tuple[str, str], ...]:
    """Snapshot unbound transport hints, never credentials or signatures."""

    names = (
        *route.provider_spec.delivery_id_headers,
        *route.provider_spec.event_headers,
    )
    excluded_names = {name.lower() for name, _value in excluding}
    return tuple(
        (name, value)
        for name in dict.fromkeys(names)
        if name.lower() not in excluded_names
        and (value := _header_from_mapping(headers, name))
    )


@dataclass(frozen=True, slots=True)
class _VerifiedWebhookMaterial:
    """Internal exact output of one selected verifier."""

    coverage: WebhookVerificationCoverage
    verified_claims: tuple[tuple[str, str], ...] = ()
    signed_timestamp: Optional[str] = None


def _coverage_for_mode(signature_mode: str) -> WebhookVerificationCoverage:
    """Coverage used only by private test receipt factories."""

    if signature_mode == "gitlab":
        return WebhookVerificationCoverage.CREDENTIAL_ONLY
    if signature_mode in {"stripe", "generic_v2"}:
        return WebhookVerificationCoverage.TIMESTAMP_BODY_MAC
    if signature_mode in {"svix", "standard_webhooks"}:
        return WebhookVerificationCoverage.ID_TIMESTAMP_BODY_MAC
    return WebhookVerificationCoverage.BODY_MAC


@dataclass(frozen=True, slots=True, init=False)
class WebhookSignatureVerificationReceipt:
    """Exact successful verifier output containing no credential material.

    ``verified_claims`` are values consumed by the MAC itself.  Provider
    delivery/event headers merely observed next to a valid signature live in
    ``observed_claims`` and can never become durable authority by accident.
    """

    route: "WebhookRouteConfig"
    body_sha256: str
    verified_at: float
    coverage: WebhookVerificationCoverage
    verified_claims: tuple[tuple[str, str], ...]
    observed_claims: tuple[tuple[str, str], ...]
    signed_timestamp: Optional[str]

    @classmethod
    def _from_verified_material(
        cls,
        route: "WebhookRouteConfig",
        body: bytes,
        headers,
        material: _VerifiedWebhookMaterial,
    ) -> "WebhookSignatureVerificationReceipt":
        receipt = object.__new__(cls)
        object.__setattr__(receipt, "route", route)
        object.__setattr__(
            receipt,
            "body_sha256",
            hashlib.sha256(body).hexdigest(),
        )
        object.__setattr__(receipt, "verified_at", time.time())
        object.__setattr__(receipt, "coverage", material.coverage)
        object.__setattr__(receipt, "verified_claims", material.verified_claims)
        object.__setattr__(
            receipt,
            "observed_claims",
            _snapshot_observed_claims(
                route,
                headers,
                excluding=material.verified_claims,
            ),
        )
        object.__setattr__(receipt, "signed_timestamp", material.signed_timestamp)
        return receipt

    @classmethod
    def _issue(
        cls,
        route: "WebhookRouteConfig",
        body: bytes,
        headers,
    ) -> "WebhookSignatureVerificationReceipt":
        """Private deterministic fixture factory; production uses a verifier.

        The factory intentionally derives only registry-declared authenticated
        header claims.  In particular, GitHub/GitLab/Chatwoot transport IDs
        remain observed hints even in tests.
        """

        verified_claims = tuple(
            (name, value)
            for name in route.provider_spec.authenticated_delivery_id_headers
            if (value := _header_from_mapping(headers, name))
        )
        return cls._from_verified_material(
            route,
            body,
            headers,
            _VerifiedWebhookMaterial(
                coverage=_coverage_for_mode(route.signature_mode),
                verified_claims=verified_claims,
            ),
        )

    @property
    def verified_headers(self):
        return MappingProxyType(dict(self.verified_claims))

    @property
    def observed_headers(self):
        return MappingProxyType(dict(self.observed_claims))


@dataclass(frozen=True, slots=True, init=False)
class WebhookLocalBypassReceipt:
    """Distinct receipt issued only after the loopback test gate."""

    route: "WebhookRouteConfig"
    body_sha256: str
    verified_at: float
    coverage: WebhookVerificationCoverage
    verified_claims: tuple[tuple[str, str], ...]
    observed_claims: tuple[tuple[str, str], ...]
    signed_timestamp: Optional[str]

    @classmethod
    def _issue(
        cls,
        route: "WebhookRouteConfig",
        body: bytes,
        headers,
    ) -> "WebhookLocalBypassReceipt":
        receipt = object.__new__(cls)
        object.__setattr__(receipt, "route", route)
        object.__setattr__(
            receipt,
            "body_sha256",
            hashlib.sha256(body).hexdigest(),
        )
        object.__setattr__(receipt, "verified_at", time.time())
        object.__setattr__(
            receipt,
            "coverage",
            WebhookVerificationCoverage.LOCAL_BYPASS,
        )
        object.__setattr__(receipt, "verified_claims", ())
        object.__setattr__(
            receipt,
            "observed_claims",
            _snapshot_observed_claims(route, headers),
        )
        object.__setattr__(receipt, "signed_timestamp", None)
        return receipt

    @property
    def verified_headers(self):
        return MappingProxyType({})

    @property
    def observed_headers(self):
        return MappingProxyType(dict(self.observed_claims))


def _header(request: "web.Request", name: str) -> str:
    return _header_from_mapping(request.headers, name)


class WebhookAuthMixin:
    """Validate the verifier selected by the canonical route binding."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._v1_signature_warned: set[str] = set()

    def _verify_github(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature = _header(request, "X-Hub-Signature-256")
        if not signature:
            return None
        expected = (
            "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        if not _hmac_str_equal(signature, expected):
            return None
        return _VerifiedWebhookMaterial(WebhookVerificationCoverage.BODY_MAC)

    def _verify_gitlab(
        self, request, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        token = _header(request, "X-Gitlab-Token")
        if not token or not _hmac_str_equal(token, secret):
            return None
        return _VerifiedWebhookMaterial(WebhookVerificationCoverage.CREDENTIAL_ONLY)

    def _verify_hindsight(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature = _header(request, "X-Hindsight-Signature")
        if not signature:
            return None
        expected = (
            "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        if not _hmac_str_equal(signature, expected):
            return None
        return _VerifiedWebhookMaterial(WebhookVerificationCoverage.BODY_MAC)

    def _verify_hermes(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature = _header(request, "X-Hermes-Signature-256")
        if not signature:
            return None
        expected = (
            "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        )
        if not _hmac_str_equal(signature, expected):
            return None
        return _VerifiedWebhookMaterial(WebhookVerificationCoverage.BODY_MAC)

    def _verify_linear(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature = _header(request, "linear-signature")
        if not signature:
            return None
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not _hmac_str_equal(signature, expected):
            return None
        return _VerifiedWebhookMaterial(WebhookVerificationCoverage.BODY_MAC)

    def _verify_stripe(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature_header = _header(request, "Stripe-Signature")
        if not signature_header:
            return None
        timestamp = ""
        signatures: list[str] = []
        for part in signature_header.split(","):
            key, separator, value = part.strip().partition("=")
            if not separator or not value:
                continue
            if key == "t" and not timestamp:
                timestamp = value
            elif key == "v1":
                signatures.append(value)
        try:
            parsed_timestamp = int(timestamp)
        except (TypeError, ValueError):
            return None
        if abs(int(time.time()) - parsed_timestamp) > DEFAULT_REPLAY_TOLERANCE_SECONDS:
            logger.warning("[webhook] Stripe signature timestamp outside replay window")
            return None
        signed_content = timestamp.encode() + b"." + body
        expected = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
        if not any(_hmac_str_equal(signature, expected) for signature in signatures):
            return None
        return _VerifiedWebhookMaterial(
            WebhookVerificationCoverage.TIMESTAMP_BODY_MAC,
            signed_timestamp=timestamp,
        )

    def _verify_generic_v2(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature = _header(request, "X-Webhook-Signature-V2")
        if not signature:
            return None
        timestamp = _header(request, "X-Webhook-Timestamp")
        if not timestamp:
            logger.warning(
                "[webhook] Route '%s' sent X-Webhook-Signature-V2 with no "
                "X-Webhook-Timestamp; refusing downgrade to legacy V1",
                request.match_info.get("route_name", ""),
            )
            return None
        try:
            parsed_timestamp = int(timestamp)
        except (TypeError, ValueError):
            return None
        if abs(int(time.time()) - parsed_timestamp) > DEFAULT_REPLAY_TOLERANCE_SECONDS:
            logger.warning(
                "[webhook] Route '%s' generic HMAC V2 timestamp outside replay window",
                request.match_info.get("route_name", ""),
            )
            return None
        signed_content = timestamp.encode() + b"." + body
        expected = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
        if not _hmac_str_equal(signature, expected):
            return None
        return _VerifiedWebhookMaterial(
            WebhookVerificationCoverage.TIMESTAMP_BODY_MAC,
            signed_timestamp=timestamp,
        )

    def _verify_generic_v1(
        self, request, body: bytes, secret: str
    ) -> Optional[_VerifiedWebhookMaterial]:
        signature = _header(request, "X-Webhook-Signature")
        if not signature:
            return None
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        route_name = request.match_info.get("route_name", "")
        if route_name not in self._v1_signature_warned:
            self._v1_signature_warned.add(route_name)
            logger.warning(
                "[webhook] Route '%s' uses legacy body-only HMAC without signed "
                "freshness; identical bodies are durably collapsed, so migrate "
                "to generic_v2",
                route_name,
            )
        if not _hmac_str_equal(signature, expected):
            return None
        return _VerifiedWebhookMaterial(WebhookVerificationCoverage.BODY_MAC)

    def _verify_material(
        self,
        request: "web.Request",
        body: bytes,
        secret: str,
        signature_mode: str,
    ) -> Optional[_VerifiedWebhookMaterial]:
        """Return exact material from one selected verifier, failing closed."""

        if not isinstance(body, bytes) or not isinstance(secret, str) or not secret:
            return None
        if signature_mode == "github":
            return self._verify_github(request, body, secret)
        if signature_mode == "gitlab":
            return self._verify_gitlab(request, secret)
        if signature_mode == "standard_webhooks":
            return self._verify_svix_material(
                body=body,
                secret=secret,
                msg_id=_header(request, "webhook-id"),
                timestamp=_header(request, "webhook-timestamp"),
                signature_header=_header(request, "webhook-signature"),
                id_claim_name="webhook-id",
            )
        if signature_mode == "hindsight":
            return self._verify_hindsight(request, body, secret)
        if signature_mode == "hermes":
            return self._verify_hermes(request, body, secret)
        if signature_mode == "linear":
            return self._verify_linear(request, body, secret)
        if signature_mode == "stripe":
            return self._verify_stripe(request, body, secret)
        if signature_mode == "svix":
            return self._verify_svix_material(
                body=body,
                secret=secret,
                msg_id=_header(request, "svix-id"),
                timestamp=_header(request, "svix-timestamp"),
                signature_header=_header(request, "svix-signature"),
                id_claim_name="svix-id",
            )
        if signature_mode == "generic_v1":
            return self._verify_generic_v1(request, body, secret)
        if signature_mode == "generic_v2":
            return self._verify_generic_v2(request, body, secret)

        logger.warning(
            "[webhook] Route '%s' has unsupported signature_mode %r",
            request.match_info.get("route_name", ""),
            signature_mode,
        )
        return None

    def _validate_signature(
        self,
        request: "web.Request",
        body: bytes,
        secret: str,
        signature_mode: str,
    ) -> bool:
        """Compatibility boolean over the typed verification result."""

        return self._verify_material(request, body, secret, signature_mode) is not None

    def _verify_signature_receipt(
        self,
        request: "web.Request",
        body: bytes,
        secret: str,
        route: "WebhookRouteConfig",
    ) -> WebhookSignatureVerificationReceipt | None:
        """Return the exact proof emitted by the already-selected verifier."""

        material = self._verify_material(
            request,
            body,
            secret,
            route.signature_mode,
        )
        if material is None:
            return None
        return WebhookSignatureVerificationReceipt._from_verified_material(
            route,
            body,
            request.headers,
            material,
        )

    def _issue_local_bypass_receipt(
        self,
        request: "web.Request",
        body: bytes,
        route: "WebhookRouteConfig",
    ) -> WebhookLocalBypassReceipt:
        return WebhookLocalBypassReceipt._issue(route, body, request.headers)

    def _verify_svix_material(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        id_claim_name: str,
        tolerance_seconds: int = DEFAULT_REPLAY_TOLERANCE_SECONDS,
    ) -> Optional[_VerifiedWebhookMaterial]:
        """Verify and return the exact ID/timestamp/body MAC components."""

        if not (msg_id and timestamp and signature_header and secret):
            return None
        try:
            parsed_timestamp = int(timestamp)
        except (TypeError, ValueError):
            return None
        if abs(int(time.time()) - parsed_timestamp) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return None

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ signing secret")
                return None
            if not key:
                logger.debug("[webhook] Empty decoded whsec_ signing secret")
                return None
        else:
            try:
                key = secret.encode("utf-8")
            except UnicodeEncodeError:
                return None

        try:
            signed_content = (
                msg_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + body
            )
        except UnicodeEncodeError:
            return None
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and _hmac_str_equal(signature, expected):
                return _VerifiedWebhookMaterial(
                    WebhookVerificationCoverage.ID_TIMESTAMP_BODY_MAC,
                    verified_claims=((id_claim_name, msg_id),),
                    signed_timestamp=timestamp,
                )
        return None

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = DEFAULT_REPLAY_TOLERANCE_SECONDS,
    ) -> bool:
        """Compatibility boolean over typed Svix verification material."""

        return (
            self._verify_svix_material(
                body=body,
                secret=secret,
                msg_id=msg_id,
                timestamp=timestamp,
                signature_header=signature_header,
                id_claim_name="message-id",
                tolerance_seconds=tolerance_seconds,
            )
            is not None
        )
