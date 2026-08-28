"""Explicit native-provider verifier coverage for the consolidated adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from aiohttp.test_utils import make_mocked_request

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_auth import WebhookVerificationCoverage
from gateway.platforms.webhook_contract import WebhookRouteConfig
from gateway.platforms.webhook_contract import resolve_event_type


def _adapter() -> WebhookAdapter:
    return WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}}))


def _route(provider: str, **extra) -> WebhookRouteConfig:
    return WebhookRouteConfig.bind(
        "events",
        {"provider": provider, **extra},
        headers={},
        request_profile="default",
    )


def _request(headers: dict[str, str]):
    return make_mocked_request(
        "POST",
        "/webhooks/events",
        headers=headers,
        match_info={"route_name": "events"},
    )


def _hex(secret: str, body: bytes, prefix: str = "") -> str:
    return prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.parametrize(
    ("provider", "header", "prefix"),
    [
        ("sentry", "sentry-hook-signature", ""),
        ("juniper_mist", "X-Mist-Signature-v2", ""),
        ("fireflies", "X-Hub-Signature", "sha256="),
        ("redmine", "X-Redmine-Signature-256", "sha256="),
        ("gitea", "X-Gitea-Signature", ""),
        ("forgejo", "X-Forgejo-Signature", ""),
        ("asana", "X-Hook-Signature", ""),
        ("attio", "Attio-Signature", ""),
        ("notion", "X-Notion-Signature", "sha256="),
        ("exit1", "X-Exit1-Signature", "sha256="),
        ("jira", "X-Hub-Signature", "sha256="),
    ],
)
def test_native_hex_body_modes_are_explicit_and_body_bound(
    provider: str,
    header: str,
    prefix: str,
):
    adapter = _adapter()
    body = b'{"type":"created","id":"evt_1"}'
    secret = f"{provider}-secret"
    route = _route(provider)
    receipt = adapter._verify_signature_receipt(
        _request({header: _hex(secret, body, prefix)}),
        body,
        secret,
        route,
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.BODY_MAC
    assert (
        adapter._verify_signature_receipt(
            _request({header: _hex(secret, body, prefix)}),
            body + b" ",
            secret,
            route,
        )
        is None
    )


def test_todoist_base64_signature_is_body_bound():
    adapter = _adapter()
    body = b'{"event_name":"item:added"}'
    secret = "todoist-secret"
    signature = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode("ascii")
    receipt = adapter._verify_signature_receipt(
        _request({"X-Todoist-Hmac-SHA256": signature}),
        body,
        secret,
        _route("todoist"),
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.BODY_MAC


def test_pocket_signature_binds_millisecond_timestamp_and_body():
    adapter = _adapter()
    body = b'{"event":"recording.ready"}'
    secret = "pocket-secret"
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(
        secret.encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    route = _route("pocket")
    receipt = adapter._verify_signature_receipt(
        _request({
            "X-HeyPocket-Signature": signature,
            "X-HeyPocket-Timestamp": timestamp,
        }),
        body,
        secret,
        route,
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.TIMESTAMP_BODY_MAC
    stale = str(int((time.time() - 301) * 1000))
    stale_signature = hmac.new(
        secret.encode(),
        stale.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    assert (
        adapter._verify_signature_receipt(
            _request({
                "X-HeyPocket-Signature": stale_signature,
                "X-HeyPocket-Timestamp": stale,
            }),
            body,
            secret,
            route,
        )
        is None
    )


def test_linear_requires_fresh_signed_payload_timestamp():
    adapter = _adapter()
    secret = "linear-secret"
    timestamp = int(time.time() * 1000)
    body = json.dumps({"webhookTimestamp": timestamp, "type": "Issue"}).encode()
    signature = _hex(secret, body)
    route = _route("linear")
    receipt = adapter._verify_signature_receipt(
        _request({
            "Linear-Signature": signature,
            "Linear-Timestamp": str(timestamp),
            "Linear-Delivery": "observed-not-signed",
        }),
        body,
        secret,
        route,
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.TIMESTAMP_BODY_MAC
    assert receipt.verified_headers == {}
    assert receipt.observed_headers == {
        "Linear-Delivery": "observed-not-signed",
    }

    stale_body = json.dumps({
        "webhookTimestamp": int((time.time() - 61) * 1000),
        "type": "Issue",
    }).encode()
    assert (
        adapter._verify_signature_receipt(
            _request({"Linear-Signature": _hex(secret, stale_body)}),
            stale_body,
            secret,
            route,
        )
        is None
    )


@pytest.mark.parametrize(
    ("provider", "headers"),
    [
        ("plain_token", {"X-Webhook-Secret": "shared-secret"}),
        ("bearer_token", {"Authorization": "Bearer shared-secret"}),
    ],
)
def test_plain_tokens_use_explicit_credential_only_transports(provider, headers):
    adapter = _adapter()
    route = _route(provider)
    receipt = adapter._verify_signature_receipt(
        _request(headers),
        b'{"untrusted":"body"}',
        "shared-secret",
        route,
    )
    assert receipt is not None
    assert receipt.coverage is WebhookVerificationCoverage.CREDENTIAL_ONLY
    assert (
        adapter._verify_signature_receipt(
            _request(headers),
            b"{}",
            "different-secret",
            route,
        )
        is None
    )


def test_token_transports_and_attio_header_variants_never_fall_through():
    adapter = _adapter()
    body = b"{}"
    secret = "shared-secret"
    assert (
        adapter._verify_signature_receipt(
            _request({"Authorization": f"Bearer {secret}"}),
            body,
            secret,
            _route("plain_token"),
        )
        is None
    )

    x_signature = _hex(secret, body)
    assert (
        adapter._verify_signature_receipt(
            _request({
                "Attio-Signature": "bad",
                "X-Attio-Signature": x_signature,
            }),
            body,
            secret,
            _route("attio"),
        )
        is None
    )
    assert (
        adapter._verify_signature_receipt(
            _request({"X-Attio-Signature": x_signature}),
            body,
            secret,
            _route("attio", signature_mode="attio_x"),
        )
        is not None
    )


def test_fireflies_event_authority_comes_from_signed_body_not_header():
    route = _route("fireflies")
    assert (
        resolve_event_type(
            route,
            {},
            {"event": "meeting.transcribed"},
            observed_headers={"X-Untrusted-Event": "forged"},
        )
        == "meeting.transcribed"
    )


def test_trello_signature_binds_exact_registered_callback_url():
    adapter = _adapter()
    body = b'{"action":{"id":"a1","type":"commentCard"}}'
    secret = "trello-secret"
    callback_url = "https://hooks.example.test/webhooks/events"
    signature = base64.b64encode(
        hmac.new(
            secret.encode(),
            body + callback_url.encode(),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    route = _route("trello", callback_url=callback_url)
    receipt = adapter._verify_signature_receipt(
        _request({"X-Trello-Webhook": signature}),
        body,
        secret,
        route,
    )
    assert receipt is not None
    wrong_route = _route(
        "trello",
        callback_url="https://hooks.example.test/webhooks/events/",
    )
    assert (
        adapter._verify_signature_receipt(
            _request({"X-Trello-Webhook": signature}),
            body,
            secret,
            wrong_route,
        )
        is None
    )
