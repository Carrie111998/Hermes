"""Security and lifecycle tests for the generic Discord component surface."""

from __future__ import annotations

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from dataclasses import FrozenInstanceError, fields

import pytest

from gateway.discord_components import (
    DISCORD_COMPONENT_ACK_TIMEOUT_SECONDS,
    DiscordComponentAuthorization,
    DiscordComponentButton,
    DiscordComponentButtonStyle,
    DiscordComponentDispatchStatus,
    DiscordComponentInteraction,
    DiscordComponentRegistry,
    DiscordComponentResponse,
    DiscordComponentTransportEvent,
    DuplicateDiscordComponentNamespace,
    InvalidDiscordComponentId,
    OverlappingDiscordComponentNamespace,
    build_discord_component_custom_id,
    parse_discord_component_custom_id,
)
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _event(
    namespace: str = "commute",
    action: str = "check-in",
    *,
    interaction_id: str = "interaction-1",
    custom_id: str | None = None,
) -> DiscordComponentTransportEvent:
    return DiscordComponentTransportEvent(
        custom_id=custom_id
        if custom_id is not None
        else build_discord_component_custom_id(namespace, action),
        guild_id="guild-1",
        channel_id="channel-1",
        message_id="message-1",
        user_id="user-1",
        session_key="discord:guild-1:channel-1:user-1",
        interaction_id=interaction_id,
    )


class _Transport:
    def __init__(self) -> None:
        self.deferred = 0
        self.responses: list[DiscordComponentResponse] = []

    async def defer(self) -> None:
        self.deferred += 1

    async def respond(self, response: DiscordComponentResponse) -> None:
        self.responses.append(response)


async def _allow(_: DiscordComponentAuthorization) -> bool:
    return True


def test_custom_id_grammar_is_strict() -> None:
    parsed = parse_discord_component_custom_id(
        "hermes-plugin:commute:check-in"
    )
    assert parsed.namespace == "commute"
    assert parsed.action == "check-in"
    assert (
        build_discord_component_custom_id("hr", "confirm.leave")
        == "hermes-plugin:hr:confirm.leave"
    )

    malformed = (
        "",
        "commute:check-in",
        "hermes-plugin:commute",
        "hermes-plugin:commute:",
        "hermes-plugin::check-in",
        "hermes-plugin:Commute:check-in",
        "hermes-plugin:commute:Check-in",
        "hermes-plugin:commute:check/in",
        "hermes-plugin:commute:check-in:extra",
        "hermes-plugin:commute :check-in",
        "hermes-plugin:commute:check in",
        "hermes-plugin:" + ("n" * 33) + ":run",
        "hermes-plugin:commute:" + ("a" * 49),
    )
    for custom_id in malformed:
        with pytest.raises(InvalidDiscordComponentId):
            parse_discord_component_custom_id(custom_id)


@pytest.mark.asyncio
async def test_malformed_and_unknown_ids_never_reach_authorize_or_handler() -> None:
    registry = DiscordComponentRegistry()
    transport = _Transport()
    calls: list[str] = []

    async def handler(_: DiscordComponentInteraction) -> str:
        calls.append("handler")
        return "ok"

    async def authorize(_: DiscordComponentAuthorization) -> bool:
        calls.append("authorize")
        return True

    registry.register("commute", plugin_owner="user.commute", handler=handler)

    malformed = await registry.dispatch(
        _event(custom_id="hermes-plugin:commute:bad/action"),
        authorize=authorize,
        defer=transport.defer,
        respond=transport.respond,
    )
    unknown = await registry.dispatch(
        _event(namespace="commute-admin", interaction_id="interaction-2"),
        authorize=authorize,
        defer=transport.defer,
        respond=transport.respond,
    )

    assert malformed.status is DiscordComponentDispatchStatus.INVALID_CUSTOM_ID
    assert unknown.status is DiscordComponentDispatchStatus.UNKNOWN_NAMESPACE
    assert calls == []
    assert transport.deferred == 0
    assert all(response.ephemeral is True for response in transport.responses)


def test_registration_rejects_duplicate_and_prefix_overlap() -> None:
    registry = DiscordComponentRegistry()

    async def handler(_: DiscordComponentInteraction) -> str:
        return "ok"

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    assert registry.owner_for("commute") == "user.commute"
    assert registry.registered_namespaces == ("commute",)

    with pytest.raises(DuplicateDiscordComponentNamespace):
        registry.register(
            "commute",
            plugin_owner="other.plugin",
            handler=handler,
        )
    with pytest.raises(OverlappingDiscordComponentNamespace):
        registry.register(
            "commute-admin",
            plugin_owner="other.plugin",
            handler=handler,
        )


