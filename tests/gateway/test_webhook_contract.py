"""Contracts for canonical webhook provider, identity, and envelope authority."""

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from gateway.platforms.webhook_auth import (
    WebhookAuthMixin,
    WebhookLocalBypassReceipt,
    WebhookSignatureVerificationReceipt,
    WebhookVerificationCoverage,
)
from gateway.platforms.webhook_contract import (
    GITHUB_AUTHENTICATED_EVENTS,
    MAX_EVENT_TYPE_UTF8_BYTES,
    PROVIDER_REGISTRY,
    WebhookContractError,
    WebhookEnvelope,
    WebhookPayloadContractError,
    WebhookReplayIdentityKind,
    WebhookRouteConfig,
    WebhookRouteScopeError,
    canonical_provider,
    canonical_signature_mode,
    resolve_delivery_identity,
    resolve_event_type,
    validate_authenticated_event_body,
)


class _Headers(dict):
    def get(self, key, default=""):
        for candidate, value in self.items():
            if str(candidate).lower() == str(key).lower():
                return value
        return default


class _Request:
    def __init__(self, headers):
        self.headers = _Headers(headers)
        self.match_info = {"route_name": "events"}


class _Verifier(WebhookAuthMixin):
    pass


def verified_envelope(route, raw_body, headers, secret="secret", trace_id=None):
    receipt = _Verifier()._verify_signature_receipt(
        _Request(headers),
        raw_body,
        secret,
        route,
    )
    assert receipt is not None
    return WebhookEnvelope.from_receipt(
        receipt,
        raw_body=raw_body,
        media_type="application/json",
        trace_id=trace_id,
    )


def _body_hmac(body, secret="secret", *, prefix="sha256="):
    return prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _github_pull_request_body(number=7):
    return json.dumps(
        {
            "action": "opened",
            "number": number,
            "pull_request": {
                "id": 701,
                "number": number,
                "state": "open",
                "title": "Authenticated PR",
            },
            "repository": {"id": 801, "full_name": "org/repo"},
            "sender": {"id": 901, "login": "octocat"},
        },
        separators=(",", ":"),
    ).encode()


def _svix_signature(body, secret, msg_id, timestamp):
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def bind(route=None, headers=None, *, profile=None):
    return WebhookRouteConfig.bind(
        "events",
        {} if route is None else route,
        headers=headers or {},
        request_profile=profile,
    )


def make_envelope(
    route,
    raw_body,
    *,
    headers=None,
    bypass=False,
    trace_id=None,
    media_type="application/json",
):
    receipt_type = (
        WebhookLocalBypassReceipt if bypass else WebhookSignatureVerificationReceipt
    )
    receipt = receipt_type._issue(route, raw_body, headers or {})
    return WebhookEnvelope.from_receipt(
        receipt,
        raw_body=raw_body,
        media_type=media_type,
        trace_id=trace_id,
    )


def test_registry_is_read_only_and_has_expected_namespaces():
    assert isinstance(PROVIDER_REGISTRY, MappingProxyType)
    assert {
        "github",
        "gitlab",
        "svix",
        "standard_webhooks",
        "chatwoot",
        "linear",
        "hindsight",
        "hermes",
        "stripe",
        "generic",
    } <= set(PROVIDER_REGISTRY)
    with pytest.raises(TypeError):
        PROVIDER_REGISTRY["evil"] = PROVIDER_REGISTRY["generic"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("agentmail", "svix"),
        ("github-hmac-sha256", "github"),
        ("gitlab_token", "gitlab"),
        ("gitlab-standard", "standard_webhooks"),
        ("generic_v1", "generic"),
        ("generic-v2", "generic"),
        ("hindsight_hmac_sha256", "hindsight"),
        ("hermes-agent", "hermes"),
    ],
)
def test_provider_aliases_canonicalize(configured, expected):
    assert canonical_provider(configured) == expected


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("github-hmac-sha256", "github"),
        ("gitlab-token", "gitlab"),
        ("generic-v2", "generic_v2"),
        ("gitlab_standard", "standard_webhooks"),
        ("hindsight_hmac_sha256", "hindsight"),
        ("hermes-agent", "hermes"),
    ],
)
def test_signature_aliases_normalize_to_executable_modes(configured, expected):
    assert canonical_signature_mode(configured) == expected


