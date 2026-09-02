"""Ollama ``/api/chat`` adapter for local context-window fidelity."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit

import httpx


def should_use_native_ollama(agent: Any) -> bool:
    if getattr(agent, "api_mode", None) != "chat_completions":
        return False
    if not getattr(agent, "_ollama_num_ctx", None):
        return False
    try:
        from agent.model_metadata import detect_local_server_type, is_local_endpoint

        base_url = str(getattr(agent, "base_url", "") or "")
        if not is_local_endpoint(base_url):
            return False
        key = getattr(agent, "api_key", "")
        return detect_local_server_type(
            base_url, api_key=key if isinstance(key, str) else ""
        ) == "ollama"
    except Exception:
        return False


def _native_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}/api/chat", "", ""))


def _native_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    copied = dict(call)
    function = dict(copied.get("function") or {})
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            function["arguments"] = json.loads(arguments)
        except (TypeError, ValueError):
            function["arguments"] = {}
    copied["function"] = function
    copied.pop("id", None)
    copied.pop("type", None)
    return copied


def build_native_payload(api_kwargs: dict[str, Any]) -> dict[str, Any]:
    extra_body = api_kwargs.get("extra_body") or {}
    options = dict(extra_body.get("options") or {})
    max_tokens = api_kwargs.get("max_completion_tokens", api_kwargs.get("max_tokens"))
    if max_tokens is not None:
        options["num_predict"] = int(max_tokens)
    if api_kwargs.get("temperature") is not None:
        options["temperature"] = api_kwargs["temperature"]

    messages = []
    for message in api_kwargs.get("messages") or []:
        copied = {
            key: value
            for key, value in message.items()
            if key in {"role", "content", "images", "tool_calls", "tool_name"}
        }
        if isinstance(copied.get("tool_calls"), list):
            copied["tool_calls"] = [_native_tool_call(call) for call in copied["tool_calls"]]
        messages.append(copied)

    payload: dict[str, Any] = {
        "model": api_kwargs["model"],
        "messages": messages,
        "stream": bool(api_kwargs.get("stream")),
        "options": options,
    }
    if api_kwargs.get("tools"):
        payload["tools"] = api_kwargs["tools"]
    if "think" in extra_body:
        payload["think"] = extra_body["think"]
    elif api_kwargs.get("reasoning_effort") is not None:
        payload["think"] = str(api_kwargs["reasoning_effort"]).lower() != "none"
    return payload


def _tool_calls(value: Any) -> list[Any] | None:
    if not isinstance(value, list):
        return None
    calls = []
    for index, item in enumerate(value):
        function = dict((item or {}).get("function") or {})
        arguments = function.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        calls.append(
            SimpleNamespace(
                index=index,
                id=(item or {}).get("id") or f"call_{index}",
                type="function",
                function=SimpleNamespace(
                    name=function.get("name") or "", arguments=arguments
                ),
            )
        )
    return calls


def _usage(data: dict[str, Any]) -> Any:
    prompt = int(data.get("prompt_eval_count") or 0)
    completion = int(data.get("eval_count") or 0)
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def _response(data: dict[str, Any]) -> Any:
    message = data.get("message") or {}
    assistant = SimpleNamespace(
        role=message.get("role") or "assistant",
        content=message.get("content"),
        tool_calls=_tool_calls(message.get("tool_calls")),
        reasoning_content=message.get("thinking"),
    )
    return SimpleNamespace(
        id="ollama-native",
        model=data.get("model"),
        choices=[
            SimpleNamespace(
                index=0,
                message=assistant,
                finish_reason=data.get("done_reason") or "stop",
            )
        ],
        usage=_usage(data),
    )


def _chunk(data: dict[str, Any]) -> Any:
    message = data.get("message") or {}
    delta = SimpleNamespace(
        role=message.get("role"),
        content=message.get("content"),
        tool_calls=_tool_calls(message.get("tool_calls")),
        reasoning_content=message.get("thinking"),
    )
    return SimpleNamespace(
        id="ollama-native",
        model=data.get("model"),
        choices=[
            SimpleNamespace(
                index=0,
                delta=delta,
                finish_reason=(
                    data.get("done_reason") if data.get("done") else None
                ),
            )
        ],
        usage=_usage(data) if data.get("done") else None,
    )


class OllamaNativeStream:
    def __init__(self, client: httpx.Client, context: Any, response: httpx.Response):
        self._client = client
        self._context = context
        self.response = response

    def __iter__(self) -> Iterator[Any]:
        for line in self.response.iter_lines():
            if line:
                data = json.loads(line)
                yield _chunk(data)
                if data.get("done"):
                    yield SimpleNamespace(
                        id="ollama-native",
                        model=data.get("model"),
                        choices=[],
                        usage=_usage(data),
                    )

    def close(self) -> None:
        try:
            self._context.__exit__(None, None, None)
        finally:
            self._client.close()


def create_native_ollama_chat(agent: Any, api_kwargs: dict[str, Any]) -> Any:
    headers = dict(api_kwargs.get("extra_headers") or {})
    api_key = getattr(agent, "api_key", "")
    if isinstance(api_key, str) and api_key and api_key != "ollama":
        headers.setdefault("Authorization", f"Bearer {api_key}")
    # A loopback Ollama request must never inherit a desktop/system proxy. Apart
    # from leaking a local payload to the proxy, macOS proxy discovery can turn
    # a healthy localhost request into a synthetic 502.
    client = httpx.Client(
        timeout=api_kwargs.get("timeout"), headers=headers, trust_env=False
    )
    payload = build_native_payload(api_kwargs)
    from agent.message_observability import log_message_shape

    log_message_shape(
        logging.getLogger(__name__),
        "ollama_native_provider_payload",
        payload["messages"],
    )
    url = _native_url(str(agent.base_url))
    if payload["stream"]:
        context = client.stream("POST", url, json=payload)
        response = context.__enter__()
        response.raise_for_status()
        return OllamaNativeStream(client, context, response)
    try:
        response = client.post(url, json=payload)
        response.raise_for_status()
        return _response(response.json())
    finally:
        client.close()