def test_outbound_buttons_are_typed_and_bound_to_exact_owner() -> None:
    registry = DiscordComponentRegistry()

    async def handler(_: DiscordComponentInteraction) -> str:
        return "ok"

    registry.register(
        "commute",
        plugin_owner="user/commute",
        handler=handler,
    )
    message = registry.build_message(
        "commute",
        plugin_owner="user/commute",
        content="Choose a commute action",
        buttons=[
            DiscordComponentButton(
                action="check-in",
                label="Check in",
                style=DiscordComponentButtonStyle.SUCCESS,
            ),
            DiscordComponentButton(
                action="check-out",
                label="Check out",
                style="danger",
            ),
        ],
    )

    assert message.plugin_owner == "user/commute"
    assert [button.custom_id for button in message.buttons] == [
        "hermes-plugin:commute:check-in",
        "hermes-plugin:commute:check-out",
    ]
    assert all(not hasattr(button, "client") for button in message.buttons)
    with pytest.raises(PermissionError):
        registry.build_message(
            "commute",
            plugin_owner="user/hr",
            content="forged",
            buttons=[
                DiscordComponentButton(
                    action="check-in",
                    label="Forged",
                )
            ],
        )

    reverse = DiscordComponentRegistry()
    reverse.register(
        "commute-admin",
        plugin_owner="other.plugin",
        handler=handler,
    )
    with pytest.raises(OverlappingDiscordComponentNamespace):
        reverse.register(
            "commute",
            plugin_owner="user.commute",
            handler=handler,
        )


@pytest.mark.asyncio
async def test_authorization_is_exact_true_and_runs_before_handler() -> None:
    registry = DiscordComponentRegistry()
    transport = _Transport()
    order: list[str] = []

    async def handler(_: DiscordComponentInteraction) -> str:
        order.append("handler")
        return "handled"

    async def deny(_: DiscordComponentAuthorization) -> bool:
        order.append("authorize-deny")
        return False

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    denied = await registry.dispatch(
        _event(),
        authorize=deny,
        defer=transport.defer,
        respond=transport.respond,
    )

    assert denied.status is DiscordComponentDispatchStatus.UNAUTHORIZED
    assert denied.handler_invoked is False
    assert order == ["authorize-deny"]
    assert transport.deferred == 1
    assert registry.replay_claim_count == 0

    async def allow(_: DiscordComponentAuthorization) -> bool:
        order.append("authorize-allow")
        return True

    async def defer() -> None:
        order.append("defer")

    async def respond(_: DiscordComponentResponse) -> None:
        order.append("respond")

    allowed = await registry.dispatch(
        _event(),
        authorize=allow,
        defer=defer,
        respond=respond,
    )

    assert allowed.status is DiscordComponentDispatchStatus.HANDLED
    assert order[-4:] == ["defer", "authorize-allow", "handler", "respond"]


@pytest.mark.asyncio
async def test_concurrent_replay_invokes_handler_only_once() -> None:
    registry = DiscordComponentRegistry()
    transport = _Transport()
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    handler_calls = 0

    async def handler(_: DiscordComponentInteraction) -> str:
        nonlocal handler_calls
        handler_calls += 1
        handler_started.set()
        await release_handler.wait()
        return "handled"

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    event = _event()

    first_task = asyncio.create_task(
        registry.dispatch(
            event,
            authorize=_allow,
            defer=transport.defer,
            respond=transport.respond,
        )
    )
    await asyncio.wait_for(handler_started.wait(), timeout=1)
    second = await registry.dispatch(
        _event(interaction_id="interaction-2"),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )
    release_handler.set()
    first = await asyncio.wait_for(first_task, timeout=1)

    assert first.status is DiscordComponentDispatchStatus.HANDLED
    assert second.status is DiscordComponentDispatchStatus.DUPLICATE
    assert handler_calls == 1
    assert transport.deferred == 2
    assert registry.replay_claim_count == 1


