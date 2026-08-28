"""Focused contracts for explicit webhook signature authority."""

import asyncio
import base64
import hashlib
import hmac

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from multidict import CIMultiDict

from gateway.config import PlatformConfig
from gateway.platforms import webhook_auth as auth
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_contract import WebhookEnvelope, WebhookRouteConfig
from gateway.platforms.webhook_ledger import AdmitDisposition, WebhookOperationLedger


class _Headers(dict):
    def get(self, key, default=""):
        for candidate, value in self.items():
            if str(candidate).lower() == str(key).lower():
                return value
        return default


class _Request:
    def __init__(self, headers=None, route="alerts"):
        self.headers = (
            headers
            if callable(getattr(headers, "getall", None))
            else _Headers(headers or {})
        )
        self.match_info = {"route_name": route}


class _Verifier(auth.WebhookAuthMixin):
    pass


def _verifier():
    return _Verifier()


def _route(provider="github"):
    return WebhookRouteConfig.bind(
        "alerts",
        {"provider": provider},
        headers={},
    )


def test_unknown_signature_mode_fails_closed():
    request = _Request({"X-Hub-Signature-256": "sha256=deadbeef"})
    assert _verifier()._validate_signature(request, b"body", "secret", "wat") is False


def test_duplicate_signature_header_is_rejected_even_when_values_match():
    body = b'{"value":1}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    headers = CIMultiDict()
    headers.add("X-Hub-Signature-256", signature)
    headers.add("X-Hub-Signature-256", signature)

    assert (
        _verifier()._validate_signature(_Request(headers), body, "secret", "github")
        is False
    )


def test_empty_secret_never_authenticates_even_with_matching_empty_key_hmac():
    body = b"body"
    signature = "sha256=" + hmac.new(b"", body, hashlib.sha256).hexdigest()
    request = _Request({"X-Hub-Signature-256": signature})
    assert _verifier()._validate_signature(request, body, "", "github") is False


def test_github_mode_does_not_accept_other_provider_headers():
    request = _Request({
        "X-Gitlab-Token": "secret",
        "linear-signature": hmac.new(b"secret", b"body", hashlib.sha256).hexdigest(),
    })
    assert (
        _verifier()._validate_signature(request, b"body", "secret", "github") is False
    )


def test_github_mode_accepts_exact_body_hmac():
    body = b'{"ok":true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _Request({"X-Hub-Signature-256": signature})
    assert _verifier()._validate_signature(request, body, "secret", "github") is True


def test_successful_verifier_returns_exact_non_secret_receipt():
    body = b'{"ok":true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _Request({
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "push",
        "Authorization": "must-not-be-snapshotted",
    })
    route = _route()
    receipt = _verifier()._verify_signature_receipt(
        request,
        body,
        "secret",
        route,
    )
    assert isinstance(receipt, auth.WebhookSignatureVerificationReceipt)
    assert receipt.route is route
    assert receipt.coverage is auth.WebhookVerificationCoverage.BODY_MAC
    assert receipt.verified_headers == {}
    assert receipt.observed_headers == {
        "X-GitHub-Delivery": "delivery-1",
        "X-GitHub-Event": "push",
    }
    assert "secret" not in repr(receipt)
    assert "Authorization" not in repr(receipt)


def test_receipt_snapshots_claims_and_cannot_be_constructed_directly():
    body = b'{"ok":true}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Delivery": "delivery-1",
    }
    request = _Request(headers)
    receipt = _verifier()._verify_signature_receipt(
        request,
        body,
        "secret",
        _route(),
    )
    headers["X-GitHub-Delivery"] = "attacker"
    request.headers["X-GitHub-Delivery"] = "attacker"
    assert receipt.observed_headers["X-GitHub-Delivery"] == "delivery-1"
    with pytest.raises(TypeError):
        auth.WebhookSignatureVerificationReceipt(  # type: ignore[call-arg]
            route=_route(),
            body_sha256="forged",
            verified_at=0.0,
            coverage=auth.WebhookVerificationCoverage.BODY_MAC,
            verified_claims=(),
            observed_claims=(),
            signed_timestamp=None,
        )


def test_gitlab_receipt_is_credential_only_and_does_not_claim_body_or_headers():
    body = b'{"object_kind":"push"}'
    request = _Request({
        "X-Gitlab-Token": "secret",
        "X-Gitlab-Event-UUID": "event-1",
        "X-GitLab-Event": "Push Hook",
    })
    receipt = _verifier()._verify_signature_receipt(
        request,
        body,
        "secret",
        _route("gitlab"),
    )
    assert receipt is not None
    assert receipt.coverage is auth.WebhookVerificationCoverage.CREDENTIAL_ONLY
    assert receipt.verified_headers == {}
    assert receipt.observed_headers == {
        "X-Gitlab-Event-UUID": "event-1",
        "X-GitLab-Event": "Push Hook",
    }


