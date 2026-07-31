"""Actual plugin -> gateway -> Discord -> waiter attestation integration."""

import asyncio
import json
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_agent_sequential_registry_dispatch_requires_discord_attestation(
    monkeypatch,
):
    import discord
    import hermes_cli.plugins as plugins
    import tools.approval as approval
    from gateway.config import PlatformConfig
    from gateway.run import _notify_gateway_approval
    from gateway.session_context import clear_session_vars, set_session_vars
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from plugins.platforms.discord.adapter import DiscordAdapter
    from run_agent import AIAgent
    from tools.registry import registry

    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 2)

    tool_name = "financial_wire_transfer_acceptance"
    tool_schema = {
        "name": tool_name,
        "description": "Acceptance-only financial transfer tool.",
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer"},
                "currency": {"type": "string"},
            },
            "required": ["amount", "currency"],
            "additionalProperties": False,
        },
    }
    registry_state = registry._snapshot_registration_state()
    dispatched = []

    def dispatch_handler(args, **kwargs):
        dispatched.append(
            {
                "args": args,
                "task_id": kwargs.get("task_id"),
                "session_id": kwargs.get("session_id"),
            }
        )
        return '{"executed":true}'

    registry.register(
        name=tool_name,
        toolset="acceptance",
        schema=tool_schema,
        handler=dispatch_handler,
    )

    manager = PluginManager()
    context = PluginContext(
        PluginManifest(
            name="financial-policy",
            key="policies/financial-policy",
            source="user",
        ),
        manager,
    )
    observed = []

    def require_once(**_kwargs):
        return {
            "action": "approve",
            "message": "Confirm this one destructive operation",
            "rule_key": "wire-transfer",
            "approval_policy": {
                "decision_scope": "once",
                "risk_class": "financial",
            },
        }

    def observe_response(**kwargs):
        observed.append(kwargs)

    context.register_hook("pre_tool_call", require_once)
    context.register_hook("post_approval_response", observe_response)
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    sent = {}
    prompt = SimpleNamespace(id="prompt-1", embeds=[], edit=AsyncMock())

    async def send(**kwargs):
        sent.update(kwargs)
        prompt.embeds = [kwargs["embed"]]
        return prompt

    channel = SimpleNamespace(
        id="123",
        guild=SimpleNamespace(id="456"),
        send=AsyncMock(side_effect=send),
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="redacted"))
    adapter._allowed_user_ids = {"operator-1"}
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )
    adapter.pause_typing_for_chat = lambda _chat_id: None

    loop = asyncio.get_running_loop()
    gateway_context = SimpleNamespace(
        _status_adapter=adapter,
        _status_chat_id="123",
        _status_thread_metadata={},
        _loop_for_step=loop,
    )
    session_key = "agent:main:discord:456:123"
    result = {}
    messages = []

    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=[{"type": "function", "function": tool_schema}],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="discord",
        )
    agent.client = MagicMock()
    agent.session_id = "conversation-1"
    agent._current_turn_id = "turn-1"
    agent._current_api_request_id = "request-1"

    tool_call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(
            name=tool_name,
            arguments=json.dumps(
                {
                    # Exercise the production schema normalization before the
                    # approval digest and the final registry dispatch.
                    "amount": "1000",
                    "currency": "KRW",
                }
            ),
        ),
    )
    assistant_message = SimpleNamespace(
        content="",
        tool_calls=[tool_call],
    )

    notify_epoch = None

    def worker():
        tokens = set_session_vars(
            platform="discord",
            chat_id="123",
            scope_id="456",
            user_id="operator-1",
            session_key=session_key,
            message_id="source-message-1",
        )
        approval_token = approval.set_current_session_key(session_key)
        notify_token = approval.set_current_gateway_notify_epoch(
            notify_epoch
        )
        try:
            result["return"] = agent._execute_tool_calls_sequential(
                assistant_message,
                messages,
                "task-1",
            )
        except BaseException as exc:
            result["error"] = exc
        finally:
            approval.reset_current_gateway_notify_epoch(notify_token)
            approval.reset_current_session_key(approval_token)
            clear_session_vars(tokens)

    thread = threading.Thread(target=worker)
    try:
        notify_epoch = approval.register_gateway_notify(
            session_key,
            lambda data: _notify_gateway_approval(
                gateway_context, session_key, data
            ),
        )
        assert notify_epoch is not None
        thread.start()
        for _ in range(200):
            if "view" in sent:
                break
            await asyncio.sleep(0.005)
        assert "view" in sent
        assert thread.is_alive(), "tool policy must block until the click"
        assert dispatched == [], "registry handler must not run before approval"

        response = SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id="operator-1", display_name="Operator", roles=[]
            ),
            guild=SimpleNamespace(id="456"),
            guild_id="456",
            channel=channel,
            channel_id="123",
            message=prompt,
            response=response,
        )
        await sent["view"]._resolve(
            interaction,
            "once",
            discord.Color.green(),
            "Approved once",
        )
        for _ in range(400):
            if not thread.is_alive():
                break
            await asyncio.sleep(0.005)
        assert not thread.is_alive()
        assert "error" not in result
        assert result["return"] is None
        assert dispatched == [
            {
                "args": {"amount": 1000, "currency": "KRW"},
                "task_id": "task-1",
                "session_id": "conversation-1",
            }
        ]
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "call-1"
        assert json.loads(messages[0]["content"]) == {"executed": True}
        assert len(observed) == 1

        attestation = observed[0]["attestation"]
        assert attestation.approval_id
        assert attestation.actor_id == "operator-1"
        assert attestation.source_operator_id == "operator-1"
        assert attestation.session_key == session_key
        assert attestation.tool_call_id == "call-1"
        assert attestation.turn_id == "turn-1"
        assert attestation.plugin_identity == "policies/financial-policy"
        assert attestation.tool_name == tool_name
        assert attestation.choice == "once"
        assert attestation.decision is True
        assert attestation.source_platform == "discord"
        assert attestation.source_guild_id == "456"
        assert attestation.source_channel_id == "123"
        assert attestation.source_message_id == "source-message-1"
        assert attestation.canonical_arguments_digest == (
            approval.canonical_tool_arguments_digest(
                dispatched[0]["args"],
                strict=True,
            )
        )
        assert approval.has_blocking_approval(session_key) is False
        response.edit_message.assert_awaited_once()
    finally:
        approval.clear_session(session_key)
        approval.unregister_gateway_notify(session_key)
        thread.join(timeout=2)
        registry._restore_registration_state(registry_state)
