"""Telegram compact action menu and safe callback routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageType, SendResult
from gateway.run import GatewayRunner
from hermes_cli.commands import resolve_command
from hermes_cli.slash_exec import execute_command
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _query(data: str = "hm:status") -> AsyncMock:
    query = AsyncMock()
    query.data = data
    query.from_user = SimpleNamespace(
        id=42,
        first_name="Alexey",
        full_name="Alexey",
        is_bot=False,
    )
    query.message = SimpleNamespace(
        chat_id=100,
        message_id=900,
        message_thread_id=7,
        is_topic_message=True,
        chat=SimpleNamespace(
            id=100,
            type="private",
            title=None,
            full_name="Alexey",
        ),
    )
    return query


def test_menu_command_is_gateway_discoverable():
    command = resolve_command("menu")

    assert command is not None
    assert command.gateway_only is True
    assert command.busy_policy == "dispatch"
    assert command.execute == "gateway_menu"


def test_menu_command_has_text_fallback_for_other_platforms():
    reply = execute_command("menu", SimpleNamespace())

    assert "/new" in reply.text
    assert "/stop" in reply.text
    assert "/status" in reply.text
    assert "/sessions" in reply.text
    assert "/fast fast" in reply.text
    assert "/reasoning high" in reply.text


@pytest.mark.asyncio
async def test_runner_menu_sends_native_menu_with_exact_thread_metadata():
    adapter = _make_adapter()
    adapter.send_main_menu = AsyncMock(
        return_value=SendResult(success=True, message_id="menu-1")
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    event = adapter._main_menu_event(_query("hm:status"), "/menu")

    result = await runner._handle_menu_command(event)

    assert result is None
    adapter.send_main_menu.assert_awaited_once_with(
        "100",
        metadata={
            "thread_id": "7",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "7",
            "telegram_reply_to_message_id": "900",
        },
    )


@pytest.mark.asyncio
async def test_runner_menu_native_failure_returns_registry_text_fallback():
    adapter = _make_adapter()
    adapter.send_main_menu = AsyncMock(
        return_value=SendResult(success=False, error="temporary failure")
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    event = adapter._main_menu_event(_query("hm:status"), "/menu")

    result = await runner._handle_menu_command(event)

    assert result == execute_command("menu", SimpleNamespace()).text


@pytest.mark.asyncio
async def test_busy_menu_dispatch_uses_same_runner_executor():
    adapter = _make_adapter()
    adapter.send_main_menu = AsyncMock(
        return_value=SendResult(success=True, message_id="menu-1")
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    event = adapter._main_menu_event(_query("hm:status"), "/menu")
    command = resolve_command("menu")

    result = await runner._dispatch_busy_slash_command(
        event,
        command,
        "telegram:100:7",
        event.source,
    )

    assert result is None
    adapter.send_main_menu.assert_awaited_once()


@pytest.mark.asyncio
async def test_main_menu_has_six_compact_actions(monkeypatch):
    adapter = _make_adapter()
    rows = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: (text, callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda value: rows.extend(value) or value,
    )
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=901)
    )

    result = await adapter.send_main_menu(
        "100", metadata={"thread_id": "7"}
    )

    assert result.success is True
    assert [[callback for _label, callback in row] for row in rows] == [
        ["hm:new", "hm:stop"],
        ["hm:status", "hm:sessions"],
        ["hm:fast", "hm:deep"],
    ]


@pytest.mark.asyncio
async def test_menu_callback_routes_authorized_action_as_existing_command(monkeypatch):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
    query = _query("hm:status")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    query.answer.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/status"
    assert event.message_type == MessageType.COMMAND
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == "100"
    assert event.source.thread_id == "7"
    assert event.source.user_id == "42"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "command"),
    [
        ("stop", "/stop"),
        ("sessions", "/sessions"),
        ("fast", "/fast fast"),
        ("deep", "/reasoning high"),
    ],
)
async def test_every_direct_menu_action_routes_to_existing_command(
    monkeypatch, action, command
):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *a, **k: True
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=_query(f"hm:{action}")), SimpleNamespace()
    )

    event = adapter.handle_message.await_args.args[0]
    assert event.text == command


@pytest.mark.asyncio
async def test_menu_callback_uses_profile_bound_authorization(monkeypatch):
    adapter = _make_adapter()

    async def multiplex_profile_handler(_event):
        return None

    adapter._message_handler = multiplex_profile_handler
    adapter.handle_message = AsyncMock()
    decisions = []
    adapter.set_authorization_check(
        lambda user_id, chat_type, chat_id: decisions.append(
            (user_id, chat_type, chat_id)
        )
        or user_id == "42"
    )
    query = _query("hm:status")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert decisions == [("42", "dm", "100")]
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_rejects_profile_bound_denial(monkeypatch):
    adapter = _make_adapter()

    async def multiplex_profile_handler(_event):
        return None

    adapter._message_handler = multiplex_profile_handler
    adapter.handle_message = AsyncMock()
    adapter.set_authorization_check(lambda _user_id, _chat_type, _chat_id: False)
    query = _query("hm:status")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert "not authorized" in query.answer.await_args.kwargs["text"].lower()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_callback_denies_when_profile_auth_raises(monkeypatch):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()

    def broken_profile_auth(_user_id, _chat_type, _chat_id):
        raise RuntimeError("profile auth unavailable")

    adapter.set_authorization_check(broken_profile_auth)
    query = _query("hm:status")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert "not authorized" in query.answer.await_args.kwargs["text"].lower()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_menu_callback_rejects_unauthorized_user(monkeypatch):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: False)
    query = _query("hm:status")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert "not authorized" in query.answer.await_args.kwargs["text"].lower()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope_config",
    [
        {"allowed_chats": ["999"]},
        {"allowed_topics": ["8"]},
        {"ignored_threads": [7]},
    ],
)
async def test_stale_menu_callback_fails_closed_after_scope_revocation(
    monkeypatch, scope_config
):
    adapter = _make_adapter()
    adapter.config.extra.update(scope_config)
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *a, **k: True
    )
    query = _query("hm:status")
    query.message.chat.type = "supergroup"
    query.message.chat.is_forum = True

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert "no longer available" in query.answer.await_args.kwargs["text"].lower()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_menu_action_routes_once_to_generic_confirmation_authority(monkeypatch):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
    query = _query("hm:new")

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/new"
    assert not hasattr(adapter, "_main_menu_new_confirmations")
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["hm:new_yes", "hm:back"])
async def test_legacy_menu_confirmation_callbacks_fail_closed(monkeypatch, action):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(adapter, "_is_callback_user_authorized", lambda *a, **k: True)
    query = _query(action)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    adapter.handle_message.assert_not_awaited()
    assert "unknown" in query.answer.await_args.kwargs["text"].lower()


@pytest.mark.asyncio
async def test_typed_menu_is_forwarded_to_authoritative_runner_path(monkeypatch):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send_main_menu = AsyncMock(
        return_value=SendResult(success=True, message_id="menu-1")
    )
    monkeypatch.setattr(adapter, "_should_process_message", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_is_user_authorized_from_message", lambda _m: True)
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *a, **k: True
    )
    monkeypatch.setattr(adapter, "_ensure_forum_commands", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_build_message_event",
        lambda *_a, **_k: adapter._main_menu_event(_query(), "/menu"),
    )
    monkeypatch.setattr(adapter, "_clean_bot_trigger_text", lambda text: text)
    message = SimpleNamespace(
        text="/menu",
        message_id=900,
        chat=SimpleNamespace(
            id=100, type="private", title=None, full_name="Alexey"
        ),
        from_user=SimpleNamespace(
            id=42, first_name="Alexey", full_name="Alexey", is_bot=False
        ),
        message_thread_id=7,
        is_topic_message=True,
    )
    update = SimpleNamespace(
        effective_message=message,
        message=message,
        update_id=12,
    )

    await adapter._handle_command(update, SimpleNamespace())

    adapter.send_main_menu.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "/menu"
    assert event.source.thread_id == "7"


@pytest.mark.asyncio
@pytest.mark.parametrize("menu_authorized", [False, None])
async def test_menu_slash_unconfirmed_authorization_uses_runner_path(
    monkeypatch, menu_authorized
):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send_main_menu = AsyncMock()
    monkeypatch.setattr(adapter, "_should_process_message", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_is_user_authorized_from_message", lambda _m: True)
    monkeypatch.setattr(
        adapter,
        "_is_callback_user_authorized",
        lambda *a, **k: menu_authorized,
    )
    monkeypatch.setattr(adapter, "_ensure_forum_commands", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_build_message_event",
        lambda *_a, **_k: adapter._main_menu_event(_query(), "/menu"),
    )
    monkeypatch.setattr(adapter, "_clean_bot_trigger_text", lambda text: text)
    message = SimpleNamespace(
        text="/menu",
        message_id=900,
        chat=SimpleNamespace(
            id=100, type="private", title=None, full_name="Alexey"
        ),
        from_user=SimpleNamespace(
            id=42, first_name="Alexey", full_name="Alexey", is_bot=False
        ),
        message_thread_id=None,
        is_topic_message=False,
    )

    await adapter._handle_command(
        SimpleNamespace(
            effective_message=message,
            message=message,
            update_id=13,
        ),
        SimpleNamespace(),
    )

    adapter.send_main_menu.assert_not_awaited()
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "/menu"


@pytest.mark.asyncio
async def test_menu_slash_send_failure_uses_runner_text_fallback(monkeypatch):
    adapter = _make_adapter()
    adapter.handle_message = AsyncMock()
    adapter.send_main_menu = AsyncMock(
        return_value=SendResult(success=False, error="temporary failure")
    )
    monkeypatch.setattr(adapter, "_should_process_message", lambda *a, **k: True)
    monkeypatch.setattr(adapter, "_is_user_authorized_from_message", lambda _m: True)
    monkeypatch.setattr(
        adapter, "_is_callback_user_authorized", lambda *a, **k: True
    )
    monkeypatch.setattr(adapter, "_ensure_forum_commands", AsyncMock())
    monkeypatch.setattr(
        adapter,
        "_build_message_event",
        lambda *_a, **_k: adapter._main_menu_event(_query(), "/menu"),
    )
    monkeypatch.setattr(adapter, "_clean_bot_trigger_text", lambda text: text)
    message = SimpleNamespace(
        text="/menu",
        message_id=900,
        chat=SimpleNamespace(
            id=100, type="private", title=None, full_name="Alexey"
        ),
        from_user=SimpleNamespace(
            id=42, first_name="Alexey", full_name="Alexey", is_bot=False
        ),
        message_thread_id=None,
        is_topic_message=False,
    )

    await adapter._handle_command(
        SimpleNamespace(
            effective_message=message,
            message=message,
            update_id=14,
        ),
        SimpleNamespace(),
    )

    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "/menu"


def test_menu_callback_discards_non_topic_reply_anchor():
    adapter = _make_adapter()
    query = _query("hm:status")
    query.message.is_topic_message = False

    event = adapter._main_menu_event(query, "/status")

    assert event.source.thread_id is None


def test_menu_callback_routes_forum_general_topic_to_thread_one():
    adapter = _make_adapter()
    query = _query("hm:status")
    query.message.message_thread_id = None
    query.message.is_topic_message = False
    query.message.chat.type = "supergroup"
    query.message.chat.is_forum = True

    event = adapter._main_menu_event(query, "/status")

    assert event.source.thread_id == "1"
