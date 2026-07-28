"""Cua Fleet compute provider backed by the public ``cua-fleet`` SDK."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from tools.computer_use.transports.base import CuaToolTransport
from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle
from tools.environments.compute_provider import ComputeLease, EnvironmentCapabilities

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://run.cua.ai"
_DEFAULT_TOKEN_URL = (
    "https://auth.cua.ai/realms/cyclops-cs/protocol/openid-connect/token"
)


@dataclass(frozen=True)
class CuaFleetConfig:
    base_url: str = _DEFAULT_BASE_URL
    token_url: str = _DEFAULT_TOKEN_URL
    client_id: str = ""
    client_secret: str = ""
    image: str = "trycua/cua:latest"
    image_pull_secret: str = "ecr-credentials"
    pool_prefix: str = "hermes"
    cwd: str = "/root"
    timeout: int = 60
    cpu: int | None = 2
    memory: str | None = "8192Mi"
    ready_timeout: float = 600
    ready_poll_interval: float = 5
    request_timeout: float = 30
    services: Mapping[str, int] = field(
        default_factory=lambda: {"server": 8000, "mcp": 3000}
    )


@dataclass
class _FleetState:
    client: Any
    http_client: Any
    pool: Any
    claim: Any
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


class _UrlLibHttpClient:
    """Build the SDK's HttpClient callback without depending on cua-sandbox."""

    def __init__(self, sdk: Any, timeout: float, allowed_hosts: Sequence[str]):
        self._sdk = sdk
        self._timeout = timeout
        self._allowed_hosts = frozenset(allowed_hosts)

    async def execute(self, request: Any) -> Any:
        return await asyncio.to_thread(self._execute, request)

    def _execute(self, request: Any) -> Any:
        parsed = urllib.parse.urlparse(request.url)
        if parsed.scheme != "https" or parsed.hostname not in self._allowed_hosts:
            raise ValueError(
                f"Cua Fleet SDK attempted a request to an unexpected URL: {request.url!r}"
            )
        native = urllib.request.Request(
            request.url,
            data=request.body,
            method=request.method,
            headers={header.name: header.value for header in request.headers},
        )
        try:
            with urllib.request.urlopen(native, timeout=self._timeout) as response:
                return self._response(
                    response.status, response.headers.items(), response.read()
                )
        except urllib.error.HTTPError as error:
            return self._response(error.code, error.headers.items(), error.read())

    def _response(self, status: int, headers: Any, body: bytes) -> Any:
        return self._sdk.HttpResponse(
            status=status,
            headers=[
                self._sdk.HttpHeader(name=name, value=value) for name, value in headers
            ],
            body=body,
        )


class _FleetMcpTransport(CuaToolTransport):
    def __init__(
        self, state: _FleetState, sdk: Any, worker: _AsyncWorker, timeout: float
    ):
        self._state = state
        self._sdk = sdk
        self._worker = worker
        self._timeout = timeout
        self._started = False
        self._request_id = 0

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

    def stop(self) -> None:
        self._started = False

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

    def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        self._request_id += 1
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        ).encode()
        response = self._worker.run(
            self._state.client.service_request(
                self._state.sandbox,
                "mcp",
                "/mcp",
                self._sdk.HttpRequest(
                    method="POST",
                    url="https://service.invalid/mcp",
                    headers=[
                        self._sdk.HttpHeader(
                            name="accept", value="application/json, text/event-stream"
                        ),
                        self._sdk.HttpHeader(
                            name="content-type", value="application/json"
                        ),
                    ],
                    body=payload,
                ),
            ),
            self._timeout,
        )
        if not 200 <= response.status < 300:
            raise RuntimeError(f"Fleet MCP request failed with HTTP {response.status}")
        parsed = _parse_mcp_response(response.body)
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


