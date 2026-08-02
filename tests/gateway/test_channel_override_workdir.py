"""Channel-override workspace routing and isolation contracts."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
import pytest

from agent.prompt_builder import build_context_files_prompt
from agent.runtime_cwd import resolve_context_cwd
from gateway.config import (
    ChannelOverride,
    GatewayConfig,
    Platform,
    PlatformConfig,
    load_gateway_config,
)
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner, TurnRunner, _get_channel_override
from gateway.session import SessionContext, SessionEntry, SessionSource, build_session_key
from gateway.turn_context import TurnContext
from tools.file_tools import read_file_tool
from tools.terminal_tool import terminal_tool


def _config(overrides: dict[str, ChannelOverride]) -> GatewayConfig:
    return GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                channel_overrides=overrides,
            ),
            Platform.SLACK: PlatformConfig(
                enabled=True,
                channel_overrides=overrides,
            ),
        }
    )


def _runner(config: GatewayConfig) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    return runner


def _source(
    platform: Platform = Platform.DISCORD,
    *,
    chat_id: str = "333",
    parent_id: str | None = "222",
    ancestors: tuple[str, ...] = ("222", "111"),
) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_type="thread",
        thread_id=chat_id,
        parent_chat_id=parent_id,
        ancestor_chat_ids=ancestors,
        user_id="user-1",
    )


def test_generic_direct_channel_workdir_resolution(tmp_path):
    config = _config({"222": ChannelOverride(workdir=str(tmp_path))})
    source = SessionSource(platform=Platform.SLACK, chat_id="222")

    assert _runner(config)._resolve_workdir_for_session(source, "session-1") == str(
        tmp_path.resolve()
    )


def test_multiplexed_workdir_uses_routed_profile_config(tmp_path, monkeypatch):
    default_workspace = tmp_path / "default"
    profile_workspace = tmp_path / "profile"
    default_workspace.mkdir()
    profile_workspace.mkdir()
    default_config = _config(
        {"222": ChannelOverride(workdir=str(default_workspace))}
    )
    default_config.multiplex_profiles = True
    profile_config = _config(
        {"222": ChannelOverride(workdir=str(profile_workspace))}
    )
    runner = _runner(default_config)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="222",
        profile="work",
    )

    monkeypatch.setattr(
        runner,
        "_resolve_profile_home_for_source",
        lambda resolved_source: tmp_path / resolved_source.profile,
    )
    monkeypatch.setattr(
        "gateway.run._profile_runtime_scope",
        lambda profile_home: nullcontext(),
    )
    monkeypatch.setattr("gateway.run.load_gateway_config", lambda: profile_config)

    assert runner._resolve_workdir_for_session(source, "profile-session") == str(
        profile_workspace
    )


def test_discord_category_channel_thread_precedence(tmp_path):
    category = tmp_path / "category"
    channel = tmp_path / "channel"
    thread = tmp_path / "thread"
    for path in (category, channel, thread):
        path.mkdir()
    source = _source()

    category_only = _config({"111": ChannelOverride(workdir=str(category))})
    assert _runner(category_only)._resolve_workdir_for_session(source, "s1") == str(category)

    channel_override = _config(
        {
            "111": ChannelOverride(workdir=str(category)),
            "222": ChannelOverride(workdir=str(channel)),
        }
    )
    assert _runner(channel_override)._resolve_workdir_for_session(source, "s2") == str(channel)

    thread_override = _config(
        {
            "111": ChannelOverride(workdir=str(category)),
            "222": ChannelOverride(workdir=str(channel)),
            "333": ChannelOverride(workdir=str(thread)),
        }
    )
    assert _runner(thread_override)._resolve_workdir_for_session(source, "s3") == str(thread)

    # Override selection is whole-entry precedence: an exact child entry with
    # no workdir intentionally stops category workspace inheritance.
    stop_inheritance = _config(
        {
            "111": ChannelOverride(workdir=str(category)),
            "333": ChannelOverride(model="thread-only-model"),
        }
    )
    assert _runner(stop_inheritance)._resolve_workdir_for_session(source, "s4") == ""


def test_override_lookup_is_deterministic_deduplicated_and_preserves_other_fields():
    category = ChannelOverride(
        model="model-category",
        provider="provider-category",
        system_prompt="category prompt",
    )
    channel = ChannelOverride(
        model="model-channel",
        provider="provider-channel",
        system_prompt="channel prompt",
    )
    config = _config({"111": category, "222": channel})

    selected = _get_channel_override(
        config,
        Platform.DISCORD,
        "333",
        thread_id="333",
        parent_id="222",
        ancestor_ids=("222", "111", "111"),
    )

    assert selected is channel
    assert selected.model == "model-channel"
    assert selected.provider == "provider-channel"
    assert selected.system_prompt == "channel prompt"


def test_turn_runner_live_prompt_lookup_receives_ancestor_ids():
    class _LookupReached(Exception):
        pass

    class _StubRunner:
        def _get_system_prompt_for_channel(self, *_args, **kwargs):
            assert kwargs["ancestor_ids"] == ("222", "111")
            raise _LookupReached

    ctx = TurnContext(
        source=_source(),
        context_prompt="",
        channel_prompt="",
    )

    with pytest.raises(_LookupReached):
        TurnRunner(_StubRunner(), ctx).run_sync()


def test_category_ancestor_routes_runtime_model_provider_and_prompt(monkeypatch):
    override = ChannelOverride(
        model="category/model",
        provider="category-provider",
        system_prompt="category system prompt",
    )
    runner = _runner(_config({"111": override}))
    runner._session_model_overrides = {}
    runner._ephemeral_system_prompt = "global prompt"
    source = _source()
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda _cfg=None: "global/model")
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {"provider": "global-provider", "api_key": "test"},
    )
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider: {"provider": provider, "api_key": "test-provider"},
    )

    model, runtime = runner._resolve_session_agent_runtime(
        source=source,
        user_config={"model": {"default": "global/model"}},
    )
    prompt = runner._get_system_prompt_for_channel(
        source.platform,
        source.chat_id,
        thread_id=source.thread_id,
        parent_id=source.parent_chat_id,
        ancestor_ids=source.ancestor_chat_ids,
    )

    assert model == "category/model"
    assert runtime["provider"] == "category-provider"
    assert prompt == "category system prompt"


@pytest.mark.parametrize("configured", ["relative/project", "/missing/hermes-workdir"])
def test_invalid_workdir_fails_clearly(configured):
    runner = _runner(_config({"222": ChannelOverride(workdir=configured)}))
    source = SessionSource(platform=Platform.SLACK, chat_id="222")

    with pytest.raises(ValueError, match="absolute path|does not exist"):
        runner._resolve_workdir_for_session(source, "session-invalid")


def test_existing_file_is_not_accepted_as_workdir(tmp_path):
    configured = tmp_path / "not-a-directory"
    configured.write_text("content", encoding="utf-8")
    runner = _runner(_config({"222": ChannelOverride(workdir=str(configured))}))
    source = SessionSource(platform=Platform.SLACK, chat_id="222")

    with pytest.raises(ValueError, match="not a directory"):
        runner._resolve_workdir_for_session(source, "session-file")


def test_workdir_mapping_is_pinned_until_conversation_scope_clears(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    override = ChannelOverride(workdir=str(first))
    runner = _runner(_config({"222": override}))
    source = SessionSource(platform=Platform.SLACK, chat_id="222")

    assert runner._resolve_workdir_for_session(source, "session-1") == str(first)
    override.workdir = str(second)
    assert runner._resolve_workdir_for_session(source, "session-1") == str(first)

    runner._clear_session_boundary_security_state = lambda _key: None
    runner._clear_conversation_scope("session-1", reason="test reset")
    assert runner._resolve_workdir_for_session(source, "session-1") == str(second)


def test_conversation_boundary_clears_owned_workspace_after_terminal_cd(
    tmp_path, monkeypatch
):
    import tools.terminal_tool as terminal_module

    workspace = str(tmp_path.resolve())
    monkeypatch.setattr(
        terminal_module,
        "_task_env_overrides",
        {"task-1": {"docker_image": "neutral:latest", "cwd": workspace}},
    )
    monkeypatch.setattr(
        terminal_module,
        "_session_cwd",
        {"task-1": f"{workspace}/subdir"},
    )
    runner = _runner(_config({}))
    conversation = runner._session_state("session-1").conversation
    conversation.workdir = workspace
    conversation.workdir_task_overrides = {"task-1": workspace}
    runner._clear_session_boundary_security_state = lambda _key: None

    runner._clear_conversation_scope("session-1", reason="test boundary")

    assert terminal_module._task_env_overrides == {
        "task-1": {"docker_image": "neutral:latest"}
    }
    assert terminal_module.get_session_cwd("task-1") is None
    assert conversation.workdir is None
    assert conversation.workdir_task_overrides == {}
    assert not hasattr(runner, "_session_workdirs")
    assert not hasattr(runner, "_session_workdir_task_overrides")


def test_session_source_ancestor_ids_roundtrip():
    restored = SessionSource.from_dict(_source().to_dict())
    assert restored.ancestor_chat_ids == ("222", "111")


def test_relay_wire_preserves_ancestor_ids():
    from gateway.relay.ws_transport import _event_from_wire

    event = _event_from_wire(
        {
            "text": "hello",
            "source": _source().to_dict(),
        }
    )

    assert event.source.ancestor_chat_ids == ("222", "111")


@pytest.mark.asyncio
async def test_gateway_handler_routes_configured_workdir_to_real_tool_context(
    tmp_path, monkeypatch
):
    """Drive config -> handler -> session context -> tool cwd as production does."""
    workspace = tmp_path / "category-project"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "gateway-e2e-workspace", encoding="utf-8"
    )
    (workspace / "marker.txt").write_text(
        "gateway-e2e-file", encoding="utf-8"
    )
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "discord:\n"
        "  channel_overrides:\n"
        '    "111":\n'
        f"      workdir: {workspace}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config = load_gateway_config()
    runner = GatewayRunner(config)
    source = _source()
    session_key = build_session_key(source)
    session_id = "workspace-e2e-session"
    entry = SessionEntry(
        session_key=session_key,
        session_id=session_id,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="thread",
    )

    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda _event: None
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._handle_active_session_busy_message = AsyncMock(return_value=False)
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100_000,
    )

    observed = {}

    async def observe_agent_context(*_args, **_kwargs):
        observed["context_cwd"] = resolve_context_cwd()
        observed["prompt"] = build_context_files_prompt(cwd=resolve_context_cwd())
        observed["file"] = read_file_tool("marker.txt", task_id=session_id)
        from tools.code_execution_tool import _resolve_child_cwd

        observed["code_cwd"] = _resolve_child_cwd(
            "project", str(tmp_path), task_id=session_id
        )
        terminal_data = json.loads(terminal_tool("pwd", task_id=session_id))
        observed["terminal_cwd"] = terminal_data["output"].strip()
        return {
            "final_response": "done",
            "messages": [
                {"role": "user", "content": "inspect workspace"},
                {"role": "assistant", "content": "done"},
            ],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=observe_agent_context)
    event = MessageEvent(text="inspect workspace", source=source, message_id="m1")

    await runner._handle_message_with_agent(event, source, session_key, 1)

    expected = str(workspace.resolve())
    assert str(observed["context_cwd"]) == expected
    assert "gateway-e2e-workspace" in observed["prompt"]
    assert "gateway-e2e-file" in observed["file"]
    assert observed["code_cwd"] == expected
    assert observed["terminal_cwd"] == expected

    # Invalid-workdir errors must retain Slack's workspace discriminator, not
    # silently fall back to the adapter's primary workspace client.
    slack_source = SessionSource(
        platform=Platform.SLACK,
        chat_id="slack-channel",
        chat_type="thread",
        user_id="slack-user",
        thread_id="slack-thread",
        scope_id="workspace-two",
        message_id="slack-message",
    )
    slack_key = build_session_key(slack_source)
    runner.config = GatewayConfig(
        platforms={
            Platform.SLACK: PlatformConfig(
                enabled=True,
                channel_overrides={
                    "slack-channel": ChannelOverride(workdir="relative/project")
                },
            )
        }
    )
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=slack_key,
        session_id="slack-invalid-workdir-session",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.SLACK,
        chat_type="thread",
    )
    slack_adapter = MagicMock()
    slack_adapter.send = AsyncMock()
    runner.adapters[Platform.SLACK] = slack_adapter
    runner._run_agent.reset_mock()

    await runner._handle_message_with_agent(
        MessageEvent(
            text="inspect workspace",
            source=slack_source,
            message_id="slack-message",
        ),
        slack_source,
        slack_key,
        2,
    )

    runner._run_agent.assert_not_awaited()
    assert slack_adapter.send.await_args.kwargs["metadata"] == {
        "thread_id": "slack-thread",
        "message_id": "slack-message",
        "slack_team_id": "workspace-two",
    }


def test_concurrent_workdirs_isolate_context_file_terminal_and_file_tools(tmp_path):
    workspaces = []
    for name in ("alpha", "docs"):
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / "AGENTS.md").write_text(f"workspace-context:{name}", encoding="utf-8")
        (workspace / "marker.txt").write_text(f"workspace-file:{name}", encoding="utf-8")
        workspaces.append((name, workspace))

    async def observe(name: str, workspace) -> tuple[str, str, str]:
        runner = _runner(_config({}))
        source = SessionSource(platform=Platform.SLACK, chat_id=name)
        context = SessionContext(
            source=source,
            connected_platforms=[Platform.SLACK],
            home_channels={},
            session_key=f"session-{name}",
            session_id=f"id-{name}",
        )
        tokens = runner._set_session_env(context, cwd=str(workspace))
        task_id = f"id-{name}"
        try:
            await asyncio.sleep(0)
            prompt = build_context_files_prompt(cwd=resolve_context_cwd())
            from tools.terminal_tool import register_task_env_overrides

            register_task_env_overrides(task_id, {"cwd": str(workspace)})
            file_result = read_file_tool("marker.txt", task_id=task_id)
            from tools.code_execution_tool import _resolve_child_cwd

            code_cwd = _resolve_child_cwd(
                "project",
                str(tmp_path),
                task_id=task_id,
            )
            terminal_result = await asyncio.to_thread(
                terminal_tool,
                "pwd",
                task_id=task_id,
            )
            terminal_data = json.loads(terminal_result)
            assert code_cwd == str(workspace)
            return prompt, file_result, terminal_data["output"].strip()
        finally:
            from tools.terminal_tool import clear_task_env_overrides

            clear_task_env_overrides(task_id)
            runner._clear_session_env(tokens)

    async def run_both():
        return await asyncio.gather(
            *(observe(name, workspace) for name, workspace in workspaces)
        )

    alpha, docs = asyncio.run(run_both())

    assert "workspace-context:alpha" in alpha[0]
    assert "workspace-context:docs" not in alpha[0]
    assert "workspace-file:alpha" in alpha[1]
    assert alpha[2] == str(workspaces[0][1])
    assert "workspace-context:docs" in docs[0]
    assert "workspace-context:alpha" not in docs[0]
    assert "workspace-file:docs" in docs[1]
    assert docs[2] == str(workspaces[1][1])