def test_unknown_explicit_provider_and_signature_fail_closed():
    with pytest.raises(WebhookContractError, match="unsupported webhook provider"):
        bind({"provider": "made-up"})
    with pytest.raises(
        WebhookContractError, match="unsupported webhook signature mode"
    ):
        bind({"signature_mode": "made-up"})


def test_provider_and_verifier_mode_cannot_disagree():
    with pytest.raises(WebhookContractError, match="does not allow signature mode"):
        bind({"provider": "github", "signature_mode": "svix"})


def test_provider_can_use_registered_generic_verifier():
    route = bind({"provider": "chatwoot", "signature_mode": "generic_v2"})
    assert (route.provider, route.signature_mode) == ("chatwoot", "generic_v2")


def test_route_event_rejects_lone_unicode_surrogate_as_typed_configuration():
    with pytest.raises(WebhookContractError, match="event is not valid Unicode"):
        bind({"provider": "github", "events": ["\udcff"]})


def test_explicit_provider_does_not_promote_uncovered_attacker_headers():
    route = bind(
        {"provider": "github"},
        {
            "svix-id": "msg_attacker",
            "svix-signature": "v1,attacker",
            "X-GitHub-Delivery": "gh_123",
        },
    )
    identity = resolve_delivery_identity(
        route,
        {"svix-id": "msg_attacker", "X-GitHub-Delivery": "gh_123"},
        {},
    )
    assert route.provider_declared is True
    assert identity is None


def test_explicit_generic_route_ignores_github_event_header():
    route = bind({"provider": "generic"})
    assert (
        resolve_event_type(
            route,
            {"X-GitHub-Event": "push"},
            {"event_type": "generic.tick"},
        )
        == "generic.tick"
    )


