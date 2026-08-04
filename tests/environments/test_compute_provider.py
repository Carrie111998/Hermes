from __future__ import annotations

import asyncio
import enum
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tools.computer_use.transports.http_mcp import HttpMcpTransport
from tools.computer_use.transports.modal_sandbox import ModalSandboxMcpTransport
from tools.computer_use.transports.stdio import StdioMcpTransport
from tools.environments import CuaFleetConfig, CuaFleetDesktopProvider
from tools.environments.capability_adapter import resolve_tools
from tools.environments.capability_manifest import CapabilityManifest, desktop_manifest
from tools.environments.compute_provider import ComputeLease, EnvironmentCapabilities
from tools.environments.cua_fleet import CuaFleetConfig as CuaFleetConfigImplementation
from tools.environments.cua_fleet import (
    CuaFleetDesktopProvider as CuaFleetDesktopProviderImplementation,
)
from tools.environments.cua_fleet import _provider_from_config
from tools.environments.desktop_lease import DesktopSandboxManager
from tools.environments.modal_desktop import (
    ModalDesktopConfig,
    ModalDesktopEnvironment,
    _TransportComputerBackend,
)


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
    manifest = CapabilityManifest.from_dict(
        {
            "image": "desktop:latest",
            "capabilities": {
                "terminal": True,
                "computer_use": {"service": "cua-driver"},
            },
        }
    )
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


class _ModalWorker:
    def run_coroutine(self, coroutine, timeout: int = 60):
        return asyncio.run(coroutine)


class _ModalSandbox:
    async def _tunnels(self, timeout: int):
        assert timeout == 50
        return {8080: SimpleNamespace(url="https://lease-1.modal.host")}

    def __init__(self):
        self.tunnels = SimpleNamespace(aio=self._tunnels)


def _modal_mcp_response():
    response = Mock()
    response.headers = {"mcp-session-id": "session-1"}
    response.read.side_effect = [b'{"jsonrpc":"2.0","id":1,"result":{}}', b""]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


def test_modal_sandbox_transport_uses_the_lease_tunnel() -> None:
    transport = ModalSandboxMcpTransport(
        _ModalSandbox(), _ModalWorker(), port=8080, path="/mcp"
    )

    with patch(
        "tools.computer_use.transports.modal_sandbox.urlopen",
        return_value=_modal_mcp_response(),
    ):
        transport.start()

    assert transport.endpoint == "https://lease-1.modal.host/mcp"
    assert transport.headers["mcp-session-id"] == "session-1"


def test_modal_sandbox_transport_decodes_streamable_http_sse() -> None:
    response = Mock()
    response.headers = {
        "mcp-session-id": "session-1",
        "content-type": "text/event-stream",
    }
    response.read.side_effect = [
        b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
        b"",
    ]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    transport = ModalSandboxMcpTransport(
        _ModalSandbox(), _ModalWorker(), port=8080, path="/mcp"
    )

    with patch(
        "tools.computer_use.transports.modal_sandbox.urlopen", return_value=response
    ):
        transport.start()

    assert transport.headers["mcp-session-id"] == "session-1"


def test_modal_desktop_starts_the_image_mcp_runtime_on_an_encrypted_port() -> None:
    lease = ComputeLease(
        "task-1",
        "lease-1",
        "modal",
        "registry.example/cua-driver-mcp@sha256:test",
        EnvironmentCapabilities(computer_use=True),
    )

    with patch("tools.environments.modal_desktop.ModalEnvironment.__init__") as init:
        ModalDesktopEnvironment(compute_lease=lease, config=ModalDesktopConfig())

    kwargs = init.call_args.kwargs
    assert kwargs["sandbox_command"] == ("/bin/cua-driver-mcp-runtime",)
    assert kwargs["modal_sandbox_kwargs"]["encrypted_ports"] == [8080]


def test_modal_desktop_backend_uses_the_lease_bound_transport() -> None:
    environment = object.__new__(ModalDesktopEnvironment)
    environment._computer_backend = None
    environment._desktop_config = ModalDesktopConfig()
    environment._sandbox = _ModalSandbox()
    environment._worker = _ModalWorker()

    with patch(
        "tools.computer_use.transports.modal_sandbox.urlopen",
        return_value=_modal_mcp_response(),
    ):
        backend = environment.get_computer_backend()

    assert isinstance(backend.transport, ModalSandboxMcpTransport)


