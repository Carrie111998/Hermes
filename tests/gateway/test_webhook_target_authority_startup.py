"""Startup composition regressions for immutable webhook target authority."""

from pathlib import Path

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from gateway.platforms.webhook_contract import WebhookContractError
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _runner(root: Path, named_home: Path) -> GatewayRunner:
    """Build the real target-authorization registry shape before publication."""

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    runner._resolve_profile_home_for_source = lambda source: (
        named_home if source.profile == "ops" else root
    )
    return runner


def _adapter(runner: GatewayRunner, route: dict) -> WebhookAdapter:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {
                    "events": {
                        "provider": "generic",
                        "signature_mode": "generic_v1",
                        "secret": "startup-target-authority-secret",
                        "deliver_only": True,
                        "prompt": "startup composition",
                        **route,
                    },
                },
                "idempotency_max_entries": 8,
            },
        )
    )
    adapter.gateway_runner = runner
    return adapter


def test_named_profile_home_is_frozen_before_secondary_adapter_publication(
    tmp_path,
    monkeypatch,
):
    root = tmp_path
    ops_home = root / "profiles" / "ops"
    ops_home.mkdir(parents=True)
    (ops_home / "config.yaml").write_text(
        """\
platforms:
  telegram:
    enabled: true
    home_channel:
      platform: telegram
      chat_id: ops-home-chat
      name: Ops home
      thread_id: ops-home-thread
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda **_kwargs: [("default", root), ("ops", ops_home)],
    )
    runner = _runner(root, ops_home)
    adapter = _adapter(
        runner,
        {
            "profile": "ops",
            "deliver": "telegram",
        },
    )

    # Webhook publication precedes secondary adapter publication during
    # multiplex startup. Target authority therefore has to come from the
    # named profile's configured home, not a live adapter registry.
    assert runner.adapters == {}
    assert runner._profile_adapters == {}
    adapter._bind_route_authentication_authorities(adapter._routes)

    target = adapter._authenticated_route_bundles["events"].prepared_target
    assert target.profile == "ops"
    assert target.platform == "telegram"
    assert target.home_chat_id == "ops-home-chat"
    assert target.home_thread_id == "ops-home-thread"
    assert runner.adapters == {}
    assert runner._profile_adapters == {}


def test_default_slack_static_chat_without_scope_fails_before_publication(
    tmp_path,
):
    runner = _runner(tmp_path, tmp_path / "profiles" / "ops")
    adapter = _adapter(
        runner,
        {
            "deliver": "slack",
            "deliver_extra": {"chat_id": "C_TARGET"},
        },
    )

    # Repeating the preflight in the same adapter-free startup state proves
    # this is the deterministic authority contract, not connection ordering.
    for _ in range(2):
        with pytest.raises(
            WebhookContractError,
            match="Slack target workspace scope cannot be established",
        ):
            adapter._bind_route_authentication_authorities(adapter._routes)

    assert adapter._authenticated_route_bundles == {}
    assert runner.adapters == {}
    assert runner._profile_adapters == {}


def test_default_slack_static_chat_and_scope_publish_without_live_adapter(
    tmp_path,
):
    runner = _runner(tmp_path, tmp_path / "profiles" / "ops")
    adapter = _adapter(
        runner,
        {
            "deliver": "slack",
            "deliver_extra": {
                "chat_id": "C_TARGET",
                "scope_id": "T_WORKSPACE",
            },
        },
    )

    assert runner.adapters == {}
    assert runner._profile_adapters == {}
    adapter._bind_route_authentication_authorities(adapter._routes)

    target = adapter._authenticated_route_bundles["events"].prepared_target
    assert target.profile == "default"
    assert target.platform == "slack"
    assert target.slack_static_chat_id == "C_TARGET"
    assert target.slack_static_scope_id == "T_WORKSPACE"
    assert target.slack_scope_locked is True
    assert runner.adapters == {}
    assert runner._profile_adapters == {}
