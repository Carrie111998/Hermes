import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
import yaml
from openai import OpenAI

from agent.agent_init import _merge_custom_provider_extra_body
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_cli.config import get_compatible_custom_providers
from hermes_cli.runtime_provider import resolve_runtime_provider
from providers import get_provider_profile


@pytest.fixture
def captured_openai_endpoint():
    captured = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            captured.append(json.loads(self.rfile.read(length)))
            body = json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "vendor-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", captured
    finally:
        server.shutdown()
        thread.join()


@pytest.mark.parametrize(
    ("configured", "tools"),
    [
        pytest.param(
            True,
            [{"type": "function", "function": {"name": "test", "parameters": {}}}],
            id="true-tools",
        ),
        pytest.param(
            False,
            [{"type": "function", "function": {"name": "test", "parameters": {}}}],
            id="false-tools",
        ),
        pytest.param(
            None,
            [{"type": "function", "function": {"name": "test", "parameters": {}}}],
            id="unset-tools",
        ),
        pytest.param(True, None, id="true-no-tools"),
        pytest.param(False, [], id="false-empty-tools"),
    ],
)
def test_temp_home_custom_provider_parallel_tool_calls_reaches_wire(
    tmp_path, monkeypatch, captured_openai_endpoint, configured, tools
):
    endpoint, captured = captured_openai_endpoint
    hermes_home = tmp_path / f"hermes-{configured}"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    provider_config = {
        "api": endpoint,
        "api_key": "test-key",
        "default_model": "vendor-model",
        "extra_body": {"include_reasoning": True},
    }
    if configured is not None:
        provider_config["parallel_tool_calls"] = configured
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "vendor", "default": "vendor-model"},
                "providers": {"vendor": provider_config},
            }
        ),
        encoding="utf-8",
    )

    custom_providers = get_compatible_custom_providers()
    resolved = resolve_runtime_provider(requested="vendor")
    agent = SimpleNamespace(
        provider=resolved["provider"],
        model=resolved.get("model", "vendor-model"),
        base_url=resolved["base_url"],
        request_overrides={},
    )
    _merge_custom_provider_extra_body(agent, custom_providers)

    kwargs = ChatCompletionsTransport().build_kwargs(
        model=agent.model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=tools,
        provider_profile=get_provider_profile("custom"),
        request_overrides=agent.request_overrides,
    )
    OpenAI(api_key=resolved["api_key"], base_url=resolved["base_url"]).chat.completions.create(
        **kwargs
    )

    assert kwargs["extra_body"]["include_reasoning"] is True
    assert len(captured) == 1
    wire_payload = captured[0]
    assert wire_payload["include_reasoning"] is True
    if configured is None:
        assert "parallel_tool_calls" not in resolved.get("request_overrides", {})
        assert "parallel_tool_calls" not in agent.request_overrides
        assert "parallel_tool_calls" not in kwargs
        assert "parallel_tool_calls" not in wire_payload
    else:
        assert resolved["request_overrides"]["parallel_tool_calls"] is configured
        assert agent.request_overrides["parallel_tool_calls"] is configured
        if tools:
            assert kwargs["parallel_tool_calls"] is configured
            assert wire_payload["parallel_tool_calls"] is configured
        else:
            assert "parallel_tool_calls" not in kwargs
            assert "parallel_tool_calls" not in wire_payload
