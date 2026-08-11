from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from openai import BadRequestError

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    _api_request_profile,
)


BOUNDARY = "hermes-structured-no-tools-no-memory-v1"
CAPABILITIES = {
    "agent_loop": False,
    "memory_access": False,
    "session_history": False,
    "tool_execution": False,
}
SCHEMA = {
    "type": "object",
    "$defs": {"boolean_flag": {"type": "boolean"}},
    "properties": {"relevant": {"$ref": "#/$defs/boolean_flag"}},
    "required": ["relevant"],
    "additionalProperties": False,
}

CRYPTO_CLASSIFICATION_SCHEMA = {
    "$defs": {
        "CryptoCatalystCitation": {
            "additionalProperties": False,
            "properties": {
                "claim": {
                    "maxLength": 160,
                    "minLength": 1,
                    "title": "Claim",
                    "type": "string",
                },
                "excerpt": {
                    "maxLength": 500,
                    "minLength": 1,
                    "title": "Excerpt",
                    "type": "string",
                },
            },
            "required": ["claim", "excerpt"],
            "title": "CryptoCatalystCitation",
            "type": "object",
        }
    },
    "additionalProperties": False,
    "properties": {
        "citations": {
            "items": {"$ref": "#/$defs/CryptoCatalystCitation"},
            "maxItems": 5,
            "title": "Citations",
            "type": "array",
        },
        "confidence": {
            "maximum": 1,
            "minimum": 0,
            "title": "Confidence",
            "type": "number",
        },
        "direction": {
            "enum": ["positive", "negative", "neutral", "unclear"],
            "title": "Direction",
            "type": "string",
        },
        "materiality": {
            "maximum": 1,
            "minimum": 0,
            "title": "Materiality",
            "type": "number",
        },
        "rationale": {
            "default": "",
            "maxLength": 2_000,
            "title": "Rationale",
            "type": "string",
        },
        "relevant": {"title": "Relevant", "type": "boolean"},
    },
    "required": ["relevant", "direction", "confidence", "materiality"],
    "title": "CryptoCatalystClassification",
    "type": "object",
}


def _adapter(
    *,
    key: str = "test-api-key",
    upstream_key: str = "upstream-secret-must-not-leak",
) -> APIServerAdapter:
    return APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": key,
                "model_routes": {
                    "pinned-model": {
                        "model": "pinned-model",
                        "provider": "pinned-provider",
                        "base_url": "https://provider.example/v1",
                        "api_key": upstream_key,
                    }
                },
            },
        )
    )


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    for method, path, handler in adapter._http_route_table():
        if path in {"/v1/capabilities", "/v1/inference/structured"}:
            app.router.add_route(method, path, handler)
    return app


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer test-api-key",
        "Content-Type": "application/json",
    }


def _request_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "pinned-model",
        "prompt": "Classify only the supplied record.",
        "json_schema": SCHEMA,
        "schema_name": "classification",
        "purpose": "trading_bot_test",
        "max_output_tokens": 400,
    }
    body.update(overrides)
    return body


class _FakeCompletions:
    def __init__(
        self,
        content: str,
        *,
        model: str = "pinned-model",
        system_fingerprint: str | None = "provider-build-7",
    ) -> None:
        self.content = content
        self.model = model
        self.system_fingerprint = system_fingerprint
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(
            model=self.model,
            system_fingerprint=self.system_fingerprint,
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=23,
                completion_tokens=7,
                total_tokens=30,
            ),
        )