def test_modal_transport_backend_forwards_app_lifecycle_actions() -> None:
    transport = Mock()
    transport.call_tool.side_effect = [
        {"structuredContent": {"pid": 42, "name": "Chromium", "windows": []}},
        {"structuredContent": {"ok": True, "message": "focused"}},
        {"structuredContent": {"ok": True, "message": "terminated"}},
    ]
    backend = _TransportComputerBackend(transport)

    launched = backend.launch_app(
        name="Chromium",
        urls=["https://example.com"],
        additional_arguments=["--incognito"],
        creates_new_application_instance=True,
    )
    focused = backend.bring_to_front(pid=42, window_id=7)
    terminated = backend.kill_app(pid=42)

    assert launched == {"pid": 42, "name": "Chromium", "windows": []}
    assert focused.ok is True
    assert terminated.ok is True
    assert transport.call_tool.call_args_list[0].args == ("launch_app", {
        "name": "Chromium",
        "urls": ["https://example.com"],
        "additional_arguments": ["--incognito"],
        "creates_new_application_instance": True,
    })
    assert transport.call_tool.call_args_list[1].args == (
        "bring_to_front", {"pid": 42, "window_id": 7},
    )
    assert transport.call_tool.call_args_list[2].args == ("kill_app", {"pid": 42})


def test_modal_transport_backend_captures_through_native_cua_window_state() -> None:
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42m"
        "NkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    transport = Mock()
    transport.call_tool.side_effect = [
        {"structuredContent": {"windows": [{
            "app_name": "Chromium", "pid": 42, "window_id": 7,
            "is_on_screen": True, "title": "Example Domain", "z_index": 1,
        }]}},
        {"structuredContent": {
            "elements": [{
                "element_index": 3, "role": "button", "label": "More information",
                "frame": {"x": 10, "y": 20, "w": 30, "h": 40},
            }],
            "screenshot_png_b64": png_b64,
            "screenshot_mime_type": "image/png",
        }},
    ]
    backend = _TransportComputerBackend(transport)

    capture = backend.capture()

    assert [call.args[0] for call in transport.call_tool.call_args_list] == [
        "list_windows", "get_window_state",
    ]
    assert transport.call_tool.call_args_list[1].args == (
        "get_window_state", {"pid": 42, "window_id": 7},
    )
    assert capture.width == 1
    assert capture.height == 1
    assert capture.png_b64 == png_b64
    assert capture.app == "Chromium"
    assert capture.window_title == "Example Domain"
    assert [(element.index, element.bounds) for element in capture.elements] == [
        (3, (10, 20, 30, 40)),
    ]


def test_modal_transport_backend_surfaces_cua_tool_errors() -> None:
    transport = Mock()
    transport.call_tool.return_value = {
        "isError": True,
        "content": [{"type": "text", "text": "Permission denied: unavailable desktop"}],
    }
    backend = _TransportComputerBackend(transport)

    with pytest.raises(RuntimeError, match="Permission denied: unavailable desktop"):
        backend.capture()

def test_modal_desktop_config_defaults() -> None:
    config = ModalDesktopConfig()

    assert config.image == "trycua/cua:latest"
    assert config.cua_driver_runtime_command == ("/bin/cua-driver-mcp-runtime",)
    assert config.cua_driver_port == 8080
    assert config.cua_driver_path == "/mcp"
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
    with pytest.raises(ValueError, match="spec.replicas"):
        CuaFleetConfig(spec={"replicas": 0})


def test_environments_package_reexports_cua_fleet_sdk() -> None:
    assert CuaFleetConfig is CuaFleetConfigImplementation
    assert CuaFleetDesktopProvider is CuaFleetDesktopProviderImplementation


class _FakeResponse:
    def __init__(self, status_code: int = 200, content: bytes | None = None):
        self.status_code = status_code
        self.content = (
            content or b'{"result":{"stdout":"ok\\n","stderr":"","returncode":0}}'
        )


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
        self.claim_specs: list[object | None] = []

    def claim(self, *, spec=None):
        self.claim_specs.append(spec)
        context = _FakeClaimContext(_FakeSandbox())
        self.claim_contexts.append(context)
        return context


class _FakeRuntimeKind(enum.Enum):
    KUBEVIRT = 0


class _FakeFirmware(enum.Enum):
    BIOS = 0
    EFI = 1