def test_linear_mode_preserves_current_main_wire_contract():
    body = b'{"type":"Issue"}'
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _Request({"linear-signature": signature})
    assert _verifier()._validate_signature(request, body, "secret", "linear") is True


def test_hindsight_mode_uses_its_own_sha256_header():
    body = b'{"event":"memory"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _Request({"X-Hindsight-Signature": signature})
    assert _verifier()._validate_signature(request, body, "secret", "hindsight") is True


def test_hermes_mode_accepts_outbound_agent_signature_without_github_aliasing():
    body = b'{"hook_event_name":"post_tool_call"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _Request({"X-Hermes-Signature-256": signature})
    assert _verifier()._validate_signature(request, body, "secret", "hermes") is True
    assert _verifier()._validate_signature(request, body, "secret", "github") is False


def test_generic_v2_requires_timestamp_even_when_v1_is_valid():
    body = b"payload"
    v1 = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _Request({
        "X-Webhook-Signature-V2": "present-but-invalid-without-timestamp",
        "X-Webhook-Signature": v1,
    })
    assert (
        _verifier()._validate_signature(request, body, "secret", "generic_v2") is False
    )


def test_generic_v2_binds_timestamp_and_body(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b"payload"
    timestamp = str(now)
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
    })
    verifier = _verifier()
    assert verifier._validate_signature(request, body, "secret", "generic_v2") is True
    assert (
        verifier._validate_signature(request, body + b"!", "secret", "generic_v2")
        is False
    )


def test_generic_v2_rejects_expired_timestamp(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now - auth.DEFAULT_REPLAY_TOLERANCE_SECONDS - 1)
    body = b"payload"
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
    })
    assert (
        _verifier()._validate_signature(request, body, "secret", "generic_v2") is False
    )


def test_generic_v2_rejects_future_timestamp_outside_replay_window(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now + auth.DEFAULT_REPLAY_TOLERANCE_SECONDS + 1)
    body = b"payload"
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
    })

    assert (
        _verifier()._validate_signature(request, body, "secret", "generic_v2") is False
    )


@pytest.mark.parametrize(
    "offset",
    [
        -auth.DEFAULT_REPLAY_TOLERANCE_SECONDS,
        auth.DEFAULT_REPLAY_TOLERANCE_SECONDS,
    ],
)
def test_generic_v2_accepts_exact_replay_window_boundaries(monkeypatch, offset):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now + offset)
    body = b"payload"
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
    })

    assert (
        _verifier()._validate_signature(request, body, "secret", "generic_v2") is True
    )