class _FakeClient:
    def __init__(
        self,
        completions: _FakeCompletions,
        *,
        base_url: str = "https://provider.example/v1",
    ) -> None:
        self.base_url = base_url
        self.chat = SimpleNamespace(completions=completions)


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    completions: _FakeCompletions,
    *,
    upstream_keys: frozenset[str] = frozenset({"upstream-secret-must-not-leak"}),
) -> None:
    def resolve_runtime_provider(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["requested"] == "pinned-provider"
        assert kwargs["explicit_api_key"] in upstream_keys
        assert kwargs["explicit_base_url"] == "https://provider.example/v1"
        assert kwargs["target_model"] == "pinned-model"
        return {
            "provider": "pinned-provider",
            "requested_provider": "pinned-provider",
            "base_url": "https://provider.example/v1",
            "api_key": kwargs["explicit_api_key"],
            "api_mode": "chat_completions",
            "source": "explicit",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_cached_client",
        lambda provider, model, **kwargs: (
            _FakeClient(completions),
            "pinned-model",
        ),
    )


@pytest.mark.asyncio
async def test_structured_inference_is_authenticated_and_discoverable() -> None:
    adapter = _adapter()
    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        unauthenticated = await client.post(
            "/v1/inference/structured",
            json=_request_body(),
        )
        assert unauthenticated.status == 401

        response = await client.get("/v1/capabilities", headers=_headers())
        assert response.status == 200
        payload = await response.json()

    assert payload["features"]["structured_inference"] is True
    assert payload["endpoints"]["structured_inference"] == {
        "method": "POST",
        "path": "/v1/inference/structured",
    }
    assert payload["structured_inference"]["boundary"] == BOUNDARY
    assert payload["structured_inference"]["capabilities"] == CAPABILITIES

    unauthenticated_adapter = _adapter(key="")
    unauthenticated_app = _app(unauthenticated_adapter)
    async with TestClient(TestServer(unauthenticated_app)) as client:
        unavailable = await client.post(
            "/v1/inference/structured",
            headers={"Content-Type": "application/json"},
            json=_request_body(),
        )
    assert unavailable.status == 503


@pytest.mark.asyncio
async def test_structured_inference_uses_one_tool_free_host_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent import auxiliary_client
    from agent.aux_accounting import (
        reset_accounting_context,
        set_accounting_context,
    )
    from agent.portal_tags import (
        reset_conversation_context,
        set_conversation_context,
    )

    adapter = _adapter()
    completions = _FakeCompletions('{"relevant":false}')
    _patch_runtime(monkeypatch, completions)
    monkeypatch.setattr(
        adapter,
        "_create_agent",
        lambda *args, **kwargs: pytest.fail("structured inference created an agent"),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm",
        lambda *args, **kwargs: pytest.fail("fallback-capable call_llm path used"),
    )

    real_build_call_kwargs = auxiliary_client._build_call_kwargs

    def build_call_kwargs_with_ambient_session(*args: Any, **kwargs: Any) -> dict[str, Any]:
        call = real_build_call_kwargs(*args, **kwargs)
        call["session_id"] = "ambient-session-must-not-leak"
        extra = dict(call.get("extra_body") or {})
        extra.update({
            "response_format": {"type": "json_object"},
            "session_id": "ambient-session-must-not-leak",
            "tags": ["product=hermes-agent", "conversation=ambient-session-must-not-leak"],
        })
        call["extra_body"] = extra
        return call

    monkeypatch.setattr(
        auxiliary_client,
        "_build_call_kwargs",
        build_call_kwargs_with_ambient_session,
    )

    class _AccountingDb:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def record_auxiliary_usage(self, *args: Any, **kwargs: Any) -> None:
            self.calls.append((args, kwargs))

    accounting_db = _AccountingDb()
    accounting_token = set_accounting_context(accounting_db, "ambient-session")
    conversation_token = set_conversation_context("ambient-session")

    app = _app(adapter)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/inference/structured",
                headers=_headers(),
                json=_request_body(),
            )
            assert response.status == 200, await response.text()
            payload = await response.json()
    finally:
        reset_conversation_context(conversation_token)
        reset_accounting_context(accounting_token)

    assert payload["boundary"] == BOUNDARY
    assert payload["capabilities"] == CAPABILITIES
    assert payload["output"] == {"relevant": False}
    assert payload["model"] == "pinned-model"
    assert payload["provider"] == "pinned-provider"
    assert payload["revision_quality"] == "provider_fingerprint"
    assert payload["backend_revision"].startswith("provider-fingerprint-sha256:")
    assert payload["usage"] == {
        "input_tokens": 23,
        "output_tokens": 7,
        "total_tokens": 30,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert len(completions.calls) == 1
    call = completions.calls[0]
    assert "tools" not in call
    assert "session" not in call
    assert "history" not in call
    assert "memory" not in call
    assert "ambient-session-must-not-leak" not in json.dumps(call)
    assert accounting_db.calls == []
    assert [message["role"] for message in call["messages"]] == ["system", "user"]
    assert all(message["role"] not in {"assistant", "tool"} for message in call["messages"])
    assert call.get("max_tokens") == 400 or call.get("max_completion_tokens") == 400
    response_format = call["extra_body"]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "classification"
    assert response_format["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_structured_inference_rejects_named_profile_before_listener_route_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True)
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda *, multiplex: [("default", object()), ("coder", object())],
    )
    monkeypatch.setattr(adapter, "_profile_scope", lambda _profile: nullcontext())
    monkeypatch.setattr(
        adapter,
        "_expected_api_key",
        lambda: (
            "coder-profile-api-key"
            if _api_request_profile.get() == "coder"
            else "test-api-key"
        ),
    )
    resolver_calls: list[str] = []

    def listener_owned_resolver(model: str) -> dict[str, Any]:
        resolver_calls.append(model)
        raise AssertionError("named profile reached listener-owned model routes")

    monkeypatch.setattr(adapter, "_resolve_structured_runtime", listener_owned_resolver)

    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    app.router.add_post(
        "/p/{profile}/v1/inference/structured",
        adapter._handle_structured_inference,
    )
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/p/coder/v1/inference/structured",
            headers={
                "Authorization": "Bearer coder-profile-api-key",
                "Content-Type": "application/json",
            },
            json=_request_body(),
        )
        payload = await response.json()

    assert response.status == 409
    assert payload["error"]["code"] == "structured_profile_unsupported"
    assert resolver_calls == []


