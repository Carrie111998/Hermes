from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tools.computer_use.transports.http_mcp import HttpMcpTransport
from tools.computer_use.transports.stdio import StdioMcpTransport
from tools.environments import CuaFleetConfig, CuaFleetDesktopProvider
from tools.environments.capability_adapter import resolve_tools
from tools.environments.capability_manifest import CapabilityManifest, desktop_manifest
from tools.environments.compute_provider import ComputeLease, EnvironmentCapabilities
from tools.environments.cua_fleet import (
    CuaFleetConfig as CuaFleetConfigImplementation,
    CuaFleetDesktopProvider as CuaFleetDesktopProviderImplementation,
    _provider_from_config,
)
from tools.environments.desktop_lease import DesktopSandboxManager
from tools.environments.modal_desktop import ModalDesktopConfig


def test_compute_lease_fields() -> None:
    capabilities = EnvironmentCapabilities(computer_use=True)
    lease = ComputeLease("task-1", "lease-1", "modal", "desktop:latest", capabilities)

    assert lease.task_id == "task-1"
    assert lease.lease_id == "lease-1"
    assert lease.provider == "modal"
    assert lease.capabilities.computer_use


def test_environment_capabilities_to_capabilities() -> None:
    capabilities = EnvironmentCapabilities(
        computer_use=True, extras=frozenset({"browser"})
    )

    assert capabilities.to_capabilities() == {
        "terminal",
        "files",
        "process",
        "computer_use",
        "browser",
    }


def test_manifest_parses_dict_and_json() -> None:
    manifest = CapabilityManifest.from_dict({
        "image": "desktop:latest",
        "capabilities": {"terminal": True, "computer_use": {"service": "cua-driver"}},
    })
    json_manifest = CapabilityManifest.from_json(
        json.dumps({"capabilities": ["files"]})
    )

    assert manifest.image == "desktop:latest"
    assert manifest.capabilities["computer_use"].service == "cua-driver"
    assert json_manifest.enabled_capabilities() == {"files"}


def test_desktop_manifest_defaults() -> None:
    manifest = desktop_manifest("desktop:latest")

    assert manifest.image == "desktop:latest"
    assert {
        "terminal",
        "files",
        "process",
        "computer_use",
    } <= manifest.enabled_capabilities()


def test_capability_adapter_resolves_authorized_intersection() -> None:
    tools = resolve_tools(
        {"terminal", "files", "computer_use"},
        {"terminal", "computer_use"},
    )

    assert tools == {"terminal", "computer_use"}


def test_desktop_sandbox_manager_acquire_release() -> None:
    provider = Mock()
    lease = ComputeLease(
        "task-1",
        "lease-1",
        "fake",
        "desktop",
        EnvironmentCapabilities(computer_use=True),
    )
    environment = Mock()
    provider.acquire.return_value = lease
    provider.create_environment.return_value = environment
    manager = DesktopSandboxManager(provider)

    first = manager.acquire("task-1")
    second = manager.acquire("task-1")
    manager.release("task-1")
    manager.release("task-1")

    assert first is second
    assert first.references == 0
    provider.acquire.assert_called_once()
    environment.cleanup.assert_called_once()
    provider.release.assert_called_once_with(lease)


def test_stdio_transport_start_stop() -> None:
    process = Mock()
    process.poll.return_value = None
    with patch(
        "tools.computer_use.transports.stdio.subprocess.Popen", return_value=process
    ) as popen:
        transport = StdioMcpTransport(("cua-driver", "mcp"))
        transport.start()
        assert transport.is_alive()
        transport.stop()

    popen.assert_called_once()
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=5)
    assert not transport.is_alive()


def test_http_transport_start_stop_and_alive() -> None:
    response = Mock()
    response.read.return_value = b'{"result": {}}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    transport = HttpMcpTransport("https://cua.example/mcp")

    assert not transport.is_alive()
    transport.start()
    with patch("tools.computer_use.transports.http_mcp.urlopen", return_value=response):
        assert transport.is_alive()
    transport.stop()
    assert not transport.is_alive()


def test_modal_desktop_config_defaults() -> None:
    config = ModalDesktopConfig()

    assert config.image == "trycua/cua:latest"
    assert config.cua_driver_command == ("cua-driver", "mcp")
    assert config.persistent_filesystem


def test_cua_fleet_config_defaults() -> None:
    config = CuaFleetConfig()

    assert config.base_url == "https://run.cua.ai"
    assert config.token_url.startswith("https://auth.cua.ai/")
    assert config.client_id == ""
    assert config.client_secret == ""


def test_environments_package_reexports_cua_fleet_sdk() -> None:
    assert CuaFleetConfig is CuaFleetConfigImplementation
    assert CuaFleetDesktopProvider is CuaFleetDesktopProviderImplementation