@pytest.mark.asyncio
async def test_replay_claims_are_ttl_bounded_and_never_evict_live_claims() -> None:
    now = [100.0]
    registry = DiscordComponentRegistry(
        replay_ttl_seconds=5,
        max_replay_entries=1,
        clock=lambda: now[0],
    )
    transport = _Transport()
    handler_calls = 0

    async def handler(_: DiscordComponentInteraction) -> str:
        nonlocal handler_calls
        handler_calls += 1
        return "ok"

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    first = await registry.dispatch(
        _event(interaction_id="interaction-1"),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )
    full = await registry.dispatch(
        _event(
            interaction_id="interaction-2",
            action="check-out",
        ),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )

    assert first.status is DiscordComponentDispatchStatus.HANDLED
    assert (
        full.status
        is DiscordComponentDispatchStatus.REPLAY_CAPACITY_EXHAUSTED
    )
    assert registry.replay_claim_count == 1
    assert handler_calls == 1

    now[0] += 6
    after_expiry = await registry.dispatch(
        _event(interaction_id="interaction-2"),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )
    assert after_expiry.status is DiscordComponentDispatchStatus.HANDLED
    assert handler_calls == 2


@pytest.mark.asyncio
async def test_defer_completes_before_slow_handler_starts() -> None:
    registry = DiscordComponentRegistry(handler_timeout_seconds=1)
    order: list[str] = []
    defer_complete = asyncio.Event()

    async def defer() -> None:
        order.append("defer")
        defer_complete.set()

    async def handler(_: DiscordComponentInteraction) -> str:
        assert defer_complete.is_set()
        order.append("handler")
        await asyncio.sleep(0.02)
        return "complete"

    async def respond(response: DiscordComponentResponse) -> None:
        assert response.ephemeral is True
        order.append("respond")

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    outcome = await registry.dispatch(
        _event(),
        authorize=_allow,
        defer=defer,
        respond=respond,
    )

    assert outcome.status is DiscordComponentDispatchStatus.HANDLED
    assert order == ["defer", "handler", "respond"]


@pytest.mark.asyncio
async def test_defer_ack_precedes_slow_core_authorization_and_handler_waits() -> None:
    assert 0 < DISCORD_COMPONENT_ACK_TIMEOUT_SECONDS < 3
    registry = DiscordComponentRegistry(callback_timeout_seconds=10)
    assert (
        registry._ack_timeout_seconds  # noqa: SLF001 - security invariant
        == DISCORD_COMPONENT_ACK_TIMEOUT_SECONDS
    )
    order: list[str] = []
    authorization_started = asyncio.Event()
    release_authorization = asyncio.Event()
    handler = AsyncMock(return_value="ok")

    async def defer() -> None:
        order.append("defer")

    async def authorize(_: DiscordComponentAuthorization) -> bool:
        order.append("authorize")
        authorization_started.set()
        await release_authorization.wait()
        return True

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    dispatch_task = asyncio.create_task(
        registry.dispatch(
            _event(),
            authorize=authorize,
            defer=defer,
            respond=AsyncMock(),
        )
    )

    await asyncio.wait_for(authorization_started.wait(), timeout=0.2)
    assert order == ["defer", "authorize"]
    handler.assert_not_awaited()

    release_authorization.set()
    outcome = await asyncio.wait_for(dispatch_task, timeout=0.2)
    assert outcome.status is DiscordComponentDispatchStatus.HANDLED
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_discord_adapter_dispatches_through_shared_registry(
    monkeypatch,
) -> None:
    registry = DiscordComponentRegistry()
    seen: list[DiscordComponentInteraction] = []
    bound_context: dict[str, str] = {}

    async def handler(interaction: DiscordComponentInteraction) -> str:
        from gateway.session_context import get_session_env
        from tools.approval import get_current_session_key

        seen.append(interaction)
        bound_context.update(
            platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
            chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
            user_id=get_session_env("HERMES_SESSION_USER_ID", ""),
            scope_id=get_session_env("HERMES_SESSION_SCOPE_ID", ""),
            session_key=get_session_env("HERMES_SESSION_KEY", ""),
            message_id=get_session_env("HERMES_SESSION_MESSAGE_ID", ""),
            approval_session_key=get_current_session_key(default=""),
        )
        return "commute recorded"

    registry.register(
        "commute",
        plugin_owner="user/commute",
        handler=handler,
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.get_discord_component_registry",
        lambda: registry,
    )

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="redacted"))
    monkeypatch.setattr(
        adapter,
        "_evaluate_slash_authorization",
        lambda _interaction: (True, None),
    )

    done = {"value": False}

    async def defer(*, ephemeral: bool) -> None:
        assert ephemeral is True
        done["value"] = True

    response = SimpleNamespace(
        is_done=lambda: done["value"],
        defer=AsyncMock(side_effect=defer),
        send_message=AsyncMock(),
    )
    followup = SimpleNamespace(send=AsyncMock())
    interaction = SimpleNamespace(
        id="interaction-1",
        data={"custom_id": "hermes-plugin:commute:check-in"},
        user=SimpleNamespace(id="operator-1", display_name="Operator"),
        guild=SimpleNamespace(id="guild-1"),
        guild_id="guild-1",
        channel=SimpleNamespace(id="channel-1", parent_id=None),
        channel_id="channel-1",
        message=SimpleNamespace(id="message-1"),
        response=response,
        followup=followup,
    )

    assert await adapter._dispatch_plugin_component(interaction) is True
    response.defer.assert_awaited_once_with(ephemeral=True)
    response.send_message.assert_not_awaited()
    followup.send.assert_awaited_once_with(
        "commute recorded",
        ephemeral=True,
    )
    assert len(seen) == 1
    assert seen[0].user_id == "operator-1"
    assert seen[0].guild_id == "guild-1"
    assert seen[0].channel_id == "channel-1"
    assert seen[0].message_id == "message-1"
    assert seen[0].idempotency_key.startswith(
        "discord-component:v1:"
    )
    assert len(seen[0].idempotency_key) == (
        len("discord-component:v1:") + 64
    )
    assert bound_context == {
        "platform": "discord",
        "chat_id": "channel-1",
        "user_id": "operator-1",
        "scope_id": "guild-1",
        "session_key": seen[0].session_key,
        "message_id": "message-1",
        "approval_session_key": seen[0].session_key,
    }
    assert all(
        field.name not in {"client", "token", "raw_interaction"}
        for field in fields(DiscordComponentInteraction)
    )

