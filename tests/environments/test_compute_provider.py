from __future__ import annotations

import json
import urllib.error
import urllib.request
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
    assert config.pool == "hermes-desktop"
    assert config.replicas == 1


def test_cua_fleet_config_rejects_non_positive_replicas() -> None:
    with pytest.raises(ValueError, match="replicas"):
        CuaFleetConfig(replicas=0)


def test_environments_package_reexports_cua_fleet_sdk() -> None:
    assert CuaFleetConfig is CuaFleetConfigImplementation
    assert CuaFleetDesktopProvider is CuaFleetDesktopProviderImplementation


class _FakeFleetClient:
    def __init__(self):
        self.calls = []
        self.pool = SimpleNamespace(
            metadata=SimpleNamespace(namespace="hermes-desktop", name="hermes-desktop"),
            status=SimpleNamespace(available_count=1),
            spec=None,
        )
        self.pools = []
        self.claim = SimpleNamespace(metadata=SimpleNamespace(name="hermes-task-claim"))
        self.sandbox = SimpleNamespace(
            namespace="hermes-task", name="sandbox-1", services=["server", "mcp"]
        )

    async def list_pools(self, namespace):
        self.calls.append(("list_pools", namespace))
        return self.pools

    async def create_pool(self, request):
        self.calls.append(("create_pool", request))
        self.pool.spec = request.spec
        self.pools = [self.pool]
        return self.pool

    async def update_pool(self, pool):
        self.calls.append(("update_pool", pool))
        self.pool = pool
        self.pools = [pool]
        return pool

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


class _FakeRecord:
    def __eq__(self, other):
        return type(self) is type(other) and self.__dict__ == other.__dict__