@pytest.mark.parametrize(
    "duplicated_header",
    ["X-Webhook-Timestamp", "X-Webhook-Signature-V2"],
)
def test_generic_v2_rejects_duplicate_physical_authority_headers(
    monkeypatch,
    duplicated_header,
):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now)
    body = b"payload"
    signature = hmac.new(
        b"secret", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    headers = CIMultiDict()
    headers.add("X-Webhook-Timestamp", timestamp)
    headers.add("X-Webhook-Signature-V2", signature)
    headers.add(
        duplicated_header,
        timestamp if duplicated_header == "X-Webhook-Timestamp" else signature,
    )

    assert (
        _verifier()._validate_signature(_Request(headers), body, "secret", "generic_v2")
        is False
    )


def test_generic_v2_timestamp_and_body_compose_the_durable_replay_identity(
    monkeypatch,
    tmp_path,
):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b'{"type":"tick"}'
    route = _route("generic")
    verifier = _verifier()

    def envelope(timestamp_value, trace_id):
        timestamp = str(timestamp_value)
        signature = hmac.new(
            b"secret", timestamp.encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        receipt = verifier._verify_signature_receipt(
            _Request({
                "X-Webhook-Timestamp": timestamp,
                "X-Webhook-Signature-V2": signature,
            }),
            body,
            "secret",
            route,
        )
        assert receipt is not None
        return WebhookEnvelope.from_receipt(
            receipt,
            raw_body=body,
            media_type="application/json",
            trace_id=trace_id,
        )

    first = envelope(now, "trace-first")
    exact_replay = envelope(now, "trace-replay")
    fresh_timestamp = envelope(now + 1, "trace-fresh")
    ledger = WebhookOperationLedger(tmp_path / "webhooks.db")

    assert ledger.admit(first).disposition is AdmitDisposition.ACCEPTED
    assert ledger.admit(exact_replay).disposition is AdmitDisposition.ACTIVE
    assert ledger.admit(fresh_timestamp).disposition is AdmitDisposition.ACCEPTED
    assert first.body_sha256 == fresh_timestamp.body_sha256
    assert first.replay_identity != fresh_timestamp.replay_identity
    assert ledger.count() == 2


def test_stripe_mode_binds_timestamp_and_body(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b'{"id":"evt_123"}'
    timestamp = str(now)
    signature = hmac.new(
        b"whsec_test", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({"Stripe-Signature": f"t={timestamp},v1={signature}"})
    verifier = _verifier()
    assert verifier._validate_signature(request, body, "whsec_test", "stripe") is True
    assert (
        verifier._validate_signature(request, body + b"!", "whsec_test", "stripe")
        is False
    )
    receipt = verifier._verify_signature_receipt(
        request,
        body,
        "whsec_test",
        _route("stripe"),
    )
    assert receipt is not None
    assert receipt.coverage is auth.WebhookVerificationCoverage.TIMESTAMP_BODY_MAC
    assert receipt.signed_timestamp == timestamp
    assert receipt.verified_headers == {}


def test_stripe_mode_rejects_expired_timestamp(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now - auth.DEFAULT_REPLAY_TOLERANCE_SECONDS - 1)
    body = b'{"id":"evt_123"}'
    signature = hmac.new(
        b"whsec_test", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({"Stripe-Signature": f"t={timestamp},v1={signature}"})
    assert (
        _verifier()._validate_signature(request, body, "whsec_test", "stripe") is False
    )


def test_stripe_mode_rejects_future_timestamp_outside_replay_window(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now + auth.DEFAULT_REPLAY_TOLERANCE_SECONDS + 1)
    body = b'{"id":"evt_123"}'
    signature = hmac.new(
        b"whsec_test", timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    request = _Request({"Stripe-Signature": f"t={timestamp},v1={signature}"})

    assert (
        _verifier()._validate_signature(request, body, "whsec_test", "stripe") is False
    )


def _svix_signature(body, secret, msg_id, timestamp):
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    return (
        "v1,"
        + base64.b64encode(
            hmac.new(secret.encode(), signed, hashlib.sha256).digest()
        ).decode()
    )


@pytest.mark.parametrize(
    ("mode", "id_header", "timestamp_header", "signature_header"),
    [
        ("svix", "svix-id", "svix-timestamp", "svix-signature"),
        (
            "standard_webhooks",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
        ),
    ],
)
def test_id_timestamp_modes_reject_future_timestamp_outside_replay_window(
    monkeypatch,
    mode,
    id_header,
    timestamp_header,
    signature_header,
):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    timestamp = str(now + auth.DEFAULT_REPLAY_TOLERANCE_SECONDS + 1)
    body = b'{"value":1}'
    signature = _svix_signature(body, "secret", "message-1", timestamp)
    request = _Request({
        id_header: "message-1",
        timestamp_header: timestamp,
        signature_header: signature,
    })

    assert _verifier()._validate_signature(request, body, "secret", mode) is False


@pytest.mark.parametrize(
    ("mode", "id_header", "timestamp_header", "signature_header"),
    [
        ("svix", "svix-id", "svix-timestamp", "svix-signature"),
        (
            "standard_webhooks",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
        ),
    ],
)
def test_id_timestamp_modes_reject_identity_substitution(
    monkeypatch,
    mode,
    id_header,
    timestamp_header,
    signature_header,
):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b'{"value":1}'
    signature = _svix_signature(body, "secret", "signed-id", str(now))
    request = _Request({
        id_header: "attacker-id",
        timestamp_header: str(now),
        signature_header: signature,
    })

    assert _verifier()._validate_signature(request, body, "secret", mode) is False


@pytest.mark.parametrize(
    ("mode", "id_header", "timestamp_header", "signature_header"),
    [
        ("svix", "svix-id", "svix-timestamp", "svix-signature"),
        (
            "standard_webhooks",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
        ),
    ],
)
@pytest.mark.parametrize("duplicated", ["id", "timestamp"])
def test_id_timestamp_modes_reject_duplicate_physical_authority_headers(
    monkeypatch,
    mode,
    id_header,
    timestamp_header,
    signature_header,
    duplicated,
):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b'{"value":1}'
    signature = _svix_signature(body, "secret", "message-1", str(now))
    headers = CIMultiDict()
    headers.add(id_header, "message-1")
    headers.add(timestamp_header, str(now))
    headers.add(signature_header, signature)
    headers.add(
        id_header if duplicated == "id" else timestamp_header,
        ("message-1" if duplicated == "id" else str(now)),
    )

    assert (
        _verifier()._validate_signature(_Request(headers), body, "secret", mode)
        is False
    )


def test_standard_webhooks_uses_standard_headers(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b"payload"
    timestamp = str(now)
    signature = _svix_signature(body, "secret", "wh_1", timestamp)
    request = _Request({
        "webhook-id": "wh_1",
        "webhook-timestamp": timestamp,
        "webhook-signature": signature,
        "svix-id": "attacker",
    })
    verifier = _verifier()
    assert (
        verifier._validate_signature(request, body, "secret", "standard_webhooks")
        is True
    )
    receipt = verifier._verify_signature_receipt(
        request,
        body,
        "secret",
        _route("standard_webhooks"),
    )
    assert receipt is not None
    assert receipt.coverage is auth.WebhookVerificationCoverage.ID_TIMESTAMP_BODY_MAC
    assert receipt.verified_headers == {"webhook-id": "wh_1"}
    assert receipt.observed_headers == {}
    assert receipt.signed_timestamp == timestamp


def test_svix_receipt_carries_the_exact_signed_id(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b'{"type":"message.received"}'
    timestamp = str(now)
    signature = _svix_signature(body, "secret", "msg_1", timestamp)
    request = _Request({
        "svix-id": "msg_1",
        "svix-timestamp": timestamp,
        "svix-signature": signature,
    })
    receipt = _verifier()._verify_signature_receipt(
        request,
        body,
        "secret",
        _route("svix"),
    )
    assert receipt is not None
    request.headers["svix-id"] = "attacker"
    assert receipt.verified_headers == {"svix-id": "msg_1"}
    assert receipt.signed_timestamp == timestamp


def test_svix_uses_explicit_replay_tolerance(monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    body = b"payload"
    timestamp = str(now - 11)
    signature = _svix_signature(body, "secret", "msg_1", timestamp)
    assert (
        _verifier()._validate_svix_signature(
            body,
            "secret",
            "msg_1",
            timestamp,
            signature,
            tolerance_seconds=10,
        )
        is False
    )


def test_direct_svix_compatibility_verifier_rejects_surrogate_message_id():
    assert (
        _verifier()._validate_svix_signature(
            b"body",
            "secret",
            "\udcff",
            "1800000000",
            "v1,unused",
        )
        is False
    )


def test_non_ascii_attacker_header_fails_cleanly():
    request = _Request({"X-Gitlab-Token": "sécret"})
    assert _verifier()._validate_signature(request, b"", "secret", "gitlab") is False


@pytest.mark.parametrize(
    ("mode", "header"),
    [
        ("github", "X-Hub-Signature-256"),
        ("gitlab", "X-Gitlab-Token"),
    ],
)
def test_surrogate_authentication_header_fails_closed(mode, header):
    request = _Request({header: "\udcff"})

    assert _verifier()._validate_signature(request, b"body", "secret", mode) is False


@pytest.mark.asyncio
async def test_raw_invalid_utf8_signature_header_returns_401():
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "alerts": {"secret": "secret", "provider": "github"},
                },
            },
        )
    )
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)

    async with TestServer(app) as server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        try:
            writer.write(
                b"POST /webhooks/alerts HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Connection: close\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"X-Hub-Signature-256: \xff\r\n"
                b"\r\n{}"
            )
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=2)
            response_body = await asyncio.wait_for(reader.read(), timeout=2)
        finally:
            writer.close()
            await writer.wait_closed()

    assert status_line == b"HTTP/1.1 401 Unauthorized\r\n"
    assert b'{"error": "Invalid signature"}' in response_body


@pytest.mark.parametrize(
    ("mode", "id_header", "timestamp_header", "signature_header"),
    [
        ("svix", "svix-id", "svix-timestamp", "svix-signature"),
        (
            "standard_webhooks",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
        ),
    ],
)
def test_id_timestamp_mode_rejects_surrogate_message_id_before_mac_encoding(
    monkeypatch,
    mode,
    id_header,
    timestamp_header,
    signature_header,
):
    now = 1_800_000_000
    monkeypatch.setattr(auth.time, "time", lambda: now)
    request = _Request({
        id_header: "\udcff",
        timestamp_header: str(now),
        signature_header: "v1,unused",
    })

    assert _verifier()._validate_signature(request, b"body", "secret", mode) is False
