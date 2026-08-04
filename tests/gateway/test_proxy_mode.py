"""Tests for gateway proxy mode — forwarding messages to a remote API server."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, StreamingConfig
from gateway.platforms.base import resolve_proxy_url
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner(proxy_url=None):
    """Create a minimal GatewayRunner for proxy tests."""
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.streaming = StreamingConfig()
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    return runner


def _make_source(platform=Platform.MATRIX):
    return SessionSource(
        platform=platform,
        chat_id="!room:server.org",
        chat_name="Test Room",
        chat_type="group",
        user_id="@user:server.org",
        user_name="testuser",
        thread_id=None,
    )


class _FakeSSEResponse:
    """Simulates an aiohttp response with SSE streaming."""

    def __init__(self, status=200, sse_chunks=None, error_text=""):
        self.status = status
        self._sse_chunks = sse_chunks or []
        self._error_text = error_text
        self.content = self

    async def text(self):
        return self._error_text

    async def iter_any(self):
        for chunk in self._sse_chunks:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _FakeSession:
    """Simulates an aiohttp.ClientSession with captured request args."""

    def __init__(self, response):
        self._response = response
        self.captured_url = None
        self.captured_json = None
        self.captured_headers = None

    def post(self, url, json=None, headers=None, **kwargs):
        self.captured_url = url
        self.captured_json = json
        self.captured_headers = headers
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _patch_aiohttp(session):
    """Patch aiohttp.ClientSession to return our fake session."""
    return patch(
        "aiohttp.ClientSession",
        return_value=session,
    )


class TestGetProxyUrl:
    """Test _get_proxy_url() config resolution."""

    def test_returns_none_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("GATEWAY_PROXY_URL", raising=False)
        runner = _make_runner()
        with patch("gateway.run._load_gateway_config", return_value={}):
            assert runner._get_proxy_url() is None


    def test_reads_from_config_yaml(self, monkeypatch):
        monkeypatch.delenv("GATEWAY_PROXY_URL", raising=False)
        runner = _make_runner()
        cfg = {"gateway": {"proxy_url": "http://10.0.0.1:8642"}}
        with patch("gateway.run._load_gateway_config", return_value=cfg):
            assert runner._get_proxy_url() == "http://10.0.0.1:8642"


class TestResolveProxyUrl:

    def test_no_proxy_bypasses_matching_host(self, monkeypatch):
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                    "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
        monkeypatch.setenv("NO_PROXY", "api.telegram.org")

        assert resolve_proxy_url(target_hosts="api.telegram.org") is None

    def test_no_proxy_bypasses_cidr_target(self, monkeypatch):
        for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                    "https_proxy", "http_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
        monkeypatch.setenv("NO_PROXY", "149.154.160.0/20")

        assert resolve_proxy_url(target_hosts=["149.154.167.220"]) is None


class TestRunAgentProxyDispatch:
    """Test that _run_agent() delegates to proxy when configured."""

    @pytest.mark.asyncio
    async def test_run_agent_delegates_to_proxy(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
        runner = _make_runner()
        source = _make_source()

        expected_result = {
            "final_response": "Hello from remote!",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "Hello from remote!"},
            ],
            "api_calls": 1,
            "tools": [],
        }

        runner._run_agent_via_proxy = AsyncMock(return_value=expected_result)

        result = await runner._run_agent(
            message="hi",
            context_prompt="",
            history=[],
            source=source,
            session_id="test-session-123",
            session_key="test-key",
            run_generation=7,
        )

        assert result["final_response"] == "Hello from remote!"
        runner._run_agent_via_proxy.assert_called_once()
        assert runner._run_agent_via_proxy.call_args.kwargs["run_generation"] == 7


class TestRunAgentViaProxy:
    """Test the actual proxy HTTP forwarding logic."""

    @pytest.mark.asyncio
    async def test_builds_correct_request(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
        monkeypatch.setenv("GATEWAY_PROXY_KEY", "test-key-123")
        runner = _make_runner()
        source = _make_source()

        resp = _FakeSSEResponse(
            status=200,
            sse_chunks=[
                'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":" world"}}]}\n\n'
                "data: [DONE]\n\n"
            ],
        )
        session = _FakeSession(resp)

        with patch("gateway.run._load_gateway_config", return_value={}):
            with _patch_aiohttp(session):
                with patch("aiohttp.ClientTimeout"):
                    result = await runner._run_agent_via_proxy(
                        message="How are you?",
                        context_prompt="You are helpful.",
                        history=[
                            {"role": "user", "content": "Hello"},
                            {"role": "assistant", "content": "Hi there!"},
                        ],
                        source=source,
                        session_id="session-abc",
                    )

        # Verify request URL
        assert session.captured_url == "http://host:8642/v1/chat/completions"

        # Verify auth header
        assert session.captured_headers["Authorization"] == "Bearer test-key-123"

        # Verify session ID header
        assert session.captured_headers["X-Hermes-Session-Id"] == "session-abc"

        # Verify messages include system, history, and current message
        messages = session.captured_json["messages"]
        assert messages[0] == {"role": "system", "content": "You are helpful."}
        assert messages[1] == {"role": "user", "content": "Hello"}
        assert messages[2] == {"role": "assistant", "content": "Hi there!"}
        assert messages[3] == {"role": "user", "content": "How are you?"}

        # Verify streaming is requested
        assert session.captured_json["stream"] is True

        # Verify response was assembled
        assert result["final_response"] == "Hello world"


    @pytest.mark.asyncio
    async def test_handles_connection_error(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://unreachable:8642")
        monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
        runner = _make_runner()
        source = _make_source()

        class _ErrorSession:
            def post(self, *args, **kwargs):
                raise ConnectionError("Connection refused")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        with patch("gateway.run._load_gateway_config", return_value={}):
            with patch("aiohttp.ClientSession", return_value=_ErrorSession()):
                with patch("aiohttp.ClientTimeout"):
                    result = await runner._run_agent_via_proxy(
                        message="hi",
                        context_prompt="",
                        history=[],
                        source=source,
                        session_id="test",
                    )

        assert "Proxy connection error" in result["final_response"]


    @pytest.mark.asyncio
    async def test_no_system_message_when_context_empty(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
        monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
        runner = _make_runner()
        source = _make_source()

        resp = _FakeSSEResponse(
            status=200,
            sse_chunks=[b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'],
        )
        session = _FakeSession(resp)

        with patch("gateway.run._load_gateway_config", return_value={}):
            with _patch_aiohttp(session):
                with patch("aiohttp.ClientTimeout"):
                    await runner._run_agent_via_proxy(
                        message="hello",
                        context_prompt="",
                        history=[],
                        source=source,
                        session_id="test",
                    )

        # No system message should appear when context_prompt is empty
        messages = session.captured_json["messages"]
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "hello"


class TestEnvVarRegistration:
    """Verify GATEWAY_PROXY_URL and GATEWAY_PROXY_KEY are registered."""

    def test_proxy_url_in_optional_env_vars(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS
        assert "GATEWAY_PROXY_URL" in OPTIONAL_ENV_VARS
        info = OPTIONAL_ENV_VARS["GATEWAY_PROXY_URL"]
        assert info["category"] == "messaging"
        assert info["password"] is False


# channel_toolsets: local FG resolution + proxy fail-closed on match

_SSE_OK = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'


def _make_discord_source(chat_id="100", parent_chat_id=None):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_name="test-channel",
        chat_type="channel",
        user_id="user-1",
        user_name="tester",
        parent_chat_id=parent_chat_id,
    )


class TestLocalForegroundSourceIds:
    @pytest.mark.asyncio
    async def test_run_agent_passes_source_ids_to_platform_tools(self, monkeypatch):
        import sys
        import threading
        import types
        from types import SimpleNamespace
        import gateway.run as gateway_run

        captured = {}
        runtime = {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
        }

        class _CapturingAgent:
            def __init__(self, *a, **kw):
                captured["enabled_toolsets"] = kw.get("enabled_toolsets")
                self.tools = []
                self.model = kw.get("model", "test-model")
                self.provider = kw.get("provider", "test")
                self.session_id = kw.get("session_id", "s1")
                self.context_compressor = None
                self.is_interrupted = False

            def run_conversation(self, *a, **k):
                return {
                    "final_response": "ok",
                    "messages": [],
                    "api_calls": 1,
                    "completed": True,
                }

            def shutdown_memory_provider(self, *a, **k):
                pass

            def close(self):
                pass

        fake = types.ModuleType("run_agent")
        fake.AIAgent = _CapturingAgent
        monkeypatch.setitem(sys.modules, "run_agent", fake)

        def fake_gpt(config, platform, **kwargs):
            captured.update(
                platform=platform,
                chat_id=kwargs.get("chat_id"),
                parent_id=kwargs.get("parent_id"),
            )
            return {"web", "memory"}

        runner = _make_runner()
        for attr, val in {
            "_ephemeral_system_prompt": "",
            "_prefill_messages": [],
            "_reasoning_config": None,
            "_service_tier": None,
            "_provider_routing": {},
            "_fallback_model": None,
            "_pending_model_notes": {},
            "_session_db": None,
            "_agent_cache_lock": threading.Lock(),
            "hooks": SimpleNamespace(loaded_hooks=False),
            "session_store": SimpleNamespace(
                get_or_create_session=lambda source: SimpleNamespace(
                    session_id="session-1"
                ),
                load_transcript=lambda session_id: [],
            ),
            "_get_or_create_gateway_honcho": lambda session_key: (None, None),
            "_gateway_loop": None,
        }.items():
            setattr(runner, attr, val)

        monkeypatch.delenv("GATEWAY_PROXY_URL", raising=False)
        monkeypatch.setattr(gateway_run.GatewayRunner, "_get_proxy_url", lambda s: None)
        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
        monkeypatch.setattr("hermes_cli.tools_config._get_platform_tools", fake_gpt)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: runtime)
        monkeypatch.setattr(
            gateway_run, "_resolve_gateway_model", lambda config=None: "test-model"
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner, "_adapter_for_source", lambda s, src: None
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner,
            "_resolve_session_agent_runtime",
            lambda s, **kw: ("test-model", runtime),
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner,
            "_resolve_session_reasoning_config",
            lambda s, **kw: None,
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner,
            "_resolve_session_service_tier",
            lambda s, **kw: None,
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner,
            "_resolve_turn_agent_config",
            lambda s, msg, model, runtime: {
                "model": model,
                "runtime": runtime,
                "request_overrides": None,
            },
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner, "_refresh_fallback_model", lambda s: None
        )
        monkeypatch.setattr(
            gateway_run.GatewayRunner, "_cleanup_agent_resources", lambda s, a: None
        )

        source = _make_discord_source(chat_id="100", parent_chat_id="parent-1")
        result = await runner._run_agent(
            message="hi",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key="agent:main:discord:channel:100",
        )
        assert result["final_response"] == "ok"
        assert captured == {
            "platform": "discord",
            "chat_id": "100",
            "parent_id": "parent-1",
            "enabled_toolsets": ["memory", "web"],
        }


class TestProxyChannelToolsets:
    @pytest.mark.asyncio
    async def test_unmatched_proxy_body_unchanged(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
        monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
        session = _FakeSession(_FakeSSEResponse(status=200, sse_chunks=[_SSE_OK]))
        with patch("gateway.run._load_gateway_config", return_value={}), _patch_aiohttp(
            session
        ), patch("aiohttp.ClientTimeout"):
            await _make_runner()._run_agent_via_proxy(
                message="hello",
                context_prompt="",
                history=[],
                source=_make_discord_source(chat_id="999"),
                session_id="test",
            )
        assert "enabled_toolsets" not in session.captured_json
        assert session.captured_json["model"] == "hermes-agent"
        assert session.captured_json["stream"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("toolsets", [[], ["web"]])
    async def test_matched_proxy_fails_closed_before_network(
        self, monkeypatch, toolsets
    ):
        monkeypatch.setenv("GATEWAY_PROXY_URL", "http://host:8642")
        monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
        session = _FakeSession(_FakeSSEResponse(status=200, sse_chunks=[_SSE_OK]))
        cfg = {"discord": {"channel_toolsets": [{"id": "100", "toolsets": toolsets}]}}
        with patch("gateway.run._load_gateway_config", return_value=cfg), _patch_aiohttp(
            session
        ), patch("aiohttp.ClientTimeout"):
            with pytest.raises(RuntimeError, match="channel_toolsets"):
                await _make_runner()._run_agent_via_proxy(
                    message="hello",
                    context_prompt="",
                    history=[],
                    source=_make_discord_source(chat_id="100"),
                    session_id="test",
                )
        assert session.captured_json is None
