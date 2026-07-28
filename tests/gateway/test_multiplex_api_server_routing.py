"""The API-server adapter enforces one profile per process."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _PROFILE_REJECTED,
)
from gateway.run import MultiplexConfigError


class _FakeRequest:
    def __init__(self, profile=None):
        self.match_info = {"profile": profile} if profile is not None else {}


@pytest.fixture
def single_profile_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda *, multiplex: [("default", tmp_path)]
        if multiplex is False
        else (_ for _ in ()).throw(
            AssertionError("multiplex inventory must remain unreachable")
        ),
    )
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "key": "direct-adapter-test-key-32-bytes",
            },
        )
    )
    adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=False)
    )
    try:
        yield adapter
    finally:
        adapter._response_store.close()


def test_native_route_table_has_no_profile_prefix(single_profile_adapter):
    paths = {
        path
        for _method, path, _handler
        in single_profile_adapter._http_route_table()
    }

    assert "/v1/models" in paths
    assert "/v1/chat/completions" in paths
    assert all(not path.startswith("/p/") for path in paths)
    assert (
        single_profile_adapter._resolve_request_profile(
            _FakeRequest("other")
        )
        is _PROFILE_REJECTED
    )


def test_direct_adapter_rejects_attached_multiplex_runner(
    single_profile_adapter,
):
    single_profile_adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True)
    )

    with pytest.raises(
        MultiplexConfigError,
        match="one Hermes gateway process per profile",
    ):
        single_profile_adapter._api_multiplex_enabled()


@pytest.mark.asyncio
async def test_direct_adapter_connect_refuses_before_app_or_listener(
    single_profile_adapter,
):
    single_profile_adapter.gateway_runner = SimpleNamespace(
        config=GatewayConfig(multiplex_profiles=True)
    )

    assert await single_profile_adapter.connect() is False
    assert single_profile_adapter._app is None
    assert single_profile_adapter._runner is None
    assert single_profile_adapter._site is None
    assert single_profile_adapter.fatal_error_code == (
        "api_server_multiplex_unsupported"
    )