def test_structured_schema_allowlist_accepts_crypto_classification_shape() -> None:
    validator = _adapter()._structured_schema_validator(
        CRYPTO_CLASSIFICATION_SCHEMA
    )

    assert not list(
        validator.iter_errors(
            {
                "relevant": True,
                "direction": "positive",
                "confidence": 0.8,
                "materiality": 0.6,
                "rationale": "Protocol release is economically relevant.",
                "citations": [
                    {
                        "claim": "The protocol shipped a release.",
                        "excerpt": "Release v2 is now live.",
                    }
                ],
            }
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_schema",
    [
        {
            "type": "object",
            "properties": {
                "value": {"type": "string", "pattern": "(a+)+$"}
            },
        },
        {
            "type": "object",
            "patternProperties": {"(a+)+$": {"type": "string"}},
        },
        {
            "type": "object",
            "properties": {"value": {"type": "string", "format": "regex"}},
        },
        {
            "type": "object",
            "properties": {
                "values": {"type": "array", "uniqueItems": True}
            },
        },
        {
            "type": "object",
            "properties": {
                "values": {
                    "type": "array",
                    "contains": {"type": "string"},
                }
            },
        },
        {
            "type": "object",
            "if": {"properties": {"flag": {"const": True}}},
            "then": {"required": ["value"]},
        },
    ],
    ids=[
        "pattern",
        "pattern-properties",
        "format-checker",
        "unique-items",
        "contains",
        "conditional",
    ],
)
async def test_structured_inference_rejects_unsafe_schema_keywords_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
    unsafe_schema: dict[str, Any],
) -> None:
    adapter = _adapter()
    resolver_calls: list[str] = []

    def resolver(model: str) -> dict[str, Any]:
        resolver_calls.append(model)
        raise AssertionError("unsafe schema reached provider resolution")

    monkeypatch.setattr(adapter, "_resolve_structured_runtime", resolver)
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(json_schema=unsafe_schema),
        )
        payload = await response.json()

    assert response.status == 400
    assert payload["error"]["code"] == "invalid_json_schema"
    assert resolver_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_mode", "expected_status"),
    [
        pytest.param("valid", 200, id="valid-terminal-usage"),
        pytest.param("missing", 502, id="missing-terminal-usage"),
        pytest.param("above-cap", 502, id="above-cap-terminal-usage"),
    ],
)
async def test_structured_inference_pins_model_and_omits_codex_unsupported_output_field(
    monkeypatch: pytest.MonkeyPatch,
    terminal_mode: str,
    expected_status: int,
) -> None:
    from agent import auxiliary_client
    from agent.codex_responses_adapter import _preflight_codex_api_kwargs

    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "test-api-key"})
    )

    class _FakeEventStream:
        def __init__(self) -> None:
            self.closed = False
            events = [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "message"},
                },
                {
                    "type": "response.output_text.delta",
                    "delta": '{"relevant":true}',
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"relevant":true}',
                            }
                        ],
                    },
                },
            ]
            if terminal_mode != "missing":
                output_tokens = 401 if terminal_mode == "above-cap" else 7
                events.append({
                    "type": "response.completed",
                    "response": {
                        "id": "response_test",
                        "status": "completed",
                        "usage": {
                            "input_tokens": 23,
                            "output_tokens": output_tokens,
                            "total_tokens": 23 + output_tokens,
                        },
                    },
                })
            self._events = iter(events)

        def __iter__(self) -> _FakeEventStream:
            return self

        def __next__(self) -> dict[str, Any]:
            return next(self._events)

        def close(self) -> None:
            self.closed = True

    class _FakeResponses:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.last_stream: _FakeEventStream | None = None

        def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if "max_output_tokens" in kwargs:
                request = httpx.Request(
                    "POST",
                    "https://chatgpt.com/backend-api/codex/responses",
                )
                response = httpx.Response(400, request=request)
                raise BadRequestError(
                    "Unsupported parameter: max_output_tokens",
                    response=response,
                    body={"detail": "Unsupported parameter: max_output_tokens"},
                )
            self.last_stream = _FakeEventStream()
            return self.last_stream

    class _FakeRawCodexClient:
        def __init__(self) -> None:
            self.api_key = "oauth-token-must-not-leak"
            self.base_url = "https://chatgpt.com/backend-api/codex"
            self.responses = _FakeResponses()

        def close(self) -> None:
            return None

    raw_client = _FakeRawCodexClient()
    codex_client = auxiliary_client.AsyncCodexAuxiliaryClient(
        auxiliary_client.CodexAuxiliaryClient(
            raw_client,
            "gpt-5.6-terra",
        )
    )
    normalized_calls: list[dict[str, Any]] = []

    def recording_preflight(api_kwargs: Any, **kwargs: Any) -> dict[str, Any]:
        normalized = _preflight_codex_api_kwargs(api_kwargs, **kwargs)
        normalized_calls.append(normalized)
        return normalized

    monkeypatch.setattr(
        "agent.codex_responses_adapter._preflight_codex_api_kwargs",
        recording_preflight,
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider._get_model_config",
        lambda: {"default": "gpt-5.6-terra"},
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_requested_provider",
        lambda requested=None: "openai-codex",
    )

    def resolve_runtime_provider(**kwargs: Any) -> dict[str, Any]:
        assert kwargs == {
            "requested": "openai-codex",
            "explicit_api_key": None,
            "explicit_base_url": None,
            "target_model": "gpt-5.6-terra",
        }
        return {
            "provider": "openai-codex",
            "requested_provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "oauth-token-must-not-leak",
            "api_mode": "codex_responses",
            "source": "hermes-auth-store",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_cached_client",
        lambda provider, model, **kwargs: (
            codex_client,
            "gpt-5.6-terra",
        ),
    )

    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(model="gpt-5.6-terra"),
        )
        payload = await response.json()

    assert response.status == expected_status
    if expected_status == 200:
        assert payload["model"] == "gpt-5.6-terra"
        assert payload["provider"] == "openai-codex"
    else:
        assert payload["error"]["code"] == "structured_output_invalid"
    assert "oauth-token-must-not-leak" not in json.dumps(payload)
    assert len(normalized_calls) == 1
    assert "max_output_tokens" not in normalized_calls[0]
    assert len(raw_client.responses.calls) == 1
    codex_request = raw_client.responses.calls[0]
    assert "max_output_tokens" not in codex_request
    assert "max_completion_tokens" not in codex_request
    assert codex_request["stream"] is True
    assert raw_client.responses.last_stream is not None
    assert raw_client.responses.last_stream.closed is True


