"""Opt-in PA business-fact bridge tools.

The bridge deliberately keeps business facts outside Hermes-owned state. It
only calls configured HTTP endpoints or local commands and returns their JSON
results to the caller.
"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from tools.registry import registry, tool_error, tool_result


DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class PABusinessOperation:
    name: str
    kind: str
    method: str = "POST"
    url: str | None = None
    command: tuple[str, ...] | None = None
    headers: Mapping[str, str] | None = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class PABusinessBridgeConfig:
    operations: Mapping[str, PABusinessOperation]


def _bridge_section(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(config, Mapping):
        return {}
    pa_section = config.get("pa")
    if isinstance(pa_section, Mapping):
        nested = pa_section.get("business")
        if isinstance(nested, Mapping):
            return nested
        for key in ("pa_business", "pa-business", "pa_business_bridge"):
            value = pa_section.get(key)
            if isinstance(value, Mapping):
                return value
    for key in ("pa_business", "pa-business", "pa_business_bridge"):
        value = config.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def load_business_bridge_config(
    config: Mapping[str, Any] | None,
) -> PABusinessBridgeConfig:
    """Parse bridge config from a Hermes-style config mapping.

    Expected shape:

        pa_business:
          operations:
            lookup_case:
              type: http
              url: http://127.0.0.1:8080/cases/lookup
              method: POST
              headers: {X-Bridge: pa}
            local_check:
              type: command
              command: [python, -c, "..."]

    Unknown or absent configuration produces an empty, inactive bridge.
    """
    section = _bridge_section(config)
    raw_operations = section.get("operations", {})
    if not isinstance(raw_operations, Mapping):
        raise ValueError("pa_business.operations must be a mapping")

    operations: dict[str, PABusinessOperation] = {}
    for name, raw in raw_operations.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"operation {name!r} must be a mapping")

        op_name = str(name)
        kind = str(raw.get("type") or raw.get("kind") or "").strip().lower()
        if kind not in {"http", "command"}:
            raise ValueError(f"operation {op_name!r} type must be 'http' or 'command'")

        timeout = float(raw.get("timeout", DEFAULT_TIMEOUT_SECONDS))
        if timeout <= 0:
            raise ValueError(f"operation {op_name!r} timeout must be positive")

        if kind == "http":
            url = str(raw.get("url") or "").strip()
            if not url:
                raise ValueError(f"operation {op_name!r} requires url")
            method = str(raw.get("method") or "POST").strip().upper()
            headers = raw.get("headers") or {}
            if not isinstance(headers, Mapping):
                raise ValueError(f"operation {op_name!r} headers must be a mapping")
            operations[op_name] = PABusinessOperation(
                name=op_name,
                kind=kind,
                method=method,
                url=url,
                headers={str(k): str(v) for k, v in headers.items()},
                timeout=timeout,
            )
            continue

        command = raw.get("command")
        if isinstance(command, str):
            command_tuple = (command,)
        elif isinstance(command, list) and all(isinstance(part, str) for part in command):
            command_tuple = tuple(command)
        else:
            raise ValueError(
                f"operation {op_name!r} command must be a string or list of strings"
            )
        if not command_tuple:
            raise ValueError(f"operation {op_name!r} requires command")
        operations[op_name] = PABusinessOperation(
            name=op_name,
            kind=kind,
            command=command_tuple,
            timeout=timeout,
        )

    return PABusinessBridgeConfig(operations=operations)


def _load_runtime_bridge_config() -> PABusinessBridgeConfig:
    try:
        from hermes_cli.config import load_config
    except Exception:
        return PABusinessBridgeConfig(operations={})
    return load_business_bridge_config(load_config())


def _bridge_available() -> bool:
    try:
        return bool(_load_runtime_bridge_config().operations)
    except Exception:
        return False


def _json_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be a mapping")
    return dict(payload)


def _parse_jsonish(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return parsed
    return {"result": parsed}


def _execute_http_operation(
    op: PABusinessOperation,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    request_payload = _json_payload(payload)
    url = op.url or ""
    data: bytes | None = None
    headers = {"Accept": "application/json", **dict(op.headers or {})}

    if op.method == "GET":
        if request_payload:
            query = urllib.parse.urlencode(request_payload, doseq=True)
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{query}"
    else:
        data = json.dumps(request_payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, headers=headers, method=op.method)
    try:
        with urllib.request.urlopen(request, timeout=op.timeout) as response:
            body = response.read().decode("utf-8")
            result = _parse_jsonish(body)
            result.setdefault("status_code", response.status)
            return result
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = _parse_jsonish(body)
        except json.JSONDecodeError:
            parsed = {"body": body}
        parsed["status_code"] = exc.code
        parsed.setdefault("error", f"HTTP {exc.code}")
        return parsed


def _execute_command_operation(
    op: PABusinessOperation,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not op.command:
        raise ValueError(f"operation {op.name!r} has no command configured")
    completed = subprocess.run(
        op.command,
        input=json.dumps(_json_payload(payload)),
        text=True,
        capture_output=True,
        timeout=op.timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"operation {op.name!r} exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return _parse_jsonish(completed.stdout)


def execute_business_operation(
    config: Mapping[str, Any] | PABusinessBridgeConfig | None,
    operation: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute a configured business operation and return its JSON-ish result."""
    bridge_config = (
        config
        if isinstance(config, PABusinessBridgeConfig)
        else load_business_bridge_config(config)
    )
    op = bridge_config.operations.get(operation)
    if op is None:
        known = ", ".join(sorted(bridge_config.operations)) or "none configured"
        raise ValueError(f"unknown PA business operation {operation!r}; known: {known}")

    if op.kind == "http":
        return _execute_http_operation(op, payload)
    if op.kind == "command":
        return _execute_command_operation(op, payload)
    raise ValueError(f"unsupported PA business operation type {op.kind!r}")


