"""Verification tests for the temperature feature (brief verification section)."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent.chat_completion_helpers import build_api_kwargs
from agent.transports import get_transport
from agent.transports.types import NormalizedResponse
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_agent(provider="custom", model="my-model", request_overrides=None):
    """Build a minimal mock agent for build_api_kwargs."""
    from agent.transports.chat_completions import ChatCompletionsTransport

    real_transport = ChatCompletionsTransport()
    captured = {}
    from types import MethodType
    real_build = MethodType(ChatCompletionsTransport.build_kwargs, real_transport)

    def spy(**kw):
        captured.update(kw)
        return real_build(**kw)

    real_transport.build_kwargs = spy
    agent = SimpleNamespace(
        api_mode="chat_completions",
        model=model,
        base_url="https://example.com/v1",
        _base_url_lower="https://example.com/v1",
        _base_url_hostname="example.com",
        provider=provider,
        reasoning_config=None,
        request_overrides=request_overrides,
        max_tokens=1024,
        _ephemeral_max_output_tokens=None,
        _ollama_num_ctx=None,
        openrouter_min_coding_score=None,
        session_id=None,
        tools=[],
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        provider_require_parameters=False,
        provider_data_collection=False,
        _max_tokens_param=lambda n: {"max_tokens": n},
        _prepare_messages_for_non_vision_model=lambda msgs: msgs,
        _is_qwen_portal=lambda: False,
        _is_openrouter_url=lambda: False,
        _supports_reasoning_extra_body=lambda: False,
        _qwen_prepare_chat_messages=lambda *a, **k: None,
        _qwen_prepare_chat_messages_inplace=lambda *a, **k: None,
        _github_models_reasoning_extra_body=lambda: None,
        _lmstudio_reasoning_options_cached=lambda: None,
        _resolved_api_call_timeout=lambda: 30.0,
    )
    agent._get_transport = lambda: real_transport
    return agent, real_transport, captured


def _fake_load_config(temp=None):
    cfg = {"model": {}}
    if temp is not None:
        cfg["model"]["temperature"] = temp
    return cfg


class TestTransportGlobalConfig:
    def test_profile_path_temperature_from_config(self):
        agent, transport, _ = _make_agent(provider="custom", model="my-model")
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(0.7)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert kwargs["temperature"] == 0.7

    def test_legacy_path_temperature_from_config(self):
        # Unknown provider -> legacy flag path
        agent, transport, _ = _make_agent(provider="totally-unknown-provider", model="my-model")
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(0.4)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert kwargs["temperature"] == 0.4

    def test_no_config_no_override_absent(self):
        agent, transport, _ = _make_agent(provider="custom", model="my-model")
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(None)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert "temperature" not in kwargs

    def test_legacy_no_config_no_override_absent(self):
        agent, transport, _ = _make_agent(provider="totally-unknown-provider", model="my-model")
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(None)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert "temperature" not in kwargs


class TestPrecedence:
    def test_session_override_beats_global_config(self):
        # Profile path: request_overrides applied last -> wins over global config
        agent, transport, _ = _make_agent(
            provider="custom", model="my-model", request_overrides={"temperature": 1.2}
        )
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(0.3)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert kwargs["temperature"] == 1.2

    def test_legacy_session_override_beats_global_config(self):
        agent, transport, _ = _make_agent(
            provider="unknown-xyz", model="my-model", request_overrides={"temperature": 1.5}
        )
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(0.3)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert kwargs["temperature"] == 1.5

    def test_provider_contract_omits_temperature_even_with_session_override(self):
        # Kimi profile -> OMIT_TEMPERATURE contract must beat session override
        agent, transport, _ = _make_agent(provider="kimi", model="kimi-k2.5",
                                          request_overrides={"temperature": 0.9})
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(0.5)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        assert "temperature" not in kwargs

    def test_legacy_provider_contract_fixed_beats_session_override(self):
        agent, transport, _ = _make_agent(provider="unknown-xyz", model="my-model",
                                          request_overrides={"temperature": 0.9})
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(0.5)):
            kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
        # Legacy path has no fixed contract here (None), so session override applies.
        assert kwargs["temperature"] == 0.9


class TestWireProof:
    def test_temperature_reaches_wire_as_top_level_field(self):
        """Pass kwargs through the OpenAI SDK with an httpx.MockTransport that
        captures the request body; assert top-level 'temperature' present."""
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "id": "x",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "my-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        from openai import OpenAI

        client = OpenAI(api_key="test", base_url="https://example.com/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        transport = get_transport("chat_completions")
        kwargs = transport.build_kwargs(
            model="my-model",
            messages=[{"role": "user", "content": "hi"}],
            temperature=0.66,
        )
        client.chat.completions.create(**kwargs)
        assert captured["body"]["temperature"] == 0.66

    def test_no_temperature_not_on_wire(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion", "created": 1, "model": "m",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        from openai import OpenAI
        client = OpenAI(api_key="test", base_url="https://example.com/v1",
                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        transport = get_transport("chat_completions")
        kwargs = transport.build_kwargs(model="my-model", messages=[{"role": "user", "content": "hi"}])
        client.chat.completions.create(**kwargs)
        assert "temperature" not in captured["body"]


class TestCLIHandler:
    def _make_cli(self, agent=None):
        cli = SimpleNamespace(
            agent=agent,
            _session_temperature=None,
        )
        return cli

    def test_show_current_unset(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = self._make_cli(agent=None)
        with patch("hermes_cli.config.load_config", return_value=_fake_load_config(None)), \
             patch("cli._cprint") as cp, patch("cli.CLI_CONFIG", {"model": {}}):
            CLICommandsMixin._handle_temperature_command(cli, "/temperature")
        out = " ".join(str(c.args[0]) for c in cp.call_args_list)
        assert "unset" in out

    def test_set_session(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        agent = MagicMock()
        agent.request_overrides = {"speed": "fast"}
        cli = self._make_cli(agent=agent)
        with patch("cli.save_config_value") as save, patch("cli._cprint") as cp:
            CLICommandsMixin._handle_temperature_command(cli, "/temperature 0.6")
        assert cli._session_temperature == 0.6
        assert agent.request_overrides["temperature"] == 0.6
        assert agent.request_overrides["speed"] == "fast"  # preserved
        save.assert_not_called()

    def test_set_global(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = self._make_cli(agent=None)
        with patch("cli.save_config_value", return_value=True) as save, patch("cli._cprint") as cp:
            CLICommandsMixin._handle_temperature_command(cli, "/temperature 0.9 --global")
        assert cli._session_temperature == 0.9
        save.assert_called_once_with("model.temperature", 0.9)

    def test_reset(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        agent = MagicMock()
        agent.request_overrides = {"temperature": 0.8, "speed": "fast"}
        cli = self._make_cli(agent=agent)
        cli._session_temperature = 0.8
        with patch("cli.save_config_value") as save, patch("cli._cprint") as cp:
            CLICommandsMixin._handle_temperature_command(cli, "/temperature reset")
        assert cli._session_temperature is None
        assert "temperature" not in agent.request_overrides
        assert agent.request_overrides["speed"] == "fast"
        save.assert_not_called()

    def test_invalid_input(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = self._make_cli(agent=None)
        with patch("cli.save_config_value") as save, patch("cli._cprint") as cp:
            CLICommandsMixin._handle_temperature_command(cli, "/temperature abc")
        assert cli._session_temperature is None
        save.assert_not_called()

    def test_out_of_range(self):
        from hermes_cli.cli_commands_mixin import CLICommandsMixin
        cli = self._make_cli(agent=None)
        with patch("cli.save_config_value") as save, patch("cli._cprint") as cp:
            CLICommandsMixin._handle_temperature_command(cli, "/temperature 2.5")
        assert cli._session_temperature is None
        save.assert_not_called()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main(["-v", __file__]))


# ── Gateway handler tests ────────────────────────────────────────────────
def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    import gateway.run as gateway_run
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._session_temperature_overrides = {}
    runner._show_reasoning = False
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.hooks.loaded_hooks = []
    runner._session_db = None
    runner._get_or_create_gateway_honcho = lambda session_key: (None, None)
    runner._evict_cached_agent = MagicMock()
    return runner


def _make_gateway_event(text="/temperature", platform=Platform.TELEGRAM, user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


class TestGatewayTemperature:
    def test_resolve_session_temperature_prefers_override_then_global(self, tmp_path, monkeypatch):
        import gateway.run as gateway_run
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model:\n  temperature: 0.4\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        source = _make_gateway_event("/temperature").source
        session_key = runner._session_key_for_source(source)

        # No override -> global config
        assert runner._resolve_session_temperature(source=source) == 0.4

        # Session override wins over global
        runner._set_session_temperature_override(session_key, 1.1)
        assert runner._resolve_session_temperature(session_key=session_key) == 1.1

        # Clear override -> back to global
        runner._set_session_temperature_override(session_key, None)
        assert runner._resolve_session_temperature(source=source) == 0.4

    def test_resolve_session_temperature_none_when_unset(self, tmp_path, monkeypatch):
        import gateway.run as gateway_run
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model:\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        assert runner._resolve_session_temperature(source=_make_gateway_event("/temperature").source) is None

    @pytest.mark.asyncio
    async def test_handle_temperature_set_and_reset(self, tmp_path, monkeypatch):
        import gateway.run as gateway_run
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model:\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_gateway_event("/temperature 0.6")
        session_key = runner._session_key_for_source(event.source)

        result = await runner._handle_temperature_command(event)
        assert runner._session_temperature_overrides[session_key] == 0.6
        runner._evict_cached_agent.assert_called_once_with(session_key)

        # Reset
        result = await runner._handle_temperature_command(_make_gateway_event("/temperature reset"))
        assert session_key not in runner._session_temperature_overrides

    @pytest.mark.asyncio
    async def test_handle_temperature_invalid_and_out_of_range(self, tmp_path, monkeypatch):
        import gateway.run as gateway_run
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model:\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_gateway_event("/temperature abc")
        await runner._handle_temperature_command(event)
        assert "temperature" not in runner._session_temperature_overrides

        event = _make_gateway_event("/temperature 2.5")
        await runner._handle_temperature_command(event)
        assert "temperature" not in runner._session_temperature_overrides

    @pytest.mark.asyncio
    async def test_handle_temperature_global_persists_to_config(self, tmp_path, monkeypatch):
        import gateway.run as gateway_run
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("model:\n", encoding="utf-8")
        monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)

        runner = _make_runner()
        event = _make_gateway_event("/temperature 0.8 --global")
        session_key = runner._session_key_for_source(event.source)
        await runner._handle_temperature_command(event)
        # Global persists and clears the session override
        assert session_key not in runner._session_temperature_overrides
        cfg = gateway_run._load_gateway_runtime_config()
        assert cfg["model"]["temperature"] == 0.8