def test_structured_codex_stream_aborts_before_accumulating_over_bound() -> None:
    from gateway.platforms.api_server import (
        _StructuredCodexEventStream,
        _StructuredOutputLimitExceeded,
    )

    class _Source:
        def __init__(self) -> None:
            self.closed = False
            self.yielded = 0
            self._events = iter([
                {"type": "response.output_text.delta", "delta": "1234"},
                {"type": "response.reasoning_summary_text.delta", "delta": "56789"},
                {"type": "response.output_text.delta", "delta": "not-consumed"},
            ])

        def __iter__(self) -> _Source:
            return self

        def __next__(self) -> dict[str, Any]:
            event = next(self._events)
            self.yielded += 1
            return event

        def close(self) -> None:
            self.closed = True

    source = _Source()
    stream = _StructuredCodexEventStream(
        source,
        max_payload_bytes=8,
        max_output_tokens=400,
    )

    assert next(stream)["delta"] == "1234"
    with pytest.raises(_StructuredOutputLimitExceeded):
        next(stream)

    assert source.yielded == 2
    assert source.closed is True


@pytest.mark.parametrize(
    ("events", "max_output_tokens"),
    [
        pytest.param(
            [{"type": "response.output_text.delta", "delta": "{}"}],
            4,
            id="missing-completed-event",
        ),
        pytest.param(
            [
                {
                    "type": "response.completed",
                    "response": {"usage": {}},
                }
            ],
            4,
            id="completed-without-output-usage",
        ),
        pytest.param(
            [
                {
                    "type": "response.completed",
                    "response": {"usage": {"output_tokens": 5}},
                }
            ],
            4,
            id="above-cap-output-usage",
        ),
        pytest.param(
            [
                {
                    "type": "response.completed",
                    "response": {"usage": {"output_tokens": -1}},
                }
            ],
            4,
            id="negative-output-usage",
        ),
    ],
)
def test_structured_codex_stream_rejects_invalid_terminal_usage(
    events: list[dict[str, Any]],
    max_output_tokens: int,
) -> None:
    from gateway.platforms.api_server import (
        _StructuredCodexEventStream,
        _StructuredCodexTerminalUsageError,
    )

    source = iter(events)
    stream = _StructuredCodexEventStream(
        source,
        max_payload_bytes=1_000,
        max_output_tokens=max_output_tokens,
    )

    with pytest.raises(_StructuredCodexTerminalUsageError):
        list(stream)