class _FakeServiceProtocol(enum.Enum):
    TCP = 0


class _FakeSandboxService:
    def __init__(
        self, *, name: str, target_port: int, protocol: _FakeServiceProtocol | None
    ):
        self.name = name
        self.target_port = target_port
        self.protocol = protocol


class _FakePreservedJson:
    def __init__(self, value: str):
        self.value = value

    @classmethod
    def from_json(cls, value: str) -> _FakePreservedJson:
        return cls(value)


class _FakeImagePullPolicy(enum.Enum):
    ALWAYS = 0
    IFNOTPRESENT = 1


class _FakeVmTemplate:
    def __init__(
        self,
        *,
        runtime: _FakeRuntimeKind | None,
        runtime_class_name: str | None,
        node_selector: dict[str, str] | None,
        tolerations: list[object] | None,
        command: list[str] | None,
        container_disk_image: str,
        image_pull_policy: _FakeImagePullPolicy | None,
        image_pull_secret: str | None,
        cpu_cores: int | None,
        memory: str | None,
        firmware: _FakeFirmware | None,
        probes: _FakePreservedJson | None,
        services: list[_FakeSandboxService] | None,
        oidc: object | None,
    ):
        self.runtime = runtime
        self.runtime_class_name = runtime_class_name
        self.node_selector = node_selector
        self.tolerations = tolerations
        self.command = command
        self.container_disk_image = container_disk_image
        self.image_pull_policy = image_pull_policy
        self.image_pull_secret = image_pull_secret
        self.cpu_cores = cpu_cores
        self.memory = memory
        self.firmware = firmware
        self.probes = probes
        self.services = services
        self.oidc = oidc


class _FakeSandboxTemplateRef:
    def __init__(self, *, name: str):
        self.name = name


class _FakeWarmPoolSpec:
    def __init__(
        self,
        *,
        replicas: int,
        sandbox_template_ref: _FakeSandboxTemplateRef,
        autoscaling: object | None,
    ):
        self.replicas = replicas
        self.sandbox_template_ref = sandbox_template_ref
        self.autoscaling = autoscaling


class _FakeTemplateSpec:
    def __init__(self, *, vm_template: _FakeVmTemplate):
        self.vm_template = vm_template


class _FakeCreatePoolRequest:
    def __init__(self, *, namespace: str, spec: _FakeWarmPoolSpec):
        self.namespace = namespace
        self.spec = spec


class _FakeCreateTemplateRequest:
    def __init__(self, *, namespace: str, name: str, spec: _FakeTemplateSpec):
        self.namespace = namespace
        self.name = name
        self.spec = spec


class _FakeClaimSpec:
    def __init__(self, *, sandbox_template_ref, warmpool, bind_deadline, lifecycle):
        self.sandbox_template_ref = sandbox_template_ref
        self.warmpool = warmpool
        self.bind_deadline = bind_deadline
        self.lifecycle = lifecycle


class _FakeSandboxApi:
    configure_calls: list[dict[str, str]] = []
    reconcile_requests: list[_FakeCreatePoolRequest] = []
    template_requests: list[_FakeCreateTemplateRequest] = []
    pool = _FakePool()
    CreatePoolRequest = _FakeCreatePoolRequest
    CreateTemplateRequest = _FakeCreateTemplateRequest
    ClaimSpec = _FakeClaimSpec
    OsGymSandboxWarmPoolSpec = _FakeWarmPoolSpec
    OsGymSandboxTemplateSpec = _FakeTemplateSpec
    VmTemplate = _FakeVmTemplate
    SandboxTemplateRef = _FakeSandboxTemplateRef
    SandboxService = _FakeSandboxService
    ServiceProtocol = _FakeServiceProtocol
    ImagePullPolicy = _FakeImagePullPolicy
    Firmware = _FakeFirmware
    RuntimeKind = _FakeRuntimeKind

    @classmethod
    def reset(cls) -> None:
        cls.configure_calls = []
        cls.reconcile_requests = []
        cls.template_requests = []
        cls.pool = _FakePool()

    @staticmethod
    def configure(**kwargs) -> None:
        _FakeSandboxApi.configure_calls.append(kwargs)

    class Pool:
        @classmethod
        async def reconcile(cls, request):
            _FakeSandboxApi.reconcile_requests.append(request)
            _FakeSandboxApi.pool.name = request.namespace
            return _FakeSandboxApi.pool

    class Template:
        @classmethod
        async def reconcile(cls, request):
            _FakeSandboxApi.template_requests.append(request)
            return SimpleNamespace(name=request.name)