@pytest.mark.asyncio
async def test_real_discord_adapter_sends_core_bound_button_view() -> None:
    registry = DiscordComponentRegistry()

    async def handler(_: DiscordComponentInteraction) -> str:
        return "ok"

    registry.register(
        "hr",
        plugin_owner="user/hr",
        handler=handler,
    )
    component_message = registry.build_message(
        "hr",
        plugin_owner="user/hr",
        content="Confirm this HR action",
        buttons=[
            DiscordComponentButton(
                action="confirm",
                label="Confirm",
                style="success",
            ),
            DiscordComponentButton(
                action="cancel",
                label="Cancel",
                style="danger",
            ),
        ],
    )
    sent_message = SimpleNamespace(id="sent-1")
    channel = SimpleNamespace(
        id=123,
        send=AsyncMock(return_value=sent_message),
        fetch_message=AsyncMock(),
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="redacted"))
    adapter._client = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )

    receipt = await adapter.send_plugin_component_message(
        chat_id="123",
        message=component_message,
    )

    assert receipt.success is True
    assert receipt.message_id == "sent-1"
    send_kwargs = channel.send.await_args.kwargs
    assert send_kwargs["content"] == "Confirm this HR action"
    assert send_kwargs["reference"] is None
    assert [
        child.custom_id for child in send_kwargs["view"].children
    ] == [
        "hermes-plugin:hr:confirm",
        "hermes-plugin:hr:cancel",
    ]


@pytest.mark.asyncio
async def test_real_discord_adapter_authorizes_before_plugin_handler(
    monkeypatch,
) -> None:
    registry = DiscordComponentRegistry()
    handler = AsyncMock(return_value="must not run")
    registry.register("hr", plugin_owner="user/hr", handler=handler)
    monkeypatch.setattr(
        "hermes_cli.plugins.get_discord_component_registry",
        lambda: registry,
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="redacted"))
    monkeypatch.setattr(
        adapter,
        "_evaluate_slash_authorization",
        lambda _interaction: (False, "wrong actor"),
    )

    done = {"value": False}

    async def defer(*, ephemeral: bool) -> None:
        assert ephemeral is True
        done["value"] = True

    response = SimpleNamespace(
        is_done=lambda: done["value"],
        defer=AsyncMock(side_effect=defer),
        send_message=AsyncMock(),
    )
    interaction = SimpleNamespace(
        id="interaction-2",
        data={"custom_id": "hermes-plugin:hr:confirm"},
        user=SimpleNamespace(id="wrong-actor", display_name="Wrong"),
        guild=SimpleNamespace(id="guild-1"),
        guild_id="guild-1",
        channel=SimpleNamespace(id="channel-1", parent_id=None),
        channel_id="channel-1",
        message=SimpleNamespace(id="message-2"),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )

    assert await adapter._dispatch_plugin_component(interaction) is True
    handler.assert_not_awaited()
    response.defer.assert_awaited_once_with(ephemeral=True)
    response.send_message.assert_not_awaited()
    interaction.followup.send.assert_awaited_once_with(
        "You are not authorized to use this action.",
        ephemeral=True,
    )