def test_structured_codex_stream_carries_explicit_zero_terminal_usage() -> None:
    from gateway.platforms.api_server import _StructuredCodexEventStream

    stream = _StructuredCodexEventStream(
        iter([
            {
                "type": "response.completed",
                "response": {"usage": {"output_tokens": 0}},
            }
        ]),
        max_payload_bytes=1_000,
        max_output_tokens=4,
    )

    assert len(list(stream)) == 1
    assert stream.terminal_output_tokens == 0


@pytest.mark.asyncio
async def test_structured_timeout_cancels_call_and_releases_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _adapter()
    adapter._max_concurrent_runs = 1
    calls = 0
    cancellations = 0

    monkeypatch.setattr(
        adapter,
        "_resolve_structured_runtime",
        lambda model: {
            "public_model": model,
            "model": model,
            "provider": "pinned-provider",
            "api_mode": "chat_completions",
            "base_url": "https://provider.example/v1",
            "api_key": "upstream-secret-must-not-leak",
            "route_source": "model_route",
        },
    )

    async def blocked_completion(**kwargs: Any) -> None:
        nonlocal calls, cancellations
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancellations += 1

    monkeypatch.setattr(adapter, "_run_structured_completion", blocked_completion)
    monkeypatch.setattr(
        "gateway.platforms.api_server.STRUCTURED_INFERENCE_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        "gateway.platforms.api_server.STRUCTURED_INFERENCE_TIMEOUT_GRACE_SECONDS",
        0.0,
    )

    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        first = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(),
        )
        assert first.status == 502
        assert adapter.active_agent_work_count() == 0

        second = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(),
        )
        assert second.status == 502

    assert calls == 2
    assert cancellations == 2
    assert adapter.active_agent_work_count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("api_mode", ["anthropic_messages", "bedrock_converse"])
