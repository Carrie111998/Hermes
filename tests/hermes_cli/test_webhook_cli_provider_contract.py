"""Provider-contract coverage for ``hermes webhook subscribe/test``."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from argparse import Namespace
from types import SimpleNamespace

import pytest

from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.webhook import (
    _bind_subscription,
    _default_test_payload,
    _load_subscriptions,
    _save_subscriptions,
    _test_headers,
    webhook_command,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: True)


def _args(**overrides) -> Namespace:
    values = {
        "webhook_action": "subscribe",
        "name": "events",
        "prompt": "",
        "events": "",
        "provider": "github",
        "signature_mode": "",
        "route_profile": "default",
        "description": "",
        "skills": "",
        "deliver": "log",
        "deliver_chat_id": "",
        "secret": "test-secret",
        "payload": "",
        "script": "",
        "deliver_only": False,
    }
    values.update(overrides)
    return Namespace(**values)


def _capture_urlopen(monkeypatch):
    captured = {}

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"status":"accepted"}'

    def _urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    return captured


def _handler(_args):
    return None


def test_subscribe_parser_exposes_provider_contract_flags():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_webhook_parser(subparsers, cmd_webhook=_handler)

    parsed = parser.parse_args([
        "webhook",
        "subscribe",
        "chatwoot",
        "--provider",
        "chatwoot",
        "--signature-mode",
        "generic_v2",
        "--route-profile",
        "ops",
    ])

    assert parsed.provider == "chatwoot"
    assert parsed.signature_mode == "generic_v2"
    assert parsed.route_profile == "ops"
    assert parsed.func is _handler


def test_named_profile_subscription_persists_and_tests_the_prefixed_url(
    monkeypatch,
    capsys,
):
    webhook_command(_args(route_profile="Ops"))

    route = _load_subscriptions()["events"]
    assert route["profile"] == "ops"
    assert "/p/ops/webhooks/events" in capsys.readouterr().out

    captured = _capture_urlopen(monkeypatch)
    webhook_command(_args(webhook_action="test"))

    assert captured["request"].full_url == "http://localhost:8644/p/ops/webhooks/events"


def test_subscribe_wildcard_bind_labels_local_url_and_requires_https_proxy(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        "hermes_cli.webhook._get_webhook_config",
        lambda: {"enabled": True, "extra": {"host": "0.0.0.0", "port": 8644}},
    )

    webhook_command(_args(route_profile="ops"))

    output = capsys.readouterr().out
    assert "Local test URL: http://localhost:8644/p/ops/webhooks/events" in output
    assert (
        "External callback: a public HTTPS reverse proxy is required; use "
        "https://<public-host>/p/ops/webhooks/events and preserve the exact path."
        in output
    )
    assert "Do not configure an external service with the local test URL." in output
    assert "Callback URL: http://localhost" not in output


def test_subscribe_nonloopback_bind_labels_callback_url(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.webhook._get_webhook_config",
        lambda: {
            "enabled": True,
            "extra": {"host": "hooks.internal.example", "port": 9443},
        },
    )

    webhook_command(_args())

    output = capsys.readouterr().out
    assert "Callback URL: http://hooks.internal.example:9443/webhooks/events" in output
    assert "Local test URL:" not in output
    assert "public HTTPS reverse proxy is required" not in output


def test_real_entrypoint_preserves_route_profile_for_webhook_subcommand(
    monkeypatch,
):
    import hermes_cli.main as main_mod

    original_home = os.environ["HERMES_HOME"]
    argv = [
        "hermes",
        "webhook",
        "subscribe",
        "entrypoint-profile",
        "--route-profile",
        "ops",
        "--provider",
        "generic",
        "--signature-mode",
        "generic_v2",
        "--secret",
        "test-secret",
    ]
    monkeypatch.setenv("HERMES_S6_SUPERVISED_CHILD", "1")
    monkeypatch.setattr(sys, "argv", argv.copy())

    # Exercise the same pre-argparse scanner and full parser/dispatch path as
    # the console entrypoint. The route flag must neither switch HERMES_HOME
    # nor disappear before the webhook subparser sees it.
    main_mod._apply_profile_override()
    assert sys.argv == argv
    assert os.environ["HERMES_HOME"] == original_home
    main_mod.main()

    route = _load_subscriptions()["entrypoint-profile"]
    assert route["profile"] == "ops"


def test_subscribe_rejects_route_name_over_contract_bound(capsys):
    webhook_command(_args(name="r" * 129))

    assert _load_subscriptions() == {}
    assert "at most 128" in capsys.readouterr().out


def test_subscribe_persists_canonical_explicit_provider_contract():
    webhook_command(
        _args(
            provider="github_hmac_sha256",
            events="pull_request, pull_request, ",
        )
    )

    route = _load_subscriptions()["events"]
    assert route["provider"] == "github"
    assert route["signature_mode"] == "github"
    assert route["events"] == ["pull_request"]


def test_subscribe_rejects_multiple_github_events_without_persisting(capsys):
    webhook_command(_args(events="push,pull_request"))

    assert _load_subscriptions() == {}
    output = capsys.readouterr().out
    assert "at most one route-bound event" in output


def test_subscribe_rejects_github_event_without_body_classifier(capsys):
    webhook_command(_args(events="workflow_run"))

    assert _load_subscriptions() == {}
    assert (
        "cannot authenticate event body shape for 'workflow_run'"
        in capsys.readouterr().out
    )


def test_subscribe_allows_multiple_payload_authoritative_events():
    webhook_command(_args(provider="generic", events="build,deploy"))

    route = _load_subscriptions()["events"]
    assert route["provider"] == "generic"
    assert route["signature_mode"] == "generic_v2"
    assert route["events"] == ["build", "deploy"]


@pytest.mark.parametrize(
    ("provider", "expected_hint"),
    [
        (
            "github",
            "Authentication: GitHub webhook secret (X-Hub-Signature-256 HMAC-SHA256).",
        ),
        (
            "gitlab",
            "Authentication: GitLab Secret token (X-Gitlab-Token exact string match).",
        ),
        (
            "generic",
            "Authentication: generic_v2 secret "
            "(X-Webhook-Timestamp + X-Webhook-Signature-V2 HMAC-SHA256).",
        ),
    ],
)
def test_subscribe_success_reports_provider_authentication_contract(
    provider,
    expected_hint,
    capsys,
):
    webhook_command(_args(provider=provider))

    output = capsys.readouterr().out
    assert expected_hint in output
    assert "Use the secret for HMAC-SHA256 signature validation" not in output


def test_github_test_uses_route_bound_event_and_exact_body_mac(monkeypatch):
    _save_subscriptions({
        "events": {
            "description": "Agent-created subscription: events",
            "provider": "github",
            "signature_mode": "github",
            "events": ["pull_request"],
            "secret": "test-secret",
        }
    })
    captured = _capture_urlopen(monkeypatch)

    webhook_command(_args(webhook_action="test"))

    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    expected = hmac.new(b"test-secret", request.data, hashlib.sha256).hexdigest()
    assert headers["x-github-event"] == "pull_request"
    assert headers["x-hub-signature-256"] == f"sha256={expected}"
    payload = json.loads(request.data)
    assert payload["pull_request"]["number"] == payload["number"]
    assert {"repository", "sender"} <= payload.keys()
    assert captured["timeout"] == 10


def test_generic_v2_test_uses_timestamp_body_mac_and_selected_event(
    monkeypatch,
):
    _save_subscriptions({
        "events": {
            "description": "Agent-created subscription: events",
            "provider": "generic",
            "signature_mode": "generic_v2",
            "events": ["deploy", "build"],
            "secret": "test-secret",
        }
    })
    monkeypatch.setattr("hermes_cli.webhook.time.time", lambda: 1_700_000_000)
    captured = _capture_urlopen(monkeypatch)

    webhook_command(_args(webhook_action="test"))

    request = captured["request"]
    headers = {key.lower(): value for key, value in request.header_items()}
    signed = b"1700000000." + request.data
    expected = hmac.new(b"test-secret", signed, hashlib.sha256).hexdigest()
    assert headers["x-webhook-timestamp"] == "1700000000"
    assert headers["x-webhook-signature-v2"] == expected
    assert json.loads(request.data)["event_type"] == "deploy"


@pytest.mark.parametrize(
    ("provider", "signature_mode", "events"),
    [
        ("github", "github", ["check_run"]),
        ("github", "github", ["issues"]),
        ("github", "github", ["ping"]),
        ("github", "github", ["pull_request"]),
        ("github", "github", ["push"]),
        ("gitlab", "gitlab", ["Push Hook"]),
        ("hindsight", "hindsight", ["memory.created"]),
        ("linear", "linear", ["Issue"]),
        ("generic", "generic_v1", ["deploy"]),
        ("generic", "generic_v2", ["deploy"]),
        ("chatwoot", "generic_v2", ["message_created"]),
    ],
)
def test_default_payloads_without_native_identity_have_fresh_body_test_ids(
    provider,
    signature_mode,
    events,
    monkeypatch,
):
    route = _bind_subscription(
        "events",
        {
            "provider": provider,
            "signature_mode": signature_mode,
            "events": events,
        },
    )
    tokens = iter(("11" * 12, "22" * 12))
    monkeypatch.setattr(
        "hermes_cli.webhook.secrets.token_hex",
        lambda length: next(tokens) if length == 12 else "",
    )

    first = _default_test_payload(route)
    second = _default_test_payload(route)

    assert first["test_id"] == f"test_{'11' * 12}"
    assert second["test_id"] == f"test_{'22' * 12}"
    assert first != second


@pytest.mark.parametrize(
    ("provider", "signature_mode", "events"),
    [
        ("github", "github", ["check_run"]),
        ("github", "github", ["issues"]),
        ("github", "github", ["ping"]),
        ("github", "github", ["pull_request"]),
        ("github", "github", ["push"]),
        ("gitlab", "gitlab", ["Push Hook"]),
        ("svix", "svix", ["message.created"]),
        ("standard_webhooks", "standard_webhooks", ["message.created"]),
        ("hindsight", "hindsight", ["memory.created"]),
        ("hermes", "hermes", ["post_tool_call"]),
        ("linear", "linear", ["Issue"]),
        ("stripe", "stripe", ["invoice.paid"]),
        ("generic", "generic_v1", ["deploy"]),
        ("generic", "generic_v2", ["deploy"]),
        ("chatwoot", "generic_v2", ["message_created"]),
    ],
)
def test_generated_default_request_satisfies_selected_verifier(
    provider,
    signature_mode,
    events,
):
    from gateway.platforms.webhook_auth import WebhookAuthMixin
    from gateway.platforms.webhook_contract import WebhookEnvelope

    class _Verifier(WebhookAuthMixin):
        pass

    route = _bind_subscription(
        "events",
        {
            "provider": provider,
            "signature_mode": signature_mode,
            "events": events,
        },
    )
    payload_object = _default_test_payload(route)
    payload = json.dumps(payload_object, ensure_ascii=False).encode()
    headers = _test_headers(route, "test-secret", payload, payload_object)
    request = SimpleNamespace(headers=headers, match_info={"route_name": "events"})

    receipt = _Verifier()._verify_signature_receipt(
        request,
        payload,
        "test-secret",
        route,
    )
    assert receipt is not None
    envelope = WebhookEnvelope.from_receipt(
        receipt,
        raw_body=payload,
        media_type="application/json",
    )
    assert envelope.event_type == events[0]


def test_test_command_rejects_unbound_manual_route_before_network(
    capsys,
    monkeypatch,
):
    _save_subscriptions({
        "events": {
            "description": "manually created",
            "secret": "test-secret",
        }
    })
    called = False

    def _unexpected_urlopen(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("urllib.request.urlopen", _unexpected_urlopen)

    webhook_command(_args(webhook_action="test"))

    assert called is False
    assert "requires an explicit provider or signature_mode" in capsys.readouterr().out
