"""Mandatory provider preflight coverage for auxiliary LLM egress."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import hermes_cli.plugins as plugins_mod
from agent.auxiliary_client import (
    _create_with_progress,
    _relay_async_completion,
    _relay_sync_completion,
    _relay_sync_stream,
)
from hermes_cli.plugins import MandatoryHookError, PluginManager


@pytest.fixture
def mandatory_guard_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / "auxiliary-guard"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "auxiliary-guard", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "import json\n\n"
        "def _guard(**kw):\n"
        "    body = kw.get('request', {}).get('body', {})\n"
        "    if body.get('stream') is True or "
        "'MAGNON_PRIVATE' in json.dumps(body, default=str):\n"
        "        return {'action': 'block', 'reason': 'private payload'}\n"
        "    return {'action': 'allow'}\n\n"
        "def register(ctx):\n"
        "    ctx.register_hook('pre_api_request', _guard)\n",
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {
                "enabled": ["auxiliary-guard"],
                "mandatory_hooks": {"pre_api_request": ["auxiliary-guard"]},
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    plugins_mod._plugin_manager = PluginManager()
    plugins_mod.discover_plugins()
    return hermes_home


def _blocked_kwargs():
    return {
        "model": "SENTINEL-MODEL",
        "messages": [{"role": "user", "content": "MAGNON_PRIVATE"}],
        "extra_headers": {"Authorization": "Bearer SENTINEL-CREDENTIAL"},
    }


def test_sync_auxiliary_preflight_blocks_before_transport(mandatory_guard_home):
    create = MagicMock()
    client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    with pytest.raises(MandatoryHookError) as exc_info:
        _relay_sync_completion(
            client,
            _blocked_kwargs(),
            provider="openrouter",
            api_mode="chat_completions",
        )

    assert exc_info.value.code == "mandatory_hook_blocked"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_async_auxiliary_preflight_blocks_before_transport(mandatory_guard_home):
    create = AsyncMock()
    client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    with pytest.raises(MandatoryHookError) as exc_info:
        await _relay_async_completion(
            client,
            _blocked_kwargs(),
            provider="openrouter",
            api_mode="chat_completions",
        )

    assert exc_info.value.code == "mandatory_hook_blocked"
    create.assert_not_awaited()


def test_streaming_auxiliary_preflight_blocks_before_transport(mandatory_guard_home):
    create = MagicMock()
    client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )

    with pytest.raises(MandatoryHookError) as exc_info:
        _relay_sync_stream(
            client,
            _blocked_kwargs(),
            provider="openrouter",
            api_mode="chat_completions",
        )

    assert exc_info.value.code == "mandatory_hook_blocked"
    create.assert_not_called()


def test_auxiliary_transport_mutation_is_guarded_before_dispatch(
    mandatory_guard_home,
):
    create = MagicMock()
    client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    safe_kwargs = {
        "model": "SENTINEL-MODEL",
        "messages": [{"role": "user", "content": "safe"}],
    }

    with pytest.raises(MandatoryHookError) as exc_info:
        _relay_sync_completion(
            client,
            safe_kwargs,
            provider="openrouter",
            api_mode="chat_completions",
            create=lambda request, preflight: _create_with_progress(
                client,
                request,
                force_stream=True,
                preflight=preflight,
            ),
            create_handles_preflight=True,
        )

    assert exc_info.value.code == "mandatory_hook_blocked"
    create.assert_not_called()


def test_native_anthropic_final_wire_is_guarded_before_messages_transport(
    mandatory_guard_home,
):
    from agent.auxiliary_client import _AnthropicCompletionsAdapter

    messages_api = SimpleNamespace(stream=MagicMock(), create=MagicMock())
    adapter = _AnthropicCompletionsAdapter(
        SimpleNamespace(messages=messages_api),
        "claude-sonnet-test",
        base_url="https://api.anthropic.com/v1",
    )
    final_wire = {
        "model": "claude-sonnet-test",
        "messages": [{"role": "user", "content": "MAGNON_PRIVATE"}],
        "max_tokens": 64,
    }

    with patch(
        "agent.anthropic_adapter.build_anthropic_kwargs",
        return_value=final_wire,
    ), pytest.raises(MandatoryHookError):
        adapter.create(
            model="claude-sonnet-test",
            messages=[{"role": "user", "content": "safe before conversion"}],
            max_tokens=64,
        )

    messages_api.stream.assert_not_called()
    messages_api.create.assert_not_called()


def test_codex_responses_final_stream_wire_is_guarded_before_transport(
    mandatory_guard_home,
):
    from agent.auxiliary_client import _CodexCompletionsAdapter

    create = MagicMock()
    client = SimpleNamespace(
        base_url="https://chatgpt.com/backend-api/codex",
        responses=SimpleNamespace(create=create),
    )
    adapter = _CodexCompletionsAdapter(client, "gpt-5.5")

    with pytest.raises(MandatoryHookError):
        adapter.create(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "safe before conversion"}],
        )

    create.assert_not_called()
