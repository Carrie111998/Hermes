"""Frozen route-configurable webhook HMAC authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import FrozenInstanceError

import pytest
from aiohttp.test_utils import make_mocked_request
from multidict import CIMultiDict

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_auth import WebhookVerificationCoverage
from gateway.platforms.webhook_common import _authentication_key_fingerprints
from gateway.platforms.webhook_contract import (
    WebhookContractError,
    WebhookRouteConfig,
)


def _adapter() -> WebhookAdapter:
    return WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))


def _route(signature: dict) -> WebhookRouteConfig:
    return WebhookRouteConfig.bind(
        "events",
        {"provider": "custom", "signature": signature},
        headers={},
        request_profile="default",
    )


def _request(headers):
    return make_mocked_request(
        "POST",
        "/webhooks/events",
        headers=headers,
        match_info={"route_name": "events"},
    )


def test_custom_signature_is_normalized_and_frozen_at_route_bind():
    route = _route({
        "header": "ElevenLabs-Signature",
        "signature_part": "v0",
        "timestamp_part": "t",
        "template": "{timestamp}.{body}",
        "algorithm": "HMAC-SHA256",
        "encoding": "hex",
        "timestamp_unit": "seconds",
        "tolerance_seconds": 1800,
    })

    assert route.provider == "custom"
    assert route.signature_mode == "custom_hmac"
    assert route.custom_signature is not None
    assert route.custom_signature.header == "elevenlabs-signature"
    assert route.custom_signature.algorithm == "sha256"
    assert route.custom_signature.timestamp_unit == "seconds"
    with pytest.raises(FrozenInstanceError):
        route.custom_signature.header = "x-other"  # type: ignore[misc]


def test_labeled_timestamp_signature_accepts_one_matching_candidate():
    adapter = _adapter()
    body = b'{"event":"transcript"}'
    secret = "eleven-secret"
    timestamp = str(int(time.time()))
    digest = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    route = _route({
        "header": "ElevenLabs-Signature",
        "signature_part": "v0",
        "timestamp_part": "t",
        "template": "{timestamp}.{body}",
    })

    receipt = adapter._verify_signature_receipt(
        _request({"ElevenLabs-Signature": f"junk,t={timestamp},v0=bad,v0={digest}"}),
        body,
        secret,
        route,
    )

    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.TIMESTAMP_BODY_MAC
    assert receipt.signed_timestamp == timestamp


def test_custom_mode_never_falls_back_to_a_valid_native_header():
    adapter = _adapter()
    body = b"body"
    secret = "secret"
    github = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    route = _route({"header": "X-Custom-Signature", "template": "{body}"})

    assert (
        adapter._verify_signature_receipt(
            _request({"X-Hub-Signature-256": github}),
            body,
            secret,
            route,
        )
        is None
    )


def test_conflicting_timestamps_and_duplicate_physical_headers_fail_closed():
    adapter = _adapter()
    body = b"body"
    secret = "secret"
    timestamp = str(int(time.time()))
    digest = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    route = _route({
        "header": "X-Parts",
        "signature_part": "v1",
        "timestamp_part": "t",
        "template": "{timestamp}.{body}",
    })

    assert (
        adapter._verify_signature_receipt(
            _request({"X-Parts": f"t={timestamp},t={int(timestamp) + 1},v1={digest}"}),
            body,
            secret,
            route,
        )
        is None
    )

    headers = CIMultiDict()
    headers.add("X-Parts", f"t={timestamp},v1={digest}")
    headers.add("X-Parts", f"t={timestamp},v1={digest}")
    assert (
        adapter._verify_signature_receipt(
            _request(headers),
            body,
            secret,
            route,
        )
        is None
    )


@pytest.mark.parametrize("offset", [-360, 360])
def test_custom_seconds_timestamp_window_is_symmetric(offset: int):
    adapter = _adapter()
    body = b"body"
    secret = "secret"
    timestamp = str(int(time.time()) + offset)
    message = b"v0:" + timestamp.encode() + b":" + body
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    route = _route({
        "header": "X-Slack-Signature",
        "signature_prefix": "v0=",
        "timestamp_header": "X-Slack-Request-Timestamp",
        "template": "v0:{timestamp}:{body}",
        "tolerance_seconds": 300,
    })

    assert (
        adapter._verify_signature_receipt(
            _request({
                "X-Slack-Signature": f"v0={signature}",
                "X-Slack-Request-Timestamp": timestamp,
            }),
            body,
            secret,
            route,
        )
        is None
    )


def test_sha512_base64_millisecond_signature_covers_raw_body():
    adapter = _adapter()
    body = b"\xff\x00raw-body"
    secret = "secret"
    timestamp = str(int(time.time() * 1000))
    digest = hmac.new(
        secret.encode(),
        timestamp.encode() + b":" + body,
        hashlib.sha512,
    ).digest()
    route = _route({
        "header": "X-Custom-Signature",
        "timestamp_header": "X-Custom-Timestamp",
        "template": "{timestamp}:{body}",
        "algorithm": "sha512",
        "encoding": "base64",
        "timestamp_unit": "milliseconds",
    })

    receipt = adapter._verify_signature_receipt(
        _request({
            "X-Custom-Signature": base64.b64encode(digest).decode("ascii"),
            "X-Custom-Timestamp": timestamp,
        }),
        body,
        secret,
        route,
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.TIMESTAMP_BODY_MAC


def test_body_only_sha1_alias_accepts_uppercase_hex_and_is_body_bound():
    adapter = _adapter()
    body = b"\xffbody"
    secret = "legacy-provider-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest().upper()
    route = _route({
        "header": "X-Legacy-Signature",
        "template": "{body}",
        "algorithm": "hmac-sha1",
    })

    receipt = adapter._verify_signature_receipt(
        _request({"X-Legacy-Signature": signature}),
        body,
        secret,
        route,
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.BODY_MAC
    assert (
        adapter._verify_signature_receipt(
            _request({"X-Legacy-Signature": signature}),
            body + b"!",
            secret,
            route,
        )
        is None
    )


@pytest.mark.parametrize(
    "signature",
    [
        {},
        {"header": "Bad Header", "template": "{body}"},
        {"header": "X-Sig", "template": "literal"},
        {"header": "X-Sig", "template": "{body}{body}"},
        {"header": "X-Sig", "template": "{body}{unknown}"},
        {"header": "X-Sig", "template": "{body}{"},
        {"header": "X-Sig", "timestamp_header": "X-Time", "template": "{body}"},
        {"header": "X-Sig", "template": "{timestamp}.{body}"},
        {
            "header": "X-Sig",
            "timestamp_header": "X-Time",
            "timestamp_part": "t",
            "template": "{timestamp}.{body}",
        },
        {
            "header": "X-Sig",
            "timestamp_header": "X-Sig",
            "template": "{timestamp}.{body}",
        },
        {
            "header": "X-Sig",
            "signature_part": "v1",
            "timestamp_part": "v1",
            "template": "{timestamp}.{body}",
        },
        {"header": "X-Sig", "template": "{body}", "algorithm": "md5"},
        {"header": "X-Sig", "template": "{body}", "encoding": "base64url"},
        {"header": "X-Sig", "template": "{body}", "tolerance_seconds": 300},
        {
            "header": "X-Sig",
            "timestamp_header": "X-Time",
            "template": "{timestamp}.{body}",
            "timestamp_unit": "minutes",
        },
        {
            "header": "X-Sig",
            "timestamp_header": "X-Time",
            "template": "{timestamp}.{body}",
            "tolerance_seconds": 86_401,
        },
        {"header": "X-Sig", "template": "{body}", "event_header": "X-Event"},
    ],
)
def test_malformed_custom_signature_authority_is_rejected_at_bind(signature):
    with pytest.raises(WebhookContractError):
        _route(signature)


def test_signature_block_is_exclusive_to_custom_hmac():
    with pytest.raises(WebhookContractError):
        WebhookRouteConfig.bind(
            "events",
            {
                "provider": "github",
                "signature": {"header": "X-Sig", "template": "{body}"},
            },
            headers={},
        )
    with pytest.raises(WebhookContractError):
        WebhookRouteConfig.bind(
            "events",
            {"provider": "custom"},
            headers={},
        )


def test_key_fingerprints_follow_selected_hmac_digest_and_token_exactness():
    sha512_key = _authentication_key_fingerprints("key", "custom_hmac", "sha512")
    sha512_padded = _authentication_key_fingerprints("key\0", "custom_hmac", "sha512")
    assert sha512_key & sha512_padded

    token = _authentication_key_fingerprints("key", "plain_token", None)
    padded_token = _authentication_key_fingerprints("key\0", "plain_token", None)
    assert not token & padded_token