class _FakeFleetSdk:
    class HttpClient:
        pass

    class CyclopsCredentials:
        def __init__(self, client_id, client_secret):
            self.client_id = client_id
            self.client_secret = client_secret

    class CyclopsConfiguration(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CyclopsClient:
        client = _FakeFleetClient()
        connect_calls = 0

        @classmethod
        def connect(cls, configuration, http_client):
            cls.connect_calls += 1
            cls.configuration = configuration
            cls.http_client = http_client
            return cls.client

    class CreatePoolRequest(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class CreateClaimRequest(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PoolSpec(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class PoolTemplate(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SandboxService(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpHeader(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpRequest(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class HttpResponse(_FakeRecord):
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def _fleet_provider(
    replicas: int = 2, available_count: int | None = None, ready_timeout: float = 600
) -> CuaFleetDesktopProvider:
    _FakeFleetSdk.CyclopsClient.client = _FakeFleetClient()
    _FakeFleetSdk.CyclopsClient.client.pool.status.available_count = (
        replicas if available_count is None else available_count
    )
    _FakeFleetSdk.CyclopsClient.connect_calls = 0
    return CuaFleetDesktopProvider(
        CuaFleetConfig(
            client_id="ukey-test",
            client_secret="secret",
            replicas=replicas,
            ready_poll_interval=0,
            ready_timeout=ready_timeout,
        ),
        sdk_module=_FakeFleetSdk,
    )


def test_cua_fleet_reconciles_missing_pool_and_releases_only_claim() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-1", image="registry.example/desktop:latest")
    environment = provider.create_environment(lease)
    provider.release(lease)
    calls = [call[0] for call in _FakeFleetSdk.CyclopsClient.client.calls]
    assert calls[:5] == [
        "list_pools",
        "create_pool",
        "get_pool",
        "create_claim",
        "wait_claim",
    ]
    assert calls[-1] == "delete_claim"
    assert "delete_pool" not in calls
    assert environment.compute_lease is lease
    assert lease.metadata["namespace"] == "hermes-desktop"
    assert lease.metadata["pool"] == "hermes-desktop"
    assert _FakeFleetSdk.CyclopsClient.client.pool.spec.replicas == 2


def test_cua_fleet_reconcile_is_noop_when_pool_spec_matches() -> None:
    provider = _fleet_provider()
    client = _FakeFleetSdk.CyclopsClient.client
    desired = provider._pool_request(
        _FakeFleetSdk, "hermes-desktop", provider.config.image
    )
    client.pool.spec = desired.spec
    client.pools = [client.pool]
    lease = provider.acquire("task-existing")
    provider.release(lease)
    calls = [call[0] for call in client.calls]
    assert "create_pool" not in calls
    assert "update_pool" not in calls
    assert calls.count("delete_claim") == 1
    assert "delete_pool" not in calls


def test_cua_fleet_reconcile_updates_drifted_pool() -> None:
    provider = _fleet_provider()
    client = _FakeFleetSdk.CyclopsClient.client
    client.pool.spec = SimpleNamespace(replicas=99)
    client.pools = [client.pool]
    lease = provider.acquire("task-drifted")
    provider.release(lease)
    calls = [call[0] for call in client.calls]
    assert "create_pool" not in calls
    assert calls.count("update_pool") == 1
    assert client.pool.spec.replicas == 2
    assert "delete_pool" not in calls


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
        "cua_fleet": {
            "base_url": "https://run.cua.ai",
            "pool": "hermes-desktop",
            "replicas": 2,
        },
    })

    assert isinstance(provider, CuaFleetDesktopProvider)
    assert provider.config.image == "registry.example/desktop:latest"
    assert provider.config.replicas == 2


def test_cua_fleet_waits_for_configured_replicas() -> None:
    provider = _fleet_provider(available_count=1, ready_timeout=0)

    with pytest.raises(TimeoutError, match="Timed out waiting for Fleet pool"):
        provider.acquire("task-insufficient-capacity")


def test_cua_fleet_release_reports_claim_delete_failure_without_deleting_pool() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-4")
    client = _FakeFleetSdk.CyclopsClient.client

    async def fail_delete_claim(claim):
        client.calls.append(("delete_claim", claim))
        raise RuntimeError("claim delete failed")

    client.delete_claim = fail_delete_claim
    with pytest.raises(RuntimeError, match="claim delete failed"):
        provider.release(lease)
    assert [call[0] for call in client.calls][-1] == "delete_claim"
    assert "delete_pool" not in [call[0] for call in client.calls]


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
    assert "delete_pool" not in calls
    assert lease.lease_id not in provider._states


def test_cua_fleet_http_client_uses_concrete_execute_first() -> None:
    class AbstractHttpClient:
        async def execute(self, request):
            raise NotImplementedError

    client_type = CuaFleetDesktopProvider._http_client_type(AbstractHttpClient)
    assert client_type.execute is not AbstractHttpClient.execute


def test_cua_fleet_terminal_parses_sse_command_response() -> None:
    from tools.environments.cua_fleet import _parse_service_response

    parsed = _parse_service_response(
        b'data: {"success":true,"result":{"stdout":"ok\\n","stderr":"","returncode":0}}\n\n'
    )
    assert parsed["result"] == {"stdout": "ok\n", "stderr": "", "returncode": 0}


def test_cua_fleet_reconcile_recovers_pool_after_ambiguous_create_failure() -> None:
    provider = _fleet_provider()
    client = _FakeFleetSdk.CyclopsClient.client
    original_create = client.create_pool

    async def create_then_fail(request):
        await original_create(request)
        raise TimeoutError("ambiguous create timeout")

    client.create_pool = create_then_fail
    lease = provider.acquire("task-timeout")
    provider.release(lease)
    calls = [call[0] for call in client.calls]
    assert calls.count("create_pool") == 1
    assert calls.count("list_pools") == 2
    assert "delete_pool" not in calls


def test_cua_fleet_claim_release_can_retry_after_transient_failure() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-retry-release")
    client = _FakeFleetSdk.CyclopsClient.client
    original = client.delete_claim
    attempts = 0

    async def fail_once(claim):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient delete failure")
        await original(claim)

    client.delete_claim = fail_once
    with pytest.raises(RuntimeError, match="transient delete failure"):
        provider.release(lease)
    assert lease.lease_id in provider._states

    provider.release(lease)
    assert lease.lease_id not in provider._states
    assert attempts == 2


def test_cua_fleet_provider_reuses_sdk_client_across_claims() -> None:
    provider = _fleet_provider()
    first = provider.acquire("task-client-one")
    first_client = provider._states[first.lease_id].client
    second = provider.acquire("task-client-two")
    second_client = provider._states[second.lease_id].client
    assert first_client is second_client
    assert _FakeFleetSdk.CyclopsClient.connect_calls == 1
    provider.release(first)
    provider.release(second)


def test_cua_fleet_http_client_rejects_redirects(monkeypatch) -> None:
    from tools.environments.cua_fleet import _UrlLibHttpClient

    class Sdk:
        class HttpHeader:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        class HttpResponse:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

    client = _UrlLibHttpClient(Sdk, 1, ("run.cua.ai",))
    request = SimpleNamespace(
        url="https://run.cua.ai/api", body=None, method="GET", headers=[]
    )
    redirected = urllib.error.HTTPError(
        request.url, 302, "Found", {"Location": "https://evil.example/steal"}, None
    )
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        Mock(return_value=Mock(open=Mock(side_effect=redirected))),
    )
    response = client._execute(request)
    assert response.status == 302


def test_cua_fleet_environment_cleanup_retries_claim_release() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-env-retry")
    environment = provider.create_environment(lease)
    client = _FakeFleetSdk.CyclopsClient.client
    original = client.delete_claim
    attempts = 0

    async def fail_once(claim):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient cleanup failure")
        await original(claim)

    client.delete_claim = fail_once
    with pytest.raises(RuntimeError, match="transient cleanup failure"):
        environment.cleanup()
    environment.cleanup()
    assert attempts == 2
    assert lease.lease_id not in provider._states


def test_desktop_terminal_reuses_existing_task_lease_without_incrementing(monkeypatch):
    from tools import terminal_tool
    from tools.environments import desktop_lease

    environment = Mock()
    manager = Mock()
    manager.get.return_value = SimpleNamespace(environment=environment)
    monkeypatch.setattr(desktop_lease, "get_desktop_sandbox_manager", lambda: manager)

    result = terminal_tool._create_environment(
        "desktop",
        "desktop:latest",
        "/root",
        60,
        task_id="task-existing",
    )

    assert result is environment
    manager.get.assert_called_once_with("task-existing")
    manager.acquire.assert_not_called()
