"""Runtime contracts for profile-scoped webhook secret references."""
from __future__ import annotations

import os
from types import SimpleNamespace

from agent.secret_scope import set_multiplex_active
from gateway.config import GatewayConfig, Platform, PlatformConfig, _apply_env_overrides
from gateway.platforms.webhook import WebhookAdapter


def test_env_override_keeps_secret_value_out_of_runtime_config(monkeypatch):
    monkeypatch.setenv("WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("WEBHOOK_SECRET", "runtime-secret-must-not-be-copied")
    config = GatewayConfig()

    _apply_env_overrides(config)

    extra = config.platforms[Platform.WEBHOOK].extra
    assert extra["secret_ref"] == "WEBHOOK_SECRET"
    assert "secret" not in extra


def test_explicit_missing_reference_never_falls_back_to_plaintext(monkeypatch):
    monkeypatch.delenv("WEBHOOK_ROUTE_MISSING", raising=False)
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "secret": "legacy-global-secret",
                "routes": {},
            },
        )
    )
    assert adapter._route_secret({"secret_ref": "WEBHOOK_ROUTE_MISSING"}) == ""


def test_named_profile_reference_resolves_inside_its_real_profile_scope(
    monkeypatch, tmp_path
):
    ref = "WEBHOOK_ROUTE_SHARED"
    default_home = tmp_path / "default"
    worker_home = tmp_path / "worker"
    default_home.mkdir()
    worker_home.mkdir()
    (default_home / ".env").write_text(f"{ref}=default-secret\n", encoding="utf-8")
    (worker_home / ".env").write_text(f"{ref}=worker-secret\n", encoding="utf-8")
    monkeypatch.delenv(ref, raising=False)

    adapter = WebhookAdapter(
        PlatformConfig(enabled=True, extra={"routes": {}})
    )
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        _resolve_profile_home_for_source=lambda source: (
            worker_home if source.profile == "worker" else default_home
        ),
    )
    adapter.gateway_runner = runner

    set_multiplex_active(True)
    try:
        assert adapter._route_secret_for_profile(
            {"secret_ref": ref}, "worker"
        ) == "worker-secret"
        assert adapter._route_secret_for_profile(
            {"secret_ref": ref}, None
        ) == "default-secret"
        assert ref not in os.environ
    finally:
        set_multiplex_active(False)