def _handle_business_read(args: Mapping[str, Any], **_kwargs: Any) -> str:
    return _handle_business_call(args)


def _handle_business_write(args: Mapping[str, Any], **_kwargs: Any) -> str:
    return _handle_business_call(args)


def _handle_business_call(args: Mapping[str, Any]) -> str:
    operation = str(args.get("operation") or "").strip()
    if not operation:
        return tool_error("operation is required")
    payload = args.get("payload") or {}
    try:
        result = execute_business_operation(
            _load_runtime_bridge_config(),
            operation=operation,
            payload=payload,
        )
    except Exception as exc:
        return tool_error(exc)
    return tool_result(result)


_PA_BUSINESS_PAYLOAD_SCHEMA = {
    "type": "object",
    "description": "JSON payload to pass through to the configured PA business operation.",
    "additionalProperties": True,
}


PA_BUSINESS_READ_SCHEMA = {
    "name": "pa_business_read",
    "description": (
        "Run an opt-in configured PA business read operation. The tool calls "
        "an external HTTP endpoint or local command and returns JSON; it does "
        "not persist business facts in Hermes state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "Configured PA business operation name.",
            },
            "payload": _PA_BUSINESS_PAYLOAD_SCHEMA,
        },
        "required": ["operation"],
    },
}


PA_BUSINESS_WRITE_SCHEMA = {
    "name": "pa_business_write",
    "description": (
        "Run an opt-in configured PA business write operation. The tool calls "
        "an external HTTP endpoint or local command and returns JSON; it does "
        "not persist business facts in Hermes state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "description": "Configured PA business operation name.",
            },
            "payload": _PA_BUSINESS_PAYLOAD_SCHEMA,
        },
        "required": ["operation"],
    },
}


registry.register(
    name="pa_business_read",
    toolset="pa-business",
    schema=PA_BUSINESS_READ_SCHEMA,
    handler=_handle_business_read,
    check_fn=_bridge_available,
)

registry.register(
    name="pa_business_write",
    toolset="pa-business",
    schema=PA_BUSINESS_WRITE_SCHEMA,
    handler=_handle_business_write,
    check_fn=_bridge_available,
)