def _pool_spec(*, replicas: int = 2) -> dict[str, object]:
    return {
        "replicas": replicas,
        "autoscaling": None,
        "sandbox_template_ref": {"name": None},
    }


def _template_spec(
    *,
    image: str = "registry.example/desktop:latest",
    vm_template_updates: dict[str, object] | None = None,
) -> dict[str, object]:
    vm_template = {
        "runtime": "kubevirt",
        "runtime_class_name": None,
        "node_selector": None,
        "tolerations": None,
        "command": None,
        "container_disk_image": image,
        "image_pull_policy": None,
        "image_pull_secret": "ecr-credentials",
        "cpu_cores": 4,
        "memory": "4Gi",
        "firmware": "bios",
        "probes": {"readinessProbe": {"tcpSocket": {"port": 8000}}},
        "services": [
            {"name": "server", "target_port": 8000, "protocol": "tcp"},
            {"name": "mcp", "target_port": 3000, "protocol": "tcp"},
        ],
        "oidc": None,
    }
    vm_template.update(vm_template_updates or {})
    return {"vm_template": vm_template}


def _fleet_provider(replicas: int = 2) -> CuaFleetDesktopProvider:
    _FakeSandboxApi.reset()
    return CuaFleetDesktopProvider(
        CuaFleetConfig(
            client_id="ukey-test",
            client_secret="secret",
            pool="hermes-desktop",
            spec=_pool_spec(replicas=replicas),
            template_spec=_template_spec(),
        ),
        sandbox_module=_FakeSandboxApi,
    )


def test_cua_fleet_reconciles_named_pool_with_public_sandbox_api() -> None:
    provider = _fleet_provider()

    lease = provider.acquire("task-1")

    assert _FakeSandboxApi.configure_calls == [
        {
            "fleet_base_url": "https://run.cua.ai",
            "token_url": (
                "https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token"
            ),
            "client_id": "ukey-test",
            "client_secret": "secret",
        }
    ]
    assert len(_FakeSandboxApi.reconcile_requests) == 1
    request = _FakeSandboxApi.reconcile_requests[0]
    assert isinstance(request, _FakeCreatePoolRequest)
    assert request.namespace == "hermes-desktop"
    assert request.spec.replicas == 2
    assert request.spec.autoscaling is None
    assert request.spec.sandbox_template_ref.name == "hermes-desktop-template"

    assert len(_FakeSandboxApi.template_requests) == 1
    template_request = _FakeSandboxApi.template_requests[0]
    assert isinstance(template_request, _FakeCreateTemplateRequest)
    assert template_request.namespace == "hermes-desktop"
    assert template_request.name == "hermes-desktop-template"
    vm_template = template_request.spec.vm_template
    assert vm_template.container_disk_image == "registry.example/desktop:latest"
    assert vm_template.runtime is _FakeRuntimeKind.KUBEVIRT
    assert vm_template.firmware is _FakeFirmware.BIOS
    assert vm_template.image_pull_policy is None
    assert isinstance(vm_template.probes, _FakePreservedJson)
    assert json.loads(vm_template.probes.value) == {
        "readinessProbe": {"tcpSocket": {"port": 8000}}
    }
    assert [
        (service.name, service.target_port, service.protocol)
        for service in vm_template.services
    ] == [
        ("server", 8000, _FakeServiceProtocol.TCP),
        ("mcp", 3000, _FakeServiceProtocol.TCP),
    ]
    assert lease.metadata == {"pool": "hermes-desktop", "sandbox": "sandbox-1"}
    assert _FakeSandboxApi.pool.claim_contexts[0].entered == 1

    provider.release(lease)
    assert _FakeSandboxApi.pool.claim_contexts[0].exited == 1


