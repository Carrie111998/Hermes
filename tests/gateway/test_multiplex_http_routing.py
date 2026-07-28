"""Webhook ingress enforces one profile per gateway process."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    _PROFILE_REJECTED,
)
from gateway.run import MultiplexConfigError
from gateway.session import SessionSource, build_session_key


class TestSessionSourceProfileField:
    def test_profile_roundtrips(self):
        source = SessionSource(
            platform=Platform.WEBHOOK,
            chat_id="c1",
            chat_type="webhook",
            profile="coder",
        )

        assert SessionSource.from_dict(source.to_dict()).profile == "coder"

    def test_profile_absent_not_serialized(self):
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="c1",
            chat_type="dm",
        )

        assert "profile" not in source.to_dict()

    def test_explicit_profile_key_namespace_remains_deterministic(self):
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="99",
            chat_type="dm",
        )

        assert (
            build_session_key(source, profile="coder")
            == "agent:coder:telegram:dm:99"
        )


class _FakeRequest:
    def __init__(self, profile=None):
        self.match_info = {"profile": profile} if profile is not None else {}


def _adapter(*, multiplex: bool) -> WebhookAdapter:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {},
            },
        )
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=multiplex)
    )
    return adapter


def test_profile_prefix_is_rejected_in_single_profile_mode():
    adapter = _adapter(multiplex=False)

    assert adapter._resolve_request_profile(_FakeRequest()) is None
    assert (
        adapter._resolve_request_profile(_FakeRequest("other"))
        is _PROFILE_REJECTED
    )


@pytest.mark.asyncio
async def test_direct_webhook_connect_refuses_multiplex_before_startup_work(
    monkeypatch,
):
    adapter = _adapter(multiplex=True)
    calls = []

    def forbidden():
        calls.append("dynamic_routes")
        raise AssertionError("startup work must remain unreachable")

    monkeypatch.setattr(adapter, "_reload_dynamic_routes", forbidden)

    assert await adapter.connect() is False
    assert adapter._runner is None
    assert adapter.fatal_error_code == "webhook_multiplex_unsupported"
    assert calls == []

    with pytest.raises(
        MultiplexConfigError,
        match="one Hermes gateway process per profile",
    ):
        adapter._resolve_request_profile(_FakeRequest())


@pytest.mark.asyncio
async def test_connected_webhook_registers_native_routes_only():
    adapter = _adapter(multiplex=False)
    assert await adapter.connect() is True
    try:
        resources = {
            resource.canonical
            for resource in adapter._runner.app.router.resources()
        }
        assert "/health" in resources
        assert "/webhooks/{route_name}" in resources
        assert all(not path.startswith("/p/") for path in resources)
    finally:
        await adapter.disconnect()