class CuaFleetEnvironment(BaseEnvironment):
    """One bound Fleet sandbox shared by terminal and computer-use tools."""

    _stdin_mode = "heredoc"
    _snapshot_timeout = 60

    def __init__(
        self,
        *,
        compute_lease: ComputeLease,
        state: _FleetState,
        sdk: Any,
        config: CuaFleetConfig,
        worker: _AsyncWorker,
        release_callback: Any,
    ):
        self._compute_lease = compute_lease
        self._state = state
        self._sdk = sdk
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
                self._state, self._sdk, self._worker, self._fleet_config.request_timeout
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
        response = await self._state.client.service_request(
            self._state.sandbox,
            service,
            path,
            self._sdk.HttpRequest(
                method="POST",
                url=f"https://service.invalid{path}",
                headers=[
                    self._sdk.HttpHeader(name="content-type", value="application/json")
                ],
                body=json.dumps(payload, separators=(",", ":")).encode(),
            ),
        )
        if not 200 <= response.status < 300:
            raise RuntimeError(
                f"Fleet service {service!r} failed with HTTP {response.status}"
            )
        parsed = json.loads(response.body.decode("utf-8", errors="replace") or "{}")
        if not isinstance(parsed, Mapping):
            raise ValueError(
                f"Fleet service {service!r} returned a non-object response"
            )
        return parsed

    def cleanup(self) -> None:
        if self._computer_backend is not None:
            self._computer_backend.stop()
            self._computer_backend = None
        if not self._released:
            self._released = True
            self._release_callback()


def _shell_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


