"""CUA Fleet compute provider backed by the public ``cua-sandbox`` API."""

from __future__ import annotations

import asyncio
import base64
import copy
import enum
import inspect
import json
import logging
import os
import threading
import types
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence, Union, get_args, get_origin, get_type_hints

from tools.computer_use.transports.base import CuaToolTransport
from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle
from tools.environments.compute_provider import ComputeLease, EnvironmentCapabilities

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://run.cua.ai"
_DEFAULT_TOKEN_URL = (
    "https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token"
)


def _default_pool_spec() -> dict[str, Any]:
    """Native ``OSGymSandboxWarmPool`` spec; the ref name is filled per pool."""
    return {
        "replicas": 1,
        "autoscaling": None,
        "sandbox_template_ref": {"name": None},
    }


def _default_template_spec() -> dict[str, Any]:
    """Native ``OSGymSandboxTemplate`` spec; the VM shape lives here now."""
    return {
        "vm_template": {
            "runtime": "kubevirt",
            "runtime_class_name": None,
            "node_selector": None,
            "tolerations": None,
            "command": None,
            "container_disk_image": "trycua/cua:latest",
            "image_pull_policy": None,
            "image_pull_secret": "ecr-credentials",
            "cpu_cores": 2,
            "memory": "8Gi",
            "firmware": "bios",
            "probes": None,
            "services": [
                {"name": "server", "target_port": 8000, "protocol": "tcp"},
                {"name": "mcp", "target_port": 3000, "protocol": "tcp"},
            ],
            "oidc": None,
        },
    }


@dataclass(frozen=True)
class CuaFleetConfig:
    base_url: str = _DEFAULT_BASE_URL
    token_url: str = _DEFAULT_TOKEN_URL
    client_id: str = ""
    client_secret: str = ""
    pool: str = "hermes-desktop"
    spec: Mapping[str, Any] = field(default_factory=_default_pool_spec)
    template_spec: Mapping[str, Any] = field(default_factory=_default_template_spec)
    cwd: str = "/root"
    timeout: int = 60
    ready_timeout: float = 600
    request_timeout: float = 30
    bind_deadline: int | None = None

    def __post_init__(self) -> None:
        replicas = self.spec.get("replicas")
        if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
            raise ValueError("spec.replicas must be a positive integer")
        if self.bind_deadline is not None and (
            isinstance(self.bind_deadline, bool)
            or not isinstance(self.bind_deadline, int)
            or self.bind_deadline < 1
        ):
            raise ValueError("bind_deadline must be a positive integer or None")
        if "template" in self.spec or "services" in self.spec:
            raise ValueError(
                "spec.template/spec.services are gone: the VM shape now lives in "
                "template_spec.vm_template (services included)"
            )

    @property
    def replicas(self) -> int:
        return int(self.spec["replicas"])

    @property
    def template_name(self) -> str:
        """Name of the template this pool's members are stamped from."""
        ref = self.spec.get("sandbox_template_ref")
        name = ref.get("name") if isinstance(ref, Mapping) else None
        return str(name) if name else f"{self.pool}-template"

    @property
    def vm_template(self) -> Mapping[str, Any]:
        vm_template = self.template_spec.get("vm_template")
        if not isinstance(vm_template, Mapping):
            raise ValueError("template_spec.vm_template must be a mapping")
        return vm_template

    @property
    def image(self) -> str:
        image = self.vm_template.get("container_disk_image")
        if not isinstance(image, str) or not image:
            raise ValueError(
                "template_spec.vm_template.container_disk_image must be a non-empty string"
            )
        return image


@dataclass
class _FleetState:
    pool: Any
    claim_context: Any
    sandbox: Any


class _AsyncWorker:
    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coroutine: Any, timeout: float) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout=timeout)
        except BaseException:
            future.cancel()
            raise

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


