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
    assert config.pool == "hermes-desktop"
    assert config.replicas == 1


def test_cua_fleet_config_rejects_non_positive_replicas() -> None:
    with pytest.raises(ValueError, match="replicas"):
        CuaFleetConfig(replicas=0)


def test_environments_package_reexports_cua_fleet_sdk() -> None:
    assert CuaFleetConfig is CuaFleetConfigImplementation
    assert CuaFleetDesktopProvider is CuaFleetDesktopProviderImplementation


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes | None = None):
        self.status_code = status_code
        self.content = content or b'{"result":{"stdout":"ok\\n","stderr":"","returncode":0}}'


class _FakeServices:
    def __init__(self):
        self.calls: list[tuple[str, str, str, object]] = []

    async def request(self, name, *, method, path, json=None):
        self.calls.append((name, method, path, json))
        return _FakeResponse()


class _FakeSandbox:
    def __init__(self):
        self.name = "sandbox-1"
        self.services = _FakeServices()


class _FakeClaimContext:
    def __init__(self, sandbox: _FakeSandbox):
        self.sandbox = sandbox
        self.entered = 0
        self.exited = 0
        self.fail_exit_once = False

    async def __aenter__(self):
        self.entered += 1
        return self.sandbox

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.exited += 1
        if self.fail_exit_once and self.exited == 1:
            raise RuntimeError("claim release failed")


class _FakePool:
    def __init__(self):
        self.name = "hermes-desktop"
        self.claim_contexts: list[_FakeClaimContext] = []

    def claim(self):
        context = _FakeClaimContext(_FakeSandbox())
        self.claim_contexts.append(context)
        return context


class _FakeSandboxApi:
    configure_calls: list[dict[str, str]] = []
    reconcile_configs: list[dict[str, object]] = []
    pool = _FakePool()

    @classmethod
    def reset(cls) -> None:
        cls.configure_calls = []
        cls.reconcile_configs = []
        cls.pool = _FakePool()

    @staticmethod
    def configure(**kwargs) -> None:
        _FakeSandboxApi.configure_calls.append(kwargs)

    class Image:
        @staticmethod
        def from_registry(ref: str):
            return SimpleNamespace(ref=ref)

    class Pool:
        @classmethod
        async def reconcile(cls, config):
            _FakeSandboxApi.reconcile_configs.append(dict(config))
            return _FakeSandboxApi.pool


def _fleet_provider(replicas: int = 2) -> CuaFleetDesktopProvider:
    _FakeSandboxApi.reset()
    return CuaFleetDesktopProvider(
        CuaFleetConfig(
            client_id="ukey-test",
            client_secret="secret",
            image="registry.example/desktop:latest",
            pool="hermes-desktop",
            replicas=replicas,
        ),
        sandbox_module=_FakeSandboxApi,
    )


def test_cua_fleet_reconciles_named_pool_with_public_sandbox_api() -> None:
    provider = _fleet_provider()

    lease = provider.acquire("task-1")

    assert _FakeSandboxApi.configure_calls == [{
        "fleet_base_url": "https://run.cua.ai",
        "token_url": (
            "https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token"
        ),
        "client_id": "ukey-test",
        "client_secret": "secret",
    }]
    assert len(_FakeSandboxApi.reconcile_configs) == 1
    config = _FakeSandboxApi.reconcile_configs[0]
    assert config["name"] == "hermes-desktop"
    assert config["image"].ref == "registry.example/desktop:latest"
    assert config["replicas"] == 2
    assert config["services"] == {"server": 8000, "mcp": 3000}
    assert lease.metadata == {"pool": "hermes-desktop", "sandbox": "sandbox-1"}
    assert _FakeSandboxApi.pool.claim_contexts[0].entered == 1

    provider.release(lease)
    assert _FakeSandboxApi.pool.claim_contexts[0].exited == 1


def test_cua_fleet_environment_routes_terminal_through_named_server_service() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-2")
    environment = provider.create_environment(lease)

    handle = environment._run_bash("printf ok")

    assert handle.stdout.read() == "ok\n"
    service_call = _FakeSandboxApi.pool.claim_contexts[0].sandbox.services.calls[0]
    assert service_call[:3] == ("server", "POST", "/cmd")
    provider.release(lease)


def test_cua_fleet_mcp_transport_routes_through_named_mcp_service() -> None:
    from tools.environments.cua_fleet import _FleetMcpTransport

    provider = _fleet_provider()
    lease = provider.acquire("task-mcp")
    state = provider._states[lease.lease_id]
    worker = provider._workers[lease.lease_id]
    transport = _FleetMcpTransport(state, worker, provider.config.request_timeout)

    transport.start()

    service_call = state.sandbox.services.calls[0]
    assert service_call[:3] == ("mcp", "POST", "/mcp")
    assert service_call[3]["method"] == "initialize"
    provider.release(lease)


def test_cua_fleet_requires_client_credentials(monkeypatch) -> None:
    monkeypatch.delenv("CUA_CLIENT_ID", raising=False)
    monkeypatch.delenv("CUA_CLIENT_SECRET", raising=False)
    provider = CuaFleetDesktopProvider(
        CuaFleetConfig(), sandbox_module=_FakeSandboxApi
    )

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


def test_cua_fleet_environment_cleanup_exits_claim_once() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-4")
    environment = provider.create_environment(lease)

    environment.cleanup()
    environment.cleanup()

    context = _FakeSandboxApi.pool.claim_contexts[0]
    assert context.exited == 1
    assert lease.lease_id not in provider._states


def test_cua_fleet_claim_release_can_retry_after_transient_failure() -> None:
    provider = _fleet_provider()
    lease = provider.acquire("task-retry-release")
    context = _FakeSandboxApi.pool.claim_contexts[0]
    context.fail_exit_once = True

    with pytest.raises(RuntimeError, match="claim release failed"):
        provider.release(lease)
    assert lease.lease_id in provider._states

    provider.release(lease)
    assert lease.lease_id not in provider._states
    assert context.exited == 2


def test_cua_fleet_terminal_parses_sse_command_response() -> None:
    from tools.environments.cua_fleet import _parse_service_response

    parsed = _parse_service_response(
        b'data: {"success":true,"result":{"stdout":"ok\\n","stderr":"","returncode":0}}\n\n'
    )
    assert parsed["result"] == {"stdout": "ok\n", "stderr": "", "returncode": 0}


def test_cua_fleet_sandbox_package_is_lazy_installable() -> None:
    from tools.lazy_deps import LAZY_DEPS

    assert LAZY_DEPS["terminal.cua_fleet"] == ("cua-sandbox==0.1.20",)


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


def test_cua_fleet_provider_uses_public_sandbox_facade() -> None:
    from tools.lazy_deps import LAZY_DEPS

    assert LAZY_DEPS["terminal.cua_fleet"] == ("cua-sandbox==0.1.20",)


def test_cua_fleet_nix_build_declares_evdev_build_system() -> None:
    from pathlib import Path

    python_nix = Path(__file__).parents[2] / "nix" / "python.nix"
    assert '"evdev"' in python_nix.read_text()