class CuaFleetDesktopProvider:
    name = "cua_fleet"

    def __init__(self, config: CuaFleetConfig | None = None, *, sdk_module: Any = None):
        self.config = config or CuaFleetConfig()
        self._sdk = sdk_module
        self._states: dict[str, _FleetState] = {}
        self._workers: dict[str, _AsyncWorker] = {}
        self._lock = threading.RLock()

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

        sdk = self._load_sdk()
        worker = _AsyncWorker()
        lease_id = uuid.uuid4().hex
        namespace = _resource_name(self.config.pool_prefix, task_id, lease_id)
        http_client_type = type(
            "FleetHttpClient", (sdk.HttpClient, _UrlLibHttpClient), {}
        )
        allowed_hosts = {
            urllib.parse.urlparse(self.config.base_url).hostname,
            urllib.parse.urlparse(self.config.token_url).hostname,
        }
        http_client = http_client_type(
            sdk,
            self.config.request_timeout,
            tuple(host for host in allowed_hosts if host),
        )
        client = sdk.CyclopsClient.connect(
            sdk.CyclopsConfiguration(
                base_url=self.config.base_url,
                token_url=self.config.token_url,
                credentials=sdk.CyclopsCredentials(client_id, client_secret),
                pool_poll_interval_ms=max(
                    1, int(self.config.ready_poll_interval * 1000)
                ),
                pool_poll_limit=max(
                    1,
                    int(
                        self.config.ready_timeout
                        / max(self.config.ready_poll_interval, 0.001)
                    ),
                ),
                claim_poll_interval_ms=max(
                    1, int(self.config.ready_poll_interval * 1000)
                ),
                claim_poll_limit=max(
                    1,
                    int(
                        self.config.ready_timeout
                        / max(self.config.ready_poll_interval, 0.001)
                    ),
                ),
            ),
            http_client,
        )
        pool = claim = None
        try:
            pool = worker.run(
                client.create_pool(
                    self._pool_request(sdk, namespace, image or self.config.image)
                ),
                self.config.request_timeout,
            )
            pool = self._wait_pool(worker, client, pool)
            claim = worker.run(
                client.create_claim(sdk.CreateClaimRequest(pool=pool, spec=None)),
                self.config.request_timeout,
            )
            sandbox = worker.run(client.wait_claim(claim), self.config.ready_timeout)
        except BaseException:
            if claim is not None:
                try:
                    worker.run(client.delete_claim(claim), self.config.request_timeout)
                except Exception:
                    logger.warning("Failed to roll back Fleet claim", exc_info=True)
            if pool is not None:
                try:
                    worker.run(client.delete_pool(pool), self.config.request_timeout)
                except Exception:
                    logger.warning("Failed to roll back Fleet pool", exc_info=True)
            worker.stop()
            raise

        state = _FleetState(client, http_client, pool, claim, sandbox)
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
            metadata={
                "namespace": pool.metadata.namespace,
                "pool": pool.metadata.name,
                "claim": claim.metadata.name,
                "sandbox": sandbox.name,
            },
        )

    def create_environment(self, lease: ComputeLease) -> CuaFleetEnvironment:
        with self._lock:
            state = self._states.get(lease.lease_id)
            worker = self._workers.get(lease.lease_id)
        if state is None or worker is None:
            raise RuntimeError(f"Unknown Cua Fleet lease {lease.lease_id}")
        config = self.config
        if lease.image != config.image:
            config = CuaFleetConfig(**{
                field_name: lease.image
                if field_name == "image"
                else getattr(config, field_name)
                for field_name in CuaFleetConfig.__dataclass_fields__
            })
        return CuaFleetEnvironment(
            compute_lease=lease,
            state=state,
            sdk=self._load_sdk(),
            config=config,
            worker=worker,
            release_callback=lambda: self.release(lease),
        )

    def release(self, lease: ComputeLease) -> None:
        with self._lock:
            state = self._states.pop(lease.lease_id, None)
            worker = self._workers.pop(lease.lease_id, None)
        if state is None or worker is None:
            return
        error = None
        try:
            worker.run(
                state.client.delete_claim(state.claim), self.config.request_timeout
            )
        except Exception as exc:
            error = exc
        try:
            # cua-fleet owns the coupled pool + namespace teardown here.
            worker.run(
                state.client.delete_pool(state.pool), self.config.request_timeout
            )
        except Exception as exc:
            error = error or exc
        finally:
            worker.stop()
        if error is not None:
            raise error

    def _load_sdk(self) -> Any:
        if self._sdk is not None:
            return self._sdk
        try:
            from tools.lazy_deps import ensure

            ensure("terminal.cua_fleet", prompt=False)
            import cyclops_sdk
        except Exception as exc:
            raise ImportError(f"Cua Fleet SDK unavailable: {exc}") from exc
        self._sdk = cyclops_sdk
        return self._sdk

    def _pool_request(self, sdk: Any, namespace: str, image: str) -> Any:
        services = [
            sdk.SandboxService(name=name, target_port=port, protocol=None)
            for name, port in self.config.services.items()
        ]
        probes = None
        if hasattr(sdk, "PreservedJson") and "server" in self.config.services:
            probes = sdk.PreservedJson.from_json(
                json.dumps({
                    "readinessProbe": {
                        "tcpSocket": {"port": self.config.services["server"]}
                    }
                })
            )
        return sdk.CreatePoolRequest(
            namespace=namespace,
            spec=sdk.PoolSpec(
                replicas=1,
                services=services,
                template=sdk.PoolTemplate(
                    runtime=None,
                    runtime_class_name=None,
                    node_selector=None,
                    tolerations=None,
                    command=None,
                    container_disk_image=image,
                    image_pull_secret=self.config.image_pull_secret or None,
                    cpu_cores=self.config.cpu,
                    memory=self.config.memory,
                    firmware=None,
                    probes=probes,
                    oidc=None,
                ),
                autoscaling=None,
            ),
        )

    def _wait_pool(self, worker: _AsyncWorker, client: Any, pool: Any) -> Any:
        deadline = time.monotonic() + self.config.ready_timeout
        while True:
            current = worker.run(client.get_pool(pool), self.config.request_timeout)
            status = getattr(current, "status", None)
            if status is not None and (getattr(status, "available_count", 0) or 0) >= 1:
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Fleet pool {pool.metadata.name!r}"
                )
            time.sleep(self.config.ready_poll_interval)


def _resource_name(prefix: str, task_id: str, lease_id: str) -> str:
    value = re.sub(
        r"[^a-z0-9-]+", "-", f"{prefix}-{task_id}-{lease_id[:8]}".lower()
    ).strip("-")
    return value[:63].rstrip("-") or f"hermes-{lease_id[:8]}"


def _provider_from_config(compute_config: Mapping[str, Any] | None = None) -> Any:
    config = dict(compute_config or {})
    provider_name = str(config.get("provider") or "modal").lower()
    if provider_name == "cua_fleet":
        fleet = dict(config.get("cua_fleet") or {})
        fleet["image"] = str(
            config.get("image") or fleet.get("image") or CuaFleetConfig.image
        )
        allowed = CuaFleetConfig.__dataclass_fields__
        return CuaFleetDesktopProvider(
            CuaFleetConfig(**{
                key: value for key, value in fleet.items() if key in allowed
            })
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
            ModalDesktopConfig(**{
                key: value for key, value in modal.items() if key in allowed
            })
        )
    raise ValueError(f"Unsupported compute provider: {provider_name}")