class _FleetMcpTransport(CuaToolTransport):
    def __init__(self, state: _FleetState, worker: _AsyncWorker, timeout: float):
        self._state = state
        self._worker = worker
        self._timeout = timeout
        self._started = False
        self._request_id = 0
        self._session_id: str | None = None

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "hermes-agent", "version": "0.1"},
            },
        )
        # Streamable-HTTP MCP servers reject requests until the client has
        # acknowledged initialization on the negotiated session.
        self._notify("notifications/initialized")

    def stop(self) -> None:
        self._started = False
        self._session_id = None

    def is_alive(self) -> bool:
        if not self._started:
            return False
        try:
            self.list_tools()
            return True
        except Exception:
            return False

    def list_tools(self) -> list[Mapping[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": dict(arguments)})

    def _headers(self) -> dict[str, str]:
        # Spec-compliant streamable-HTTP MCP servers answer 406 unless the
        # client accepts both JSON and SSE, and 400 without the session id
        # negotiated by initialize.
        headers = {"accept": "application/json, text/event-stream"}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    def _post(self, payload: Mapping[str, Any]) -> Any:
        response = self._worker.run(
            self._state.sandbox.services.request(
                "mcp", method="POST", path="/mcp", json=payload, headers=self._headers()
            ),
            self._timeout,
        )
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        return response

    def _notify(self, method: str) -> None:
        response = self._post({"jsonrpc": "2.0", "method": method})
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"Fleet MCP notification failed with HTTP {response.status_code}"
            )

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }
        response = self._post(payload)
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"Fleet MCP request failed with HTTP {response.status_code}"
            )
        parsed = _parse_mcp_response(response.content)
        if "error" in parsed:
            error = parsed["error"]
            raise RuntimeError(
                error.get("message", str(error))
                if isinstance(error, Mapping)
                else str(error)
            )
        result = parsed.get("result", {})
        return result if isinstance(result, Mapping) else {"result": result}


def _parse_mcp_response(body: bytes) -> Mapping[str, Any]:
    text = body.decode("utf-8", errors="replace").strip()
    if text.startswith("data:") or "\ndata:" in text:
        events = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        text = events[-1] if events else "{}"
    parsed = json.loads(text or "{}")
    if not isinstance(parsed, Mapping):
        raise ValueError("Fleet MCP endpoint returned a non-object response")
    return parsed


def _parse_service_response(body: bytes) -> Mapping[str, Any]:
    """Parse computer-server JSON or its SSE ``data:`` envelope."""
    text = body.decode("utf-8", errors="replace").strip()
    data_frames = [
        line[5:].strip() for line in text.splitlines() if line.startswith("data:")
    ]
    if data_frames:
        text = data_frames[0]
    parsed = json.loads(text or "{}")
    if not isinstance(parsed, Mapping):
        raise ValueError("Fleet service returned a non-object response")
    return parsed