def test_authenticated_payload_event_type_has_exact_utf8_bound():
    route = bind({"provider": "generic"})

    assert (
        resolve_event_type(
            route,
            {},
            {"event_type": "e" * MAX_EVENT_TYPE_UTF8_BYTES},
        )
        == "e" * MAX_EVENT_TYPE_UTF8_BYTES
    )
    with pytest.raises(WebhookPayloadContractError, match="exceeds 1024"):
        resolve_event_type(
            route,
            {},
            {"event_type": "é" * (MAX_EVENT_TYPE_UTF8_BYTES // 2 + 1)},
        )


def test_authenticated_payload_rejects_lone_unicode_surrogates():
    raw_body = b'{"type":"\\ud800"}'
    with pytest.raises(WebhookPayloadContractError, match="invalid Unicode"):
        make_envelope(bind({"provider": "generic"}), raw_body)


@pytest.mark.parametrize(
    "raw_body",
    [
        b'{"amount":1e9999}',
        b'{"nested":[-1e9999]}',
    ],
)
def test_authenticated_payload_rejects_finite_syntax_that_overflows(raw_body):
    with pytest.raises(WebhookPayloadContractError, match="non-finite number"):
        make_envelope(bind({"provider": "generic"}), raw_body)


@pytest.mark.parametrize(
    "name",
    [" events", "events ", "Events", "events/path", "e" * 129, 7],
)
def test_route_name_must_already_be_one_canonical_url_slug(name):
    with pytest.raises(WebhookContractError, match="canonical lowercase URL slug"):
        WebhookRouteConfig.bind(
            name,
            {"provider": "generic"},
            headers={},
        )


@pytest.mark.parametrize("profile", [" ops", "ops ", "Ops", "ops/path", "p" * 65])
def test_route_profile_must_already_be_canonical(profile):
    with pytest.raises(WebhookContractError, match="non-canonical profile"):
        bind({"provider": "generic", "profile": profile}, profile=profile)


def test_route_bound_event_type_has_exact_utf8_bound():
    with pytest.raises(WebhookContractError, match="event exceeds 1024"):
        bind({"provider": "generic", "events": ["é" * 513]})


@pytest.mark.parametrize(
    "headers",
    [
        {"svix-id": "msg_1", "svix-signature": "v1,attacker"},
        {"X-Hub-Signature-256": "sha256=attacker"},
        {"linear-signature": "attacker"},
        {"X-Hermes-Signature-256": "sha256=attacker"},
        {"Stripe-Signature": "t=1,v1=attacker"},
        {"X-Webhook-Signature-V2": "attacker"},
    ],
)
def test_request_headers_cannot_select_undeclared_provider_authority(headers):
    with pytest.raises(WebhookContractError, match="requires an explicit"):
        bind({}, headers)


def test_explicit_generic_v1_cannot_be_upgraded_or_downgraded_by_headers():
    route = bind(
        {"signature_mode": "generic_v1"},
        {
            "X-Webhook-Signature-V2": "attacker-v2",
            "X-Webhook-Signature": "configured-v1",
            "X-Hub-Signature-256": "sha256=attacker",
        },
    )
    assert (route.provider, route.signature_mode) == ("generic", "generic_v1")


def test_explicit_generic_v2_cannot_be_relabelled_by_provider_headers():
    route = bind(
        {"signature_mode": "generic_v2"},
        {
            "X-Hub-Signature-256": "sha256=attacker",
            "linear-signature": "attacker",
            "X-Hindsight-Signature": "attacker",
            "X-Webhook-Signature": "captured-v1",
        },
    )
    assert (route.provider, route.signature_mode) == ("generic", "generic_v2")


def test_provider_field_verifier_alias_preserves_declared_generic_v1():
    route = bind({"provider": "generic_v1"}, {})
    assert (route.provider, route.signature_mode, route.provider_declared) == (
        "generic",
        "generic_v1",
        True,
    )


def test_signature_mode_can_bind_provider_namespace():
    route = bind({"signature_mode": "generic_v2"})
    assert (route.provider, route.signature_mode, route.provider_declared) == (
        "generic",
        "generic_v2",
        True,
    )


def test_gitlab_transport_ids_are_not_delivery_authority():
    identity = resolve_delivery_identity(
        bind({"provider": "gitlab"}),
        {
            "Idempotency-Key": "fallback",
            "X-Gitlab-Webhook-UUID": "webhook-uuid",
            "X-Gitlab-Event-UUID": "event-uuid",
        },
        {},
    )
    assert identity is None


def test_unbound_event_header_authority_is_route_fixed():
    with pytest.raises(WebhookContractError, match="at most one route-bound event"):
        bind({"provider": "github", "events": ["push", "pull_request"]})

    unfiltered = bind({"provider": "github"})
    assert (
        resolve_event_type(
            unfiltered,
            {},
            {},
            observed_headers={"X-GitHub-Event": "push"},
        )
        == "unknown"
    )

    fixed = bind({"provider": "github", "events": ["pull_request"]})
    with pytest.raises(WebhookContractError, match="missing the route-bound"):
        resolve_event_type(fixed, {}, {}, observed_headers={})
    assert (
        resolve_event_type(
            fixed,
            {},
            {},
            observed_headers={"X-GitHub-Event": "pull_request"},
        )
        == "pull_request"
    )
    with pytest.raises(WebhookContractError, match="route-bound event authority"):
        resolve_event_type(
            fixed,
            {},
            {},
            observed_headers={"X-GitHub-Event": "workflow_run"},
        )


def test_github_route_binding_accepts_only_body_classified_events():
    assert GITHUB_AUTHENTICATED_EVENTS == {
        "check_run",
        "issues",
        "ping",
        "pull_request",
        "push",
    }
    with pytest.raises(
        WebhookContractError,
        match="cannot authenticate event body shape for 'workflow_run'",
    ):
        bind({"provider": "github", "events": ["workflow_run"]})


def test_github_supported_body_classifiers_are_mutually_exclusive():
    repository = {"id": 801, "full_name": "org/repo"}
    sender = {"id": 901, "login": "octocat"}
    payloads = {
        "check_run": {
            "action": "completed",
            "check_run": {
                "id": 401,
                "name": "tests",
                "status": "completed",
            },
            "repository": repository,
            "sender": sender,
        },
        "issues": {
            "action": "opened",
            "issue": {"id": 601, "number": 3},
            "repository": repository,
            "sender": sender,
        },
        "ping": {
            "zen": "Keep it logically awesome.",
            "hook_id": 501,
            "hook": {"id": 501, "type": "Repository"},
        },
        "pull_request": json.loads(_github_pull_request_body()),
        "push": {
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": "1" * 40,
            "created": False,
            "deleted": False,
            "forced": False,
            "commits": [],
            "repository": repository,
            "pusher": {"name": "octocat"},
            "sender": sender,
        },
    }

    for configured_event in GITHUB_AUTHENTICATED_EVENTS:
        route = bind({"provider": "github", "events": [configured_event]})
        for payload_event, payload in payloads.items():
            if configured_event == payload_event:
                validate_authenticated_event_body(route, configured_event, payload)
            else:
                with pytest.raises(WebhookContractError, match="payload shape"):
                    validate_authenticated_event_body(
                        route,
                        configured_event,
                        payload,
                    )

    # Even if a future body happened to satisfy two registered predicates,
    # it cannot inherit either event authority.
    ambiguous = dict(payloads["pull_request"])
    ambiguous.update(payloads["issues"])
    ambiguous["action"] = "edited"
    with pytest.raises(WebhookContractError, match="payload shape"):
        validate_authenticated_event_body(
            bind({"provider": "github", "events": ["pull_request"]}),
            "pull_request",
            ambiguous,
        )

    deployment_status = {
        "action": "created",
        "check_run": {"id": 401, "name": "deploy", "status": "queued"},
        "deployment": {"id": 301},
        "deployment_status": {"id": 302, "state": "pending"},
        "repository": repository,
        "sender": sender,
    }
    with pytest.raises(WebhookContractError, match="payload shape"):
        validate_authenticated_event_body(
            bind({"provider": "github", "events": ["check_run"]}),
            "check_run",
            deployment_status,
        )


def test_github_uncovered_header_mutation_cannot_mint_replay_authority():
    route = bind({"provider": "github", "events": ["pull_request"]})
    body = _github_pull_request_body()
    signature = _body_hmac(body)

    first = verified_envelope(
        route,
        body,
        {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": "delivery-a",
            "X-GitHub-Event": "pull_request",
        },
        trace_id="trace-a",
    )
    second = verified_envelope(
        route,
        body,
        {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": "delivery-b",
            "X-GitHub-Event": "pull_request",
        },
        trace_id="trace-b",
    )

    assert first.delivery_identity is None
    assert second.delivery_identity is None
    assert (first.observed_delivery_id, second.observed_delivery_id) == (
        "delivery-a",
        "delivery-b",
    )
    assert first.replay_identity == second.replay_identity
    assert (
        first.replay_identity.kind
        is WebhookReplayIdentityKind.AUTHENTICATED_BODY_SHA256
    )

    receipt = _Verifier()._verify_signature_receipt(
        _Request({
            "X-Hub-Signature-256": signature,
            "X-GitHub-Delivery": "delivery-c",
            "X-GitHub-Event": "workflow_run",
        }),
        body,
        "secret",
        route,
    )
    assert receipt is not None
    with pytest.raises(WebhookContractError, match="route-bound event authority"):
        WebhookEnvelope.from_receipt(
            receipt,
            raw_body=body,
            media_type="application/json",
        )


def test_signed_github_ping_body_cannot_be_relabelled_as_pull_request():
    route = bind({"provider": "github", "events": ["pull_request"]})
    body = json.dumps(
        {
            "zen": "Keep it logically awesome.",
            "hook_id": 12345,
            "hook": {"id": 12345, "type": "Repository", "name": "web"},
            "repository": {"id": 801, "full_name": "org/repo"},
            "sender": {"id": 901, "login": "octocat"},
        },
        separators=(",", ":"),
    ).encode()
    receipt = _Verifier()._verify_signature_receipt(
        _Request({
            "X-Hub-Signature-256": _body_hmac(body),
            # This unsigned header is the attempted privilege relabel.
            "X-GitHub-Event": "pull_request",
        }),
        body,
        "secret",
        route,
    )

    assert receipt is not None
    with pytest.raises(
        WebhookContractError,
        match="payload shape does not match route-bound event 'pull_request'",
    ):
        WebhookEnvelope.from_receipt(
            receipt,
            raw_body=body,
            media_type="application/json",
        )


def test_gitlab_credential_receipt_uses_fixed_event_and_body_replay_fence():
    route = bind({"provider": "gitlab", "events": ["Push Hook"]})
    body = b'{"object_kind":"push","before":"abc"}'
    first = verified_envelope(
        route,
        body,
        {
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-a",
            "X-GitLab-Event": "Push Hook",
        },
    )
    second = verified_envelope(
        route,
        body,
        {
            "X-Gitlab-Token": "secret",
            "X-Gitlab-Event-UUID": "event-b",
            "X-GitLab-Event": "Push Hook",
        },
    )
    assert first.auth.coverage == WebhookVerificationCoverage.CREDENTIAL_ONLY.value
    assert first.delivery_identity is None
    assert first.event_type == "Push Hook"
    assert first.observed_event_type == "Push Hook"
    assert first.replay_identity == second.replay_identity
    assert (
        first.replay_identity.kind
        is WebhookReplayIdentityKind.CREDENTIAL_OBSERVED_BODY_SHA256
    )


def test_hermes_outbound_headers_resolve_event_and_delivery_identity():
    payload = {
        "delivery_id": "hermes-delivery-1",
        "hook_event_name": "post_tool_call",
        "timestamp": "2026-08-27T00:00:00Z",
    }
    route = bind(
        {"provider": "hermes"},
        {
            "X-Hermes-Signature-256": "sha256=value",
            "X-Hermes-Delivery": "hermes-delivery-1",
            "X-Hermes-Event": "post_tool_call",
        },
    )
    identity = resolve_delivery_identity(
        route,
        {"X-Hermes-Delivery": "hermes-delivery-1"},
        payload,
    )
    assert (route.provider, route.signature_mode) == ("hermes", "hermes")
    assert identity is not None
    assert identity.value == "hermes-delivery-1"
    assert (
        resolve_event_type(route, {"X-Hermes-Event": "post_tool_call"}, payload)
        == "post_tool_call"
    )


@pytest.mark.parametrize("route", [[], "", 0, False])
def test_falsey_non_object_route_config_is_rejected(route):
    with pytest.raises(WebhookContractError, match="must be an object"):
        bind(route)


def test_hermes_unsigned_headers_must_match_authenticated_payload():
    route = bind({"provider": "hermes"})
    payload = {
        "delivery_id": "real-delivery",
        "hook_event_name": "real-event",
    }
    with pytest.raises(WebhookContractError, match="delivery metadata"):
        resolve_delivery_identity(
            route,
            {"X-Hermes-Delivery": "attacker-delivery"},
            payload,
        )
    with pytest.raises(WebhookContractError, match="event metadata"):
        resolve_event_type(
            route,
            {"X-Hermes-Event": "attacker-event"},
            payload,
        )


@pytest.mark.parametrize(
    ("provider", "id_header", "timestamp_header", "signature_header", "msg_id"),
    [
        ("svix", "svix-id", "svix-timestamp", "svix-signature", "msg_1"),
        (
            "standard_webhooks",
            "webhook-id",
            "webhook-timestamp",
            "webhook-signature",
            "wh_1",
        ),
    ],
)
def test_message_id_signature_promotes_exact_native_identity(
    provider,
    id_header,
    timestamp_header,
    signature_header,
    msg_id,
):
    route = bind({"provider": provider})
    body = b'{"type":"message.received"}'
    timestamp = str(int(time.time()))
    envelope = verified_envelope(
        route,
        body,
        {
            id_header: msg_id,
            timestamp_header: timestamp,
            signature_header: _svix_signature(body, "secret", msg_id, timestamp),
        },
    )
    assert envelope.delivery_identity is not None
    assert envelope.delivery_identity.value == msg_id
    assert envelope.replay_identity.kind is (
        WebhookReplayIdentityKind.AUTHENTICATED_DELIVERY
    )
    assert envelope.replay_identity.value == msg_id


def test_chatwoot_uncovered_delivery_header_cannot_change_replay_identity():
    route = bind({"provider": "chatwoot", "signature_mode": "generic_v2"})
    body = b'{"event":"message_created","conversation":{"id":17}}'
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"secret",
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    base_headers = {
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature-V2": signature,
    }
    first = verified_envelope(
        route,
        body,
        {**base_headers, "X-Chatwoot-Delivery": "delivery-a"},
    )
    second = verified_envelope(
        route,
        body,
        {**base_headers, "X-Chatwoot-Delivery": "delivery-b"},
    )
    assert first.delivery_identity is None
    assert first.event_type == "message_created"
    assert first.replay_identity == second.replay_identity
    assert first.observed_delivery_id == "delivery-a"
    assert second.observed_delivery_id == "delivery-b"


def test_hermes_and_stripe_promote_authenticated_body_ids():
    hermes_delivery = "hermes-1"
    hermes_body = json.dumps({
        "delivery_id": hermes_delivery,
        "hook_event_name": "post_tool_call",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }).encode()
    hermes = verified_envelope(
        bind({"provider": "hermes"}),
        hermes_body,
        {
            "X-Hermes-Signature-256": _body_hmac(hermes_body),
            "X-Hermes-Delivery": hermes_delivery,
            "X-Hermes-Event": "post_tool_call",
        },
    )
    assert hermes.delivery_identity is not None
    assert hermes.delivery_identity.value == hermes_delivery
    assert (
        hermes.replay_identity.kind is WebhookReplayIdentityKind.AUTHENTICATED_DELIVERY
    )

    stripe_body = b'{"id":"evt_123","type":"invoice.paid"}'
    stripe_timestamp = str(int(time.time()))
    stripe_signature = hmac.new(
        b"whsec_test",
        stripe_timestamp.encode() + b"." + stripe_body,
        hashlib.sha256,
    ).hexdigest()
    stripe = verified_envelope(
        bind({"provider": "stripe"}),
        stripe_body,
        {"Stripe-Signature": f"t={stripe_timestamp},v1={stripe_signature}"},
        secret="whsec_test",
    )
    assert stripe.delivery_identity is not None
    assert stripe.delivery_identity.value == "evt_123"
    assert stripe.event_type == "invoice.paid"
    assert (
        stripe.replay_identity.kind is WebhookReplayIdentityKind.AUTHENTICATED_DELIVERY
    )


def test_body_only_mode_uses_labeled_authenticated_body_replay_fence():
    envelope = make_envelope(
        bind({"provider": "generic", "signature_mode": "generic_v1"}),
        b'{"type":"tick"}',
        headers={"X-Webhook-Signature": "sig"},
        trace_id="trace-1",
    )
    assert envelope.delivery_identity is None
    assert (
        envelope.replay_identity.kind
        is WebhookReplayIdentityKind.AUTHENTICATED_BODY_SHA256
    )
    assert (
        envelope.replay_identity.value == hashlib.sha256(b'{"type":"tick"}').hexdigest()
    )
    assert "authenticated_body_sha256" in envelope.idempotency_key
    assert envelope.delivery_id == "trace-1"


def test_timestamp_headers_are_never_delivery_identity():
    body = b"{}"
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"secret",
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    envelope = verified_envelope(
        bind({"provider": "generic"}),
        body,
        headers={
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature-V2": signature,
        },
        trace_id="trace-2",
    )
    assert envelope.delivery_identity is None
    expected = hashlib.sha256(
        timestamp.encode() + b"." + hashlib.sha256(body).hexdigest().encode()
    ).hexdigest()
    assert (
        envelope.replay_identity.kind
        is WebhookReplayIdentityKind.AUTHENTICATED_TIMESTAMP_BODY_SHA256
    )
    assert envelope.replay_identity.value == expected
    assert timestamp not in envelope.idempotency_key


def test_fresh_signed_timestamps_distinguish_identical_authenticated_bodies():
    body = b'{"type":"tick"}'
    route = bind({"provider": "generic"})
    now = int(time.time())

    def envelope(timestamp: int):
        timestamp_text = str(timestamp)
        signature = hmac.new(
            b"secret",
            timestamp_text.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        return verified_envelope(
            route,
            body,
            {
                "X-Webhook-Timestamp": timestamp_text,
                "X-Webhook-Signature-V2": signature,
            },
        )

    first = envelope(now)
    second = envelope(now + 1)

    assert first.body_sha256 == second.body_sha256
    assert first.replay_identity != second.replay_identity
    assert (
        first.replay_identity.kind
        is WebhookReplayIdentityKind.AUTHENTICATED_TIMESTAMP_BODY_SHA256
    )


def test_payload_id_is_accepted_only_for_registered_explicit_provider():
    explicit = bind({"provider": "stripe"})
    identity = resolve_delivery_identity(explicit, {}, {"id": "evt_123"})
    assert identity is not None
    assert (identity.provider, identity.value) == ("stripe", "evt_123")


def test_chatwoot_does_not_treat_payload_id_as_delivery_authority():
    route = bind({"provider": "chatwoot", "signature_mode": "generic_v2"})
    assert resolve_delivery_identity(route, {}, {"id": 17}) is None
    assert resolve_event_type(route, {}, {"event": "message_created"}) == (
        "message_created"
    )


def test_idempotency_scope_and_session_trace_are_qualified_and_distinct():
    route = WebhookRouteConfig.bind(
        "deploy",
        {"profile": "ops", "provider": "github"},
        headers={},
        request_profile="ops",
    )
    envelope = make_envelope(
        route,
        b"{}",
        headers={"X-GitHub-Delivery": "abc"},
        trace_id="trace-3",
    )
    body_hash = hashlib.sha256(b"{}").hexdigest()
    assert envelope.delivery_identity is None
    assert envelope.observed_delivery_id == "abc"
    assert envelope.idempotency_scope == (
        "ops",
        "deploy",
        "github",
        f"authenticated_body_sha256:{body_hash}",
    )
    assert envelope.session_key == "webhook:ops:deploy:github:trace-3"
    assert "abc" not in envelope.session_key


def test_route_profile_mismatch_and_malformed_boolean_fail_closed():
    with pytest.raises(WebhookRouteScopeError, match="not bound to profile"):
        WebhookRouteConfig.bind(
            "deploy",
            {"profile": "ops", "provider": "github"},
            headers={},
            request_profile="default",
        )
    with pytest.raises(WebhookContractError, match="enabled must be a boolean"):
        bind({"provider": "github", "enabled": "false"})


def test_envelope_captures_body_hash_and_auth_provenance():
    raw_body = _github_pull_request_body()
    envelope = make_envelope(
        bind({
            "provider": "github",
            "signature_mode": "github",
            "events": ["pull_request"],
        }),
        raw_body,
        headers={
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "pull_request",
        },
        trace_id="trace-4",
    )
    assert envelope.event_type == "pull_request"
    assert envelope.auth.provider == "github"
    assert envelope.auth.signature_mode == "github"
    assert envelope.auth.coverage == WebhookVerificationCoverage.BODY_MAC.value
    assert envelope.auth.compatibility_inferred is False
    assert envelope.auth.verified is True
    assert envelope.body_sha256 == hashlib.sha256(raw_body).hexdigest()


def test_local_bypass_is_explicit_and_other_unverified_envelopes_refuse():
    route = bind({"provider": "generic"})
    envelope = make_envelope(
        route,
        b"{}",
        bypass=True,
    )
    assert envelope.auth.verified is False
    assert envelope.auth.bypass == "insecure_local_test"
    assert envelope.auth.coverage == WebhookVerificationCoverage.LOCAL_BYPASS.value
    assert envelope.delivery_identity is None
    assert (
        envelope.replay_identity.kind
        is WebhookReplayIdentityKind.LOCAL_BYPASS_BODY_SHA256
    )
    assert envelope.replay_identity.value == hashlib.sha256(b"{}").hexdigest()
    with pytest.raises(WebhookContractError, match="exact verification receipt"):
        WebhookEnvelope.from_receipt(
            True,
            raw_body=b"{}",
            media_type="application/json",
        )


def test_envelope_payload_is_recursive_snapshot_and_mutable_copy_is_detached():
    envelope = make_envelope(
        bind({"provider": "generic"}),
        b'{"outer":{"items":[1,{"x":2}]}}',
    )
    with pytest.raises(TypeError):
        envelope.payload["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        envelope.payload["outer"]["new"] = 1  # type: ignore[index]
    mutable = envelope.mutable_payload()
    mutable["outer"]["items"][1]["x"] = 7
    assert envelope.payload["outer"]["items"][1]["x"] == 2


def test_envelope_rejects_bytes_not_covered_by_receipt():
    route = bind({"provider": "generic"})
    receipt = WebhookSignatureVerificationReceipt._issue(
        route,
        b'{"trusted":true}',
        {},
    )
    with pytest.raises(WebhookContractError, match="does not cover"):
        WebhookEnvelope.from_receipt(
            receipt,
            raw_body=b'{"trusted":false}',
            media_type="application/json",
        )


def test_envelope_rejects_media_type_reinterpretation_of_signed_bytes():
    with pytest.raises(WebhookContractError, match="JSON semantics"):
        make_envelope(
            bind({"provider": "generic"}),
            b'{"amount":"1","admin":"true"}',
            media_type="application/x-www-form-urlencoded",
        )


def test_hermes_authenticated_timestamp_must_be_fresh():
    raw_body = (
        b'{"delivery_id":"delivery-1","hook_event_name":"post_tool_call",'
        b'"timestamp":"2026-08-27T00:00:00Z"}'
    )
    with pytest.raises(WebhookContractError, match="outside replay window"):
        make_envelope(
            bind({"provider": "hermes"}),
            raw_body,
            headers={
                "X-Hermes-Delivery": "delivery-1",
                "X-Hermes-Event": "post_tool_call",
            },
        )


def test_envelope_cannot_be_constructed_outside_the_build_gate():
    with pytest.raises(TypeError):
        WebhookEnvelope(  # type: ignore[call-arg]
            route=bind({"provider": "generic"}),
            auth=None,
            event_type="forged",
            delivery_identity=None,
            trace_id="forged",
            body_sha256="forged",
            payload={},
        )


def test_malformed_route_events_are_rejected():
    with pytest.raises(WebhookContractError, match="events must be a sequence"):
        bind({"provider": "github", "events": "push"})


@pytest.mark.parametrize(
    "events",
    [None, "", [1], ["push", ""], {"push"}],
)
def test_route_event_authority_is_not_coerced_or_unordered(events):
    with pytest.raises(WebhookContractError, match="events must"):
        bind({"provider": "github", "events": events})