async def test_structured_inference_rejects_sync_only_transports_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
    api_mode: str,
) -> None:
    adapter = _adapter()
    client_resolution_calls: list[str] = []

    def resolve_runtime_provider(**kwargs: Any) -> dict[str, Any]:
        return {
            "provider": "pinned-provider",
            "requested_provider": "pinned-provider",
            "base_url": "https://provider.example/v1",
            "api_key": kwargs["explicit_api_key"],
            "api_mode": api_mode,
            "source": "explicit",
        }

    def get_cached_client(*args: Any, **kwargs: Any) -> tuple[None, None]:
        client_resolution_calls.append(api_mode)
        return None, None

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        resolve_runtime_provider,
    )
    monkeypatch.setattr(
        "agent.auxiliary_client._get_cached_client",
        get_cached_client,
    )

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(),
        )
        payload = await response.json()

    assert response.status == 409
    assert payload["error"]["code"] == "structured_model_not_pinned"
    assert client_resolution_calls == []


def test_provider_fingerprint_revision_also_binds_non_secret_route_configuration() -> None:
    adapter = _adapter()
    base_runtime = {
        "public_model": "pinned-model",
        "model": "pinned-model",
        "provider": "pinned-provider",
        "api_mode": "chat_completions",
        "base_url": "https://provider.example/v1",
        "route_source": "model_routes",
        "api_key": "first-secret-must-not-affect-revision",
    }

    first_revision, first_quality = adapter._structured_revision(
        base_runtime,
        provider_fingerprint="provider-build-7",
    )
    rotated_secret_revision, rotated_quality = adapter._structured_revision(
        {
            **base_runtime,
            "api_key": "rotated-secret-must-not-affect-revision",
        },
        provider_fingerprint="provider-build-7",
    )
    different_route_revision, different_route_quality = adapter._structured_revision(
        {
            **base_runtime,
            "base_url": "https://provider-alt.example/v1",
        },
        provider_fingerprint="provider-build-7",
    )

    assert first_quality == rotated_quality == different_route_quality == "provider_fingerprint"
    assert first_revision.startswith("provider-fingerprint-sha256:")
    assert first_revision == rotated_secret_revision
    assert first_revision != different_route_revision
    assert first_revision != (
        "provider-fingerprint-sha256:"
        + hashlib.sha256(b"provider-build-7").hexdigest()
    )


@pytest.mark.asyncio
async def test_structured_inference_configuration_revision_is_stable_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream_keys = frozenset({
        "upstream-secret-must-not-leak",
        "rotated-upstream-secret-must-not-leak",
    })
    adapters = [
        _adapter(upstream_key=upstream_key)
        for upstream_key in sorted(upstream_keys)
    ]
    completions = _FakeCompletions(
        '{"relevant":true}',
        system_fingerprint=None,
    )
    _patch_runtime(monkeypatch, completions, upstream_keys=upstream_keys)

    responses = []
    payloads = []
    for adapter in adapters:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/inference/structured",
                headers=_headers(),
                json=_request_body(),
            )
            payload = await response.json()
        responses.append(response)
        payloads.append(payload)

    assert all(response.status == 200 for response in responses)
    revisions = {payload["backend_revision"] for payload in payloads}
    assert len(revisions) == 1
    revision = revisions.pop()
    assert revision.startswith("configuration-sha256:")
    assert all(payload["revision_quality"] == "configuration_only" for payload in payloads)
    assert "upstream-secret-must-not-leak" not in json.dumps(payloads)
    assert "rotated-upstream-secret-must-not-leak" not in json.dumps(payloads)