@pytest.mark.asyncio
async def test_handler_timeout_and_exception_are_isolated_without_secret_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timeout_registry = DiscordComponentRegistry(handler_timeout_seconds=0.01)
    timeout_transport = _Transport()
    cancelled = asyncio.Event()

    async def slow_handler(_: DiscordComponentInteraction) -> str:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return "unreachable"

    timeout_registry.register(
        "commute",
        plugin_owner="user.commute",
        handler=slow_handler,
    )
    timeout_outcome = await timeout_registry.dispatch(
        _event(),
        authorize=_allow,
        defer=timeout_transport.defer,
        respond=timeout_transport.respond,
    )

    assert timeout_outcome.status is DiscordComponentDispatchStatus.HANDLER_TIMEOUT
    assert cancelled.is_set()
    assert timeout_transport.responses[-1].ephemeral is True

    secret = "discord-token-must-never-appear"
    error_registry = DiscordComponentRegistry()
    error_transport = _Transport()

    async def failing_handler(_: DiscordComponentInteraction) -> str:
        raise RuntimeError(secret)

    error_registry.register(
        "hr",
        plugin_owner="user.hr",
        handler=failing_handler,
    )
    with caplog.at_level(logging.WARNING, logger="gateway.discord_components"):
        error_outcome = await error_registry.dispatch(
            _event(namespace="hr"),
            authorize=_allow,
            defer=error_transport.defer,
            respond=error_transport.respond,
        )

    assert error_outcome.status is DiscordComponentDispatchStatus.HANDLER_ERROR
    assert secret not in caplog.text
    assert secret not in error_transport.responses[-1].content


@pytest.mark.asyncio
async def test_plugin_context_is_frozen_scalar_only_and_transport_stays_private() -> None:
    registry = DiscordComponentRegistry()
    transport = _Transport()
    captured: list[DiscordComponentInteraction] = []

    async def handler(interaction: DiscordComponentInteraction) -> str:
        captured.append(interaction)
        return "ok"

    registry.register("commute", plugin_owner="user.commute", handler=handler)
    await registry.dispatch(
        _event(),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )

    interaction = captured[0]
    assert {field.name for field in fields(interaction)} == {
        "platform",
        "guild_id",
        "channel_id",
        "message_id",
        "user_id",
        "session_key",
        "interaction_id",
        "idempotency_key",
        "namespace",
        "action",
    }
    assert interaction.idempotency_key.startswith(
        "discord-component:v1:"
    )
    assert not hasattr(interaction, "__dict__")
    for forbidden in (
        "raw_interaction",
        "client",
        "token",
        "authorize",
        "defer",
        "respond",
    ):
        assert not hasattr(interaction, forbidden)
        assert forbidden not in inspect.signature(
            DiscordComponentTransportEvent
        ).parameters

    with pytest.raises(FrozenInstanceError):
        interaction.action = "tampered"  # type: ignore[misc]
    with pytest.raises(TypeError):
        DiscordComponentResponse("public", ephemeral=False)  # type: ignore[call-arg]
    assert transport.responses[-1].ephemeral is True


@pytest.mark.asyncio
async def test_commute_and_hr_have_distinct_exact_owners_and_handlers() -> None:
    registry = DiscordComponentRegistry()
    transport = _Transport()
    handled: list[tuple[str, str]] = []

    async def commute_handler(
        interaction: DiscordComponentInteraction,
    ) -> str:
        handled.append((interaction.namespace, interaction.action))
        return "commute complete"

    async def hr_handler(interaction: DiscordComponentInteraction) -> str:
        handled.append((interaction.namespace, interaction.action))
        return "hr complete"

    registry.register(
        "commute",
        plugin_owner="user.commute",
        handler=commute_handler,
    )
    registry.register("hr", plugin_owner="user.hr", handler=hr_handler)

    commute = await registry.dispatch(
        _event(namespace="commute", action="check-in", interaction_id="one"),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )
    hr = await registry.dispatch(
        _event(namespace="hr", action="confirm", interaction_id="two"),
        authorize=_allow,
        defer=transport.defer,
        respond=transport.respond,
    )

    assert commute.status is DiscordComponentDispatchStatus.HANDLED
    assert commute.plugin_owner == "user.commute"
    assert hr.status is DiscordComponentDispatchStatus.HANDLED
    assert hr.plugin_owner == "user.hr"
    assert handled == [("commute", "check-in"), ("hr", "confirm")]