def test_cua_fleet_passes_native_template_request_fields_to_reconcile() -> None:
    _FakeSandboxApi.reset()
    provider = CuaFleetDesktopProvider(
        CuaFleetConfig(
            client_id="ukey-test",
            client_secret="secret",
            pool="hermes-cua-pool",
            spec=_pool_spec(),
            template_spec=_template_spec(
                image="registry.example/windows:latest",
                vm_template_updates={
                    "firmware": "efi",
                    "cpu_cores": 10,
                    "memory": "20Gi",
                    "image_pull_policy": "always",
                },
            ),
        ),
        sandbox_module=_FakeSandboxApi,
    )

    lease = provider.acquire("task-template")

    request = _FakeSandboxApi.template_requests[0]
    assert request.namespace == "hermes-cua-pool"
    assert request.name == "hermes-cua-pool-template"
    assert request.spec.vm_template.container_disk_image == (
        "registry.example/windows:latest"
    )
    assert request.spec.vm_template.firmware is _FakeFirmware.EFI
    assert request.spec.vm_template.cpu_cores == 10
    assert request.spec.vm_template.memory == "20Gi"
    assert request.spec.vm_template.image_pull_policy is _FakeImagePullPolicy.ALWAYS

    provider.release(lease)


def test_cua_fleet_reconciles_the_pool_before_its_template() -> None:
    provider = _fleet_provider()

    lease = provider.acquire("task-order")

    # The pool owns the namespace, so it has to land first.
    assert _FakeSandboxApi.pool.name == "hermes-desktop"
    assert _FakeSandboxApi.template_requests[0].namespace == "hermes-desktop"
    assert len(_FakeSandboxApi.reconcile_requests) == 1

    provider.release(lease)


def test_cua_fleet_lets_the_sdk_derive_the_claim_from_the_pool_spec() -> None:
    provider = _fleet_provider()

    lease = provider.acquire("task-claim-default")

    # A hand-built ClaimSpec used to point at the pool name, which is not a
    # template, and every claim timed out waiting to bind.
    assert _FakeSandboxApi.pool.claim_specs == [None]

    provider.release(lease)


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
    provider = CuaFleetDesktopProvider(CuaFleetConfig(), sandbox_module=_FakeSandboxApi)

    with pytest.raises(RuntimeError, match="CUA_CLIENT_ID"):
        provider.acquire("task-3")


def test_compute_config_selects_cua_fleet_provider(monkeypatch) -> None:
    monkeypatch.setenv("CUA_CLIENT_ID", "ukey-test")
    monkeypatch.setenv("CUA_CLIENT_SECRET", "secret")
    provider = _provider_from_config(
        {
            "provider": "cua_fleet",
            "cua_fleet": {
                "base_url": "https://run.cua.ai",
                "pool": "hermes-desktop",
                "spec": _pool_spec(),
                "template_spec": _template_spec(
                    image="registry.example/desktop:latest",
                    vm_template_updates={"firmware": "efi"},
                ),
            },
        }
    )

    assert isinstance(provider, CuaFleetDesktopProvider)
    assert provider.config.image == "registry.example/desktop:latest"
    assert provider.config.replicas == 2
    assert provider.config.template_name == "hermes-desktop-template"
    assert provider.config.vm_template["firmware"] == "efi"


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

    assert LAZY_DEPS["terminal.cua_fleet"] == ("cua-sandbox==0.1.25",)


def test_cua_fleet_nix_extra_matches_lazy_dependency_pin() -> None:
    from pathlib import Path
    import tomllib

    from tools.lazy_deps import LAZY_DEPS

    with (Path(__file__).parents[2] / "pyproject.toml").open("rb") as pyproject_file:
        optional_dependencies = tomllib.load(pyproject_file)["project"][
            "optional-dependencies"
        ]

    assert tuple(optional_dependencies["cua-fleet"]) == LAZY_DEPS[
        "terminal.cua_fleet"
    ]


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

    assert LAZY_DEPS["terminal.cua_fleet"] == ("cua-sandbox==0.1.25",)


def test_cua_fleet_nix_build_declares_evdev_build_system() -> None:
    from pathlib import Path

    python_nix = Path(__file__).parents[2] / "nix" / "python.nix"
    assert "evdev = prev.evdev.overrideAttrs" in python_nix.read_text()


def test_cua_fleet_nix_build_provides_evdev_kernel_headers() -> None:
    from pathlib import Path

    python_nix = Path(__file__).parents[2] / "nix" / "python.nix"
    source = python_nix.read_text()
    assert "linuxHeaders" in source
    assert "evdev = prev.evdev.overrideAttrs" in source


def test_cua_fleet_nix_build_sets_evdev_header_search_path() -> None:
    from pathlib import Path

    python_nix = Path(__file__).parents[2] / "nix" / "python.nix"
    assert "export CPATH=${linuxHeaders}/include" in python_nix.read_text()