@pytest.mark.asyncio
async def test_structured_inference_does_not_retry_or_fallback_provider_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingCompletions(_FakeCompletions):
        async def create(self, **kwargs: Any) -> Any:
            self.calls.append(kwargs)
            raise RuntimeError("synthetic provider failure")

    adapter = _adapter()
    completions = _FailingCompletions("")
    _patch_runtime(monkeypatch, completions)
    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm",
        lambda *args, **kwargs: pytest.fail("fallback-capable call_llm path used"),
    )

    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(),
        )
        payload = await response.json()

    assert response.status == 502
    assert payload["error"]["code"] == "structured_inference_failed"
    assert len(completions.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected_status"),
    [
        ({}, 400),
        (_request_body(tools=[]), 400),
        (_request_body(model="unconfigured-model"), 409),
        (_request_body(max_output_tokens=0), 400),
        (_request_body(prompt="x" * 196_609), 413),
        (
            _request_body(
                json_schema={
                    "type": "object",
                    "properties": {"value": {"$ref": "https://example.test/schema"}},
                }
            ),
            400,
        ),
    ],
    ids=[
        "missing-fields",
        "unknown-tools-field",
        "model-not-pinned",
        "invalid-output-bound",
        "prompt-too-large",
        "external-schema-ref",
    ],
)
async def test_structured_inference_rejects_unbounded_or_steerable_requests(
    body: dict[str, Any],
    expected_status: int,
) -> None:
    adapter = _adapter()
    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=body,
        )
    assert response.status == expected_status


@pytest.mark.asyncio
async def test_structured_inference_enforces_request_schema_and_output_byte_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gateway.platforms.api_server as api_server

    adapter = _adapter()
    app = _app(adapter)
    with monkeypatch.context() as scoped:
        scoped.setattr(api_server, "STRUCTURED_MAX_REQUEST_BYTES", 32)
        async with TestClient(TestServer(app)) as client:
            request_too_large = await client.post(
                "/v1/inference/structured",
                headers=_headers(),
                json=_request_body(),
            )
    assert request_too_large.status == 413

    with monkeypatch.context() as scoped:
        scoped.setattr(api_server, "STRUCTURED_MAX_SCHEMA_BYTES", 16)
        async with TestClient(TestServer(_app(_adapter()))) as client:
            schema_too_large = await client.post(
                "/v1/inference/structured",
                headers=_headers(),
                json=_request_body(),
            )
    assert schema_too_large.status == 400

    completions = _FakeCompletions('{"relevant":false}')
    _patch_runtime(monkeypatch, completions)
    with monkeypatch.context() as scoped:
        scoped.setattr(api_server, "STRUCTURED_MAX_OUTPUT_BYTES", 8)
        async with TestClient(TestServer(_app(_adapter()))) as client:
            output_too_large = await client.post(
                "/v1/inference/structured",
                headers=_headers(),
                json=_request_body(),
            )
            output_payload = await output_too_large.json()
    assert output_too_large.status == 502
    assert output_payload["error"]["code"] == "structured_output_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "served_model"),
    [
        ('{"relevant":"not-a-boolean"}', "pinned-model"),
        ('{"relevant":true,"relevant":false}', "pinned-model"),
        ('[{"relevant":true}]', "pinned-model"),
        ('{"relevant":true}', "different-model"),
    ],
    ids=[
        "schema-mismatch",
        "duplicate-output-key",
        "non-object-output",
        "served-model-drift",
    ],
)
async def test_structured_inference_fails_closed_on_output_or_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    served_model: str,
) -> None:
    adapter = _adapter()
    completions = _FakeCompletions(content, model=served_model)
    _patch_runtime(monkeypatch, completions)

    app = _app(adapter)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/v1/inference/structured",
            headers=_headers(),
            json=_request_body(),
        )
        payload = await response.json()

    assert response.status == 502
    assert payload["error"]["code"] in {
        "structured_output_invalid",
        "structured_identity_drift",
    }
