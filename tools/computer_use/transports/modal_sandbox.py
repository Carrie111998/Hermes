"""Stateful HTTP transport for a CUA Driver running in a Modal sandbox."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping
from urllib.request import Request, urlopen

from agent.monitoring.modal_lifecycle import record as record_modal_lifecycle
from agent.monitoring.modal_lifecycle import sandbox_id as modal_sandbox_id
from tools.computer_use.transports.http_mcp import HttpMcpTransport


class ModalSandboxMcpTransport(HttpMcpTransport):
    """Connect a desktop lease to its CUA Driver MCP tunnel."""

    def __init__(
        self,
        sandbox: Any,
        worker: Any,
        *,
        task_id: str | None = None,
        lease_id: str | None = None,
        image: str | None = None,
        port: int,
        path: str = "/mcp",
        timeout: float = 30,
        tunnel_timeout: int = 50,
    ):
        super().__init__("", timeout=timeout)
        self._sandbox = sandbox
        self._worker = worker
        self._port = port
        self._task_id = task_id
        self._lease_id = lease_id
        self._image = image
        self._path = "/" + path.lstrip("/")
        self._tunnel_timeout = tunnel_timeout

    def start(self) -> None:
        if self._started:
            return
        self.endpoint = self._resolve_endpoint()
        self._started = True
        self._request(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "hermes-agent", "version": "0.1"},
            },
        )
        self._post({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        })

    def _resolve_endpoint(self) -> str:
        started = time.monotonic()
        try:
            async def resolve() -> str:
                tunnels = await self._sandbox.tunnels.aio(timeout=self._tunnel_timeout)
                tunnel = tunnels.get(self._port)
                if tunnel is None:
                    raise RuntimeError(
                        f"Modal sandbox did not expose CUA Driver port {self._port}"
                    )
                return tunnel.url.rstrip("/") + self._path

            endpoint = self._worker.run_coroutine(resolve(), timeout=self._tunnel_timeout + 5)
        except Exception as exc:
            record_modal_lifecycle(
                "mcp.tunnel", task_id=self._task_id, lease_id=self._lease_id,
                sandbox_id=modal_sandbox_id(self._sandbox), image=self._image,
                duration_ms=(time.monotonic() - started) * 1000, error=exc,
            )
            raise
        record_modal_lifecycle(
            "mcp.tunnel", task_id=self._task_id, lease_id=self._lease_id,
            sandbox_id=modal_sandbox_id(self._sandbox), image=self._image,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        return endpoint

    def _post(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        started = time.monotonic()
        succeeded = False
        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **self.headers,
            }
            request = Request(self.endpoint, data=data, headers=headers, method="POST")
            with urlopen(request, timeout=self.timeout) as response:
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self.headers["mcp-session-id"] = session_id
                content_type = response.headers.get("Content-Type", response.headers.get("content-type", ""))
                body = response.read().decode("utf-8")
            if not body:
                return {}
            if "text/event-stream" in content_type.lower():
                body = "\n".join(
                    line[5:].lstrip() for line in body.splitlines() if line.startswith("data:")
                )
                if not body:
                    return {}
            parsed = json.loads(body)
            if not isinstance(parsed, Mapping):
                raise ValueError("MCP endpoint returned a non-object response")
            succeeded = True
            return parsed
        except Exception as exc:
            record_modal_lifecycle(
                "mcp.request", task_id=self._task_id, lease_id=self._lease_id,
                sandbox_id=modal_sandbox_id(self._sandbox), image=self._image,
                duration_ms=(time.monotonic() - started) * 1000, error=exc,
            )
            raise
        finally:
            if succeeded:
                record_modal_lifecycle(
                    "mcp.request", task_id=self._task_id, lease_id=self._lease_id,
                    sandbox_id=modal_sandbox_id(self._sandbox), image=self._image,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