class CuaFleetEnvironment(BaseEnvironment):
    """One bound Fleet sandbox shared by terminal and computer-use tools."""

    _stdin_mode = "heredoc"
    _snapshot_timeout = 60

    def __init__(
        self,
        *,
        compute_lease: ComputeLease,
        state: _FleetState,
        config: CuaFleetConfig,
        worker: _AsyncWorker,
        release_callback: Any,
    ):
        self._compute_lease = compute_lease
        self._state = state
        self._fleet_config = config
        self._worker = worker
        self._release_callback = release_callback
        self._released = False
        self._computer_backend = None
        super().__init__(cwd=config.cwd, timeout=config.timeout)

    @property
    def compute_lease(self) -> ComputeLease:
        return self._compute_lease

    def get_computer_backend(self):
        if self._computer_backend is None:
            from tools.environments.modal_desktop import _TransportComputerBackend

            transport = _FleetMcpTransport(
                self._state, self._worker, self._fleet_config.request_timeout
            )
            self._computer_backend = _TransportComputerBackend(transport)
            self._computer_backend.start()
        return self._computer_backend

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ):
        command = ["bash"]
        if login:
            command.append("-l")
        command.extend(["-c", cmd_string])
        shell_command = " ".join(_shell_quote(part) for part in command)
        if stdin_data is not None:
            encoded = base64.b64encode(stdin_data.encode()).decode("ascii")
            shell_command = (
                f"printf %s {_shell_quote(encoded)} | base64 -d | {shell_command}"
            )

        def execute() -> tuple[str, int]:
            response = self._worker.run(
                self._service_json(
                    "server",
                    "/cmd",
                    {
                        "command": "run_command",
                        "params": {"command": shell_command, "timeout": timeout},
                    },
                ),
                timeout + self._fleet_config.request_timeout,
            )
            result = response.get("result", response)
            stdout = str(result.get("stdout", ""))
            stderr = str(result.get("stderr", ""))
            output = stdout + (
                ("\n" if stdout and not stdout.endswith("\n") else "") + stderr
                if stderr
                else ""
            )
            return output, int(result.get("returncode", result.get("return_code", 0)))

        return _ThreadedProcessHandle(execute)

    async def _service_json(
        self, service: str, path: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        response = await self._state.sandbox.services.request(
            service, method="POST", path=path, json=dict(payload)
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"Fleet service {service!r} failed with HTTP {response.status_code}"
            )
        return _parse_service_response(response.content)

    def cleanup(self) -> None:
        if self._computer_backend is not None:
            self._computer_backend.stop()
            self._computer_backend = None
        if not self._released:
            self._release_callback()
            self._released = True


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


def _hydrate_fleet_schema(schema_type: Any, value: Any) -> Any:
    """Construct public Fleet schema objects from their native config shape."""
    origin = get_origin(schema_type)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in get_args(schema_type):
            return None
        for option in get_args(schema_type):
            if option is not type(None):
                return _hydrate_fleet_schema(option, value)

    if origin in (list, tuple, Sequence):
        item_type = get_args(schema_type)[0] if get_args(schema_type) else Any
        return [_hydrate_fleet_schema(item_type, item) for item in value]
    if origin in (dict, Mapping):
        return value

    if inspect.isclass(schema_type):
        from_json = getattr(schema_type, "from_json", None)
        if callable(from_json):
            if isinstance(value, schema_type):
                return value
            return from_json(json.dumps(value, separators=(",", ":")))

    if inspect.isclass(schema_type) and issubclass(schema_type, enum.Enum):
        if isinstance(value, schema_type):
            return value
        if not isinstance(value, str):
            raise TypeError(f"{schema_type.__name__} must be configured by name")
        return schema_type[value.upper()]

    if inspect.isclass(schema_type) and isinstance(value, Mapping):
        fields = [
            parameter.name
            for parameter in inspect.signature(schema_type).parameters.values()
            if parameter.name != "self"
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        if fields:
            missing = [name for name in fields if name not in value]
            if missing:
                raise ValueError(
                    f"{schema_type.__name__} config is missing {sorted(missing)}"
                )
            hints = get_type_hints(schema_type.__init__)
            return schema_type(
                **{
                    name: _hydrate_fleet_schema(hints.get(name, Any), value[name])
                    for name in fields
                }
            )

    return value


def _create_pool_request(sandbox_api: Any, config: CuaFleetConfig) -> Any:
    spec = copy.deepcopy(dict(config.spec))
    ref = spec.get("sandbox_template_ref")
    spec["sandbox_template_ref"] = {
        **(dict(ref) if isinstance(ref, Mapping) else {}),
        "name": config.template_name,
    }
    return sandbox_api.CreatePoolRequest(
        namespace=config.pool,
        spec=_hydrate_fleet_schema(sandbox_api.OsGymSandboxWarmPoolSpec, spec),
    )


def _create_template_request(
    sandbox_api: Any, config: CuaFleetConfig, image: str | None
) -> Any:
    vm_template = copy.deepcopy(dict(config.vm_template))
    if image is not None:
        vm_template["container_disk_image"] = image
    spec = {**copy.deepcopy(dict(config.template_spec)), "vm_template": vm_template}
    return sandbox_api.CreateTemplateRequest(
        namespace=config.pool,
        name=config.template_name,
        spec=_hydrate_fleet_schema(sandbox_api.OsGymSandboxTemplateSpec, spec),
    )


def _claim_spec(sandbox_api: Any, pool: Any, config: CuaFleetConfig) -> Any:
    """A claim spec only when the SDK's default cannot carry the config.

    ``spec=None`` makes the SDK clone the pool's own ``sandbox_template_ref``
    into the claim, so anything hand-built has to take that ref from the pool
    resource rather than rebuild the name, or the two can drift and the claim
    binds to nothing.
    """
    if config.bind_deadline is None:
        return None
    return sandbox_api.ClaimSpec(
        sandbox_template_ref=pool.resource.spec.sandbox_template_ref,
        warmpool=None,
        bind_deadline=config.bind_deadline,
        lifecycle=None,
    )


class CuaFleetDesktopProvider:
    name = "cua_fleet"

    def __init__(
        self, config: CuaFleetConfig | None = None, *, sandbox_module: Any = None
    ):
        self.config = config or CuaFleetConfig()
        self._sandbox_module = sandbox_module
        self._states: dict[str, _FleetState] = {}
        self._workers: dict[str, _AsyncWorker] = {}
        self._lock = threading.RLock()
        self._reconcile_lock = threading.Lock()

    def acquire(
        self,
        task_id: str,
        *,
        image: str | None = None,
        capabilities: Sequence[str] | None = None,
    ) -> ComputeLease:
        enabled = EnvironmentCapabilities(computer_use=True)
        requested = frozenset(capabilities or enabled.to_capabilities())
        missing = requested - enabled.to_capabilities()
        if missing:
            raise ValueError(
                f"Cua Fleet image lacks requested capabilities: {sorted(missing)}"
            )

        client_id = self.config.client_id or os.environ.get("CUA_CLIENT_ID", "")
        client_secret = self.config.client_secret or os.environ.get(
            "CUA_CLIENT_SECRET", ""
        )
        if not client_id or not client_secret:
            raise RuntimeError(
                "Cua Fleet requires CUA_CLIENT_ID and CUA_CLIENT_SECRET (a ukey credential)"
            )

        sandbox_api = self._load_sandbox_api()
        sandbox_api.configure(
            fleet_base_url=self.config.base_url,
            token_url=self.config.token_url,
            client_id=client_id,
            client_secret=client_secret,
        )
        worker = _AsyncWorker()
        lease_id = uuid.uuid4().hex
        pool = claim_context = None
        entered_claim = False
        try:
            pool_request = _create_pool_request(sandbox_api, self.config)
            template_request = _create_template_request(sandbox_api, self.config, image)
            with self._reconcile_lock:
                # The pool owns the namespace, so it has to land before the
                # template that its members are stamped from.
                pool = worker.run(
                    sandbox_api.Pool.reconcile(pool_request),
                    self.config.request_timeout,
                )
                worker.run(
                    sandbox_api.Template.reconcile(template_request),
                    self.config.request_timeout,
                )
            claim_spec = _claim_spec(sandbox_api, pool, self.config)
            claim_context = (
                pool.claim() if claim_spec is None else pool.claim(spec=claim_spec)
            )
            sandbox = worker.run(claim_context.__aenter__(), self.config.ready_timeout)
            entered_claim = True
        except BaseException:
            if entered_claim:
                try:
                    worker.run(
                        claim_context.__aexit__(None, None, None),
                        self.config.request_timeout,
                    )
                except Exception:
                    logger.warning("Failed to roll back Fleet claim", exc_info=True)
            worker.stop()
            raise

        state = _FleetState(pool, claim_context, sandbox)
        with self._lock:
            self._states[lease_id] = state
            self._workers[lease_id] = worker
        return ComputeLease(
            task_id=task_id,
            lease_id=lease_id,
            provider=self.name,
            image=image or self.config.image,
            capabilities=enabled,
            endpoint=self.config.base_url,
            metadata={"pool": pool.name, "sandbox": sandbox.name},
        )

    def create_environment(self, lease: ComputeLease) -> CuaFleetEnvironment:
        with self._lock:
            state = self._states.get(lease.lease_id)
            worker = self._workers.get(lease.lease_id)
        if state is None or worker is None:
            raise RuntimeError(f"Unknown Cua Fleet lease {lease.lease_id}")
        config = self.config
        if lease.image != config.image:
            config = replace(
                config,
                template_spec={
                    **config.template_spec,
                    "vm_template": {
                        **config.vm_template,
                        "container_disk_image": lease.image,
                    },
                },
            )
        return CuaFleetEnvironment(
            compute_lease=lease,
            state=state,
            config=config,
            worker=worker,
            release_callback=lambda: self.release(lease),
        )

    def release(self, lease: ComputeLease) -> None:
        with self._lock:
            state = self._states.get(lease.lease_id)
            worker = self._workers.get(lease.lease_id)
        if state is None or worker is None:
            return
        worker.run(
            state.claim_context.__aexit__(None, None, None),
            self.config.request_timeout,
        )
        with self._lock:
            self._states.pop(lease.lease_id, None)
            self._workers.pop(lease.lease_id, None)
        worker.stop()

    def _load_sandbox_api(self) -> Any:
        if self._sandbox_module is not None:
            return self._sandbox_module
        try:
            from tools.lazy_deps import ensure

            ensure("terminal.cua_fleet", prompt=False)
            import cua_sandbox
        except Exception as exc:
            raise ImportError(f"CUA Sandbox API unavailable: {exc}") from exc
        self._sandbox_module = cua_sandbox
        return self._sandbox_module


def _provider_from_config(compute_config: Mapping[str, Any] | None = None) -> Any:
    config = dict(compute_config or {})
    provider_name = str(config.get("provider") or "modal").lower()
    if provider_name == "cua_fleet":
        fleet = dict(config.get("cua_fleet") or {})
        allowed = CuaFleetConfig.__dataclass_fields__
        return CuaFleetDesktopProvider(
            CuaFleetConfig(
                **{key: value for key, value in fleet.items() if key in allowed}
            )
        )
    if provider_name == "modal":
        from tools.environments.modal_desktop import (
            ModalDesktopConfig,
            ModalDesktopProvider,
        )

        modal = dict(config.get("modal") or {})
        modal["image"] = str(
            config.get("image") or modal.get("image") or ModalDesktopConfig.image
        )
        allowed = ModalDesktopConfig.__dataclass_fields__
        return ModalDesktopProvider(
            ModalDesktopConfig(
                **{key: value for key, value in modal.items() if key in allowed}
            )
        )
    raise ValueError(f"Unsupported compute provider: {provider_name}")