class _FakeFleetClient:
    def __init__(self):
        self.calls = []
        self.pool = SimpleNamespace(
            metadata=SimpleNamespace(namespace="hermes-task", name="hermes-task"),
            status=SimpleNamespace(available_count=1),
        )
        self.claim = SimpleNamespace(metadata=SimpleNamespace(name="hermes-task-claim"))
        self.sandbox = SimpleNamespace(
            namespace="hermes-task", name="sandbox-1", services=["server", "mcp"]
        )

    async def create_pool(self, request):
        self.calls.append(("create_pool", request))
        return self.pool

    async def get_pool(self, pool):
        self.calls.append(("get_pool", pool))
        return self.pool

    async def create_claim(self, request):
        self.calls.append(("create_claim", request))
        return self.claim

    async def wait_claim(self, claim):
        self.calls.append(("wait_claim", claim))
        return self.sandbox

    async def delete_claim(self, claim):
        self.calls.append(("delete_claim", claim))

    async def delete_pool(self, pool):
        self.calls.append(("delete_pool", pool))

    async def service_request(self, sandbox, service, path, request):
        self.calls.append(("service_request", sandbox, service, path, request))
        return SimpleNamespace(
            status=200,
            headers=[],
            body=b'{"result":{"stdout":"ok\\n","stderr":"","returncode":0}}',
        )


class _FakeFleetSdk:
    class HttpClient:
        pass

    class CyclopsCredentials:
        def __init__(self, client_id, client_secret):
            self.client_id = client_id
            self.client_secret = client_secret

    class CyclopsConfiguration:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CyclopsClient:
        client = _FakeFleetClient()

        @classmethod
        def connect(cls, configuration, http_client):
            cls.configuration = configuration
            cls.http_client = http_client
            return cls.client

    class CreatePoolRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CreateClaimRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PoolSpec:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PoolTemplate:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SandboxService:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpHeader:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpResponse:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def _fleet_provider() -> CuaFleetDesktopProvider:
    _FakeFleetSdk.CyclopsClient.client = _FakeFleetClient()
    return CuaFleetDesktopProvider(
        CuaFleetConfig(
            client_id="ukey-test", client_secret="secret", ready_poll_interval=0
        ),
        sdk_module=_FakeFleetSdk,
    )


def test_cua_fleet_sdk_owns_pool_claim_and_namespace_lifecycle() -> None:
    provider = _fleet_provider()

    lease = provider.acquire("task-1", image="registry.example/desktop:latest")
    environment = provider.create_environment(lease)
    provider.release(lease)

    calls = [call[0] for call in _FakeFleetSdk.CyclopsClient.client.calls]
    assert calls[:4] == ["create_pool", "get_pool", "create_claim", "wait_claim"]
    assert calls[-2:] == ["delete_claim", "delete_pool"]
    assert environment.compute_lease is lease
    assert lease.metadata["namespace"] == lease.metadata["pool"]
    request = _FakeFleetSdk.CyclopsClient.client.calls[0][1]
    assert {service.name for service in request.spec.services} == {"server", "mcp"}


def test_cua_fleet_environment_routes_terminal_through_bound_sandbox() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-2")
    environment = provider.create_environment(lease)

    handle = environment._run_bash("printf ok", timeout=10)
    assert handle.wait(timeout=2) == 0
    assert handle.stdout.read() == "ok\n"
    service_call = next(
        call
        for call in _FakeFleetSdk.CyclopsClient.client.calls
        if call[0] == "service_request"
    )
    assert service_call[1] is provider._states[lease.lease_id].sandbox
    assert service_call[2:4] == ("server", "/cmd")
    provider.release(lease)


def test_cua_fleet_requires_client_credentials(monkeypatch) -> None:
    monkeypatch.delenv("CUA_CLIENT_ID", raising=False)
    monkeypatch.delenv("CUA_CLIENT_SECRET", raising=False)
    provider = CuaFleetDesktopProvider(CuaFleetConfig(), sdk_module=_FakeFleetSdk)

    with pytest.raises(RuntimeError, match="CUA_CLIENT_ID"):
        provider.acquire("task-3")


def test_compute_config_selects_cua_fleet_provider(monkeypatch) -> None:
    monkeypatch.setenv("CUA_CLIENT_ID", "ukey-test")
    monkeypatch.setenv("CUA_CLIENT_SECRET", "secret")
    provider = _provider_from_config({
        "provider": "cua_fleet",
        "image": "registry.example/desktop:latest",
        "cua_fleet": {"base_url": "https://run.cua.ai", "pool_prefix": "hermes"},
    })

    assert isinstance(provider, CuaFleetDesktopProvider)
    assert provider.config.image == "registry.example/desktop:latest"


def test_cua_fleet_release_still_deletes_pool_when_claim_delete_fails() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-4")
    client = _FakeFleetSdk.CyclopsClient.client

    async def fail_delete_claim(claim):
        client.calls.append(("delete_claim", claim))
        raise RuntimeError("claim delete failed")

    client.delete_claim = fail_delete_claim

    with pytest.raises(RuntimeError, match="claim delete failed"):
        provider.release(lease)

    assert [call[0] for call in client.calls][-2:] == ["delete_claim", "delete_pool"]


def test_cua_fleet_sdk_package_is_lazy_installable() -> None:
    from tools.lazy_deps import LAZY_DEPS

    assert LAZY_DEPS["terminal.cua_fleet"] == ("cua-fleet==0.0.5",)


def test_cua_fleet_environment_cleanup_releases_sdk_resources() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-5")
    environment = provider.create_environment(lease)

    environment.cleanup()
    environment.cleanup()

    calls = [call[0] for call in _FakeFleetSdk.CyclopsClient.client.calls]
    assert calls.count("delete_claim") == 1
    assert calls.count("delete_pool") == 1
    assert lease.lease_id not in provider._states
