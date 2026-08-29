"""Tests for generic Telegram action buttons (#15311).

Covers the whole vertical slice: a producer declares ``metadata["buttons"]`` →
``send()`` renders an inline keyboard on the LAST chunk and registers one nonce
per action button against the identity Telegram confirmed → the tap comes back
as a ``gateway_platform_event`` of type ``action_button`` → an expired /
replayed / misrouted tap is a graceful no-op.

The real authorization boundary for a tap is exercised in
``test_telegram_action_button_authz.py``; the callbacks here use the env
allowlist so they can focus on routing and nonce lifetime.

The built-in approval/clarify/model-picker keyboards own their own callback
namespaces and are deliberately not exercised here — this flow must not touch
them.
"""

import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from telegram.error import BadRequest  # noqa: E402


RICH_CONTENT = "## Results\n\n| Case | Status |\n|---|---|\n| rich | ✅ |\n\n- [x] renders"


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _tg_message(
    *,
    chat_id=12345,
    message_id=100,
    chat_type="private",
    thread_id=None,
    is_topic=False,
    is_forum=False,
):
    """A Telegram ``Message`` stand-in.

    The SAME shape is returned by ``send_message`` and carried by the callback
    query, because in production they are literally the same message — that
    identity is what a nonce is bound to.
    """
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat_id = chat_id
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.is_forum = is_forum
    msg.message_thread_id = thread_id
    msg.is_topic_message = is_topic
    return msg


def _inaccessible_message(*, chat_id, message_id=100, chat_type="supergroup", is_forum=True):
    """PTB's ``InaccessibleMessage`` — what a tap on a deleted / too-old message
    carries. It has ONLY ``chat``, ``message_id`` and ``date``; deliberately not
    built on MagicMock, because the missing ``message_thread_id`` is the point
    (a MagicMock would auto-create one)."""

    class InaccessibleMessage:  # noqa: D401 — name is the structural marker
        def __init__(self):
            self.message_id = message_id
            self.chat_id = chat_id
            self.chat = MagicMock()
            self.chat.id = chat_id
            self.chat.type = chat_type
            self.chat.is_forum = is_forum
            self.date = 0  # Bot API: always 0 on an inaccessible message

    return InaccessibleMessage()


def _route(profile, *, chat_id="12345", thread_id=None):
    from gateway.profile_routing import ProfileRoute

    return ProfileRoute(
        name=f"route-{profile}", platform="telegram", profile=profile,
        chat_id=chat_id, thread_id=thread_id,
    )


@contextmanager
def _profile_routed(adapter, routes, served=("default", "finances", "other")):
    """Wire a REAL ``_profile_name_for_source`` onto the adapter's runner.

    This is the primary (unowned) adapter shape: no ``_owner_profile``, so the
    profile a button binds to comes purely from ``gateway.profile_routes``.
    ``runner.config.profile_routes`` stays mutable so a test can re-point a
    route between the send and the tap.
    """
    from gateway.run import GatewayRunner

    runner = MagicMock(spec=GatewayRunner)
    runner.config = MagicMock(
        multiplex_profiles=True, multiplex_profile_allowlist=None, profile_routes=list(routes),
    )
    runner._profile_name_for_source = GatewayRunner._profile_name_for_source.__get__(runner)
    adapter.gateway_runner = runner
    with patch(
        "hermes_cli.profiles.profiles_to_serve",
        return_value=[(name, Path(f"/profiles/{name}")) for name in served],
    ):
        yield runner


@pytest.fixture
def keyboard(monkeypatch):
    """Render InlineKeyboard* as plain data so the markup shape is assertable.

    The gateway conftest installs a MagicMock ``telegram`` module, so the real
    classes are opaque. Mirrors the monkeypatch in
    test_telegram_approval_buttons.py.
    """
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data=None, url=None: {
            "text": text, "callback_data": callback_data, "url": url,
        },
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", lambda rows: rows
    )


def _query(adapter, callback_data, *, message=None, user_id="777"):
    """A callback_query stand-in for a tap on ``callback_data``."""
    query = AsyncMock()
    query.data = callback_data
    query.message = message if message is not None else _tg_message()
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "Tester"
    query.from_user.username = "tester"
    query.answer = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


async def _send_with_buttons(
    adapter, buttons, content="Deploy?", chat_id="12345", sent=None, **meta
):
    adapter._bot.send_message = AsyncMock(
        return_value=sent if sent is not None else _tg_message()
    )
    return await adapter.send(
        chat_id, content, metadata={"buttons": buttons, **meta},
    )


def _minted_callback_data(adapter, row=0, col=0):
    """The nonce the last send() put on a rendered button."""
    return adapter._bot.send_message.call_args[1]["reply_markup"][row][col]["callback_data"]


def _observe(adapter):
    """Capture every gateway_platform_event the adapter fires."""
    seen = []

    async def handler(event, source):
        seen.append((event, source))

    adapter.set_platform_event_handler(handler)
    return seen


async def _tap(adapter, update, allowed="*"):
    with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": allowed}, clear=False):
        await adapter._handle_callback_query(update, MagicMock())


# ===========================================================================
# Send side — render → register
# ===========================================================================

class TestActionButtonRender:
    @pytest.mark.asyncio
    async def test_flat_buttons_render_one_row_of_minted_nonces(self, keyboard):
        adapter = _make_adapter()

        result = await _send_with_buttons(
            adapter,
            [{"text": "Ship it", "value": "ship"}, {"text": "Hold", "value": "hold"}],
            button_producer="deploy-webhook",
        )

        assert result.success is True
        markup = adapter._bot.send_message.call_args[1]["reply_markup"]
        assert len(markup) == 1 and len(markup[0]) == 2
        assert [b["text"] for b in markup[0]] == ["Ship it", "Hold"]
        # callback_data is a versioned, server-minted nonce — the producer's
        # own value is nowhere on the wire.
        for button in markup[0]:
            assert button["callback_data"].startswith("hb1:")
            assert len(button["callback_data"]) <= 64
        assert "ship" not in str(markup)
        # ...it lives in the registry instead, bound to the identity Telegram
        # actually delivered the keyboard to.
        assert len(adapter._action_button_state) == 2
        record = adapter._action_button_state[markup[0][0]["callback_data"][4:]]
        assert record["chat_id"] == "12345"
        assert record["thread_id"] is None
        assert record["message_id"] == "100"
        assert record["value"] == "ship"
        assert record["producer"] == "deploy-webhook"

    @pytest.mark.asyncio
    async def test_grid_and_url_buttons(self, keyboard):
        adapter = _make_adapter()

        await _send_with_buttons(
            adapter,
            [
                [{"text": "Yes", "value": "y"}, {"text": "No", "value": "n"}],
                [{"text": "Docs", "url": "https://example.com"}],
            ],
        )

        markup = adapter._bot.send_message.call_args[1]["reply_markup"]
        assert [len(row) for row in markup] == [2, 1]
        # URL buttons pass through untouched and mint nothing.
        assert markup[1][0]["url"] == "https://example.com"
        assert markup[1][0]["callback_data"] is None
        assert len(adapter._action_button_state) == 2

    @pytest.mark.asyncio
    async def test_no_buttons_leaves_send_kwargs_untouched(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=_tg_message())

        await adapter.send("12345", "plain", metadata={"notify": True})

        assert "reply_markup" not in adapter._bot.send_message.call_args[1]
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_markup_on_last_chunk_only(self, keyboard):
        adapter = _make_adapter()
        adapter.truncate_message = lambda text, limit, **kw: ["one", "two"]

        await _send_with_buttons(adapter, [{"text": "Go", "value": "go"}], content="x" * 50)

        calls = adapter._bot.send_message.call_args_list
        assert len(calls) == 2
        assert "reply_markup" not in calls[0][1]
        assert calls[1][1]["reply_markup"][0][0]["text"] == "Go"
        # One action, one nonce — not one per chunk.
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_rich_content_with_buttons_still_uses_send_rich_message(self, keyboard):
        """Buttons must not force rich content down the lossy MarkdownV2 path."""
        adapter = _make_adapter({"rich_messages": True})
        adapter._bot.do_api_request = AsyncMock(
            return_value={"message_id": 100, "chat": {"id": 12345, "type": "private"}}
        )
        adapter._bot.send_message = AsyncMock(return_value=_tg_message())

        result = await adapter.send(
            "12345",
            RICH_CONTENT,
            metadata={"buttons": [{"text": "Ship it", "value": "ship"}]},
        )

        assert result.success is True
        assert adapter._bot.do_api_request.call_args.args[0] == "sendRichMessage"
        payload = adapter._bot.do_api_request.call_args.kwargs["api_kwargs"]
        assert payload["reply_markup"][0][0]["callback_data"].startswith("hb1:")
        # Table pipes survive: no legacy chunked resend.
        adapter._bot.send_message.assert_not_awaited()
        # ...and the button is registered against the rich response's identity.
        assert len(adapter._action_button_state) == 1
        record = next(iter(adapter._action_button_state.values()))
        assert (record["chat_id"], record["message_id"]) == ("12345", "100")

    @pytest.mark.asyncio
    async def test_rich_rejection_falls_back_to_legacy_with_buttons(self, keyboard):
        adapter = _make_adapter({"rich_messages": True})
        adapter._bot.do_api_request = AsyncMock(side_effect=BadRequest("bad markup"))
        adapter._bot.send_message = AsyncMock(return_value=_tg_message())

        result = await adapter.send(
            "12345",
            RICH_CONTENT,
            metadata={"buttons": [{"text": "Ship it", "value": "ship"}]},
        )

        assert result.success is True
        assert adapter._bot.send_message.call_args[1]["reply_markup"][0][0]["text"] == "Ship it"
        # One delivery, one registration — the rejected rich attempt left nothing.
        assert len(adapter._action_button_state) == 1


class TestActionButtonRegistration:
    """Nothing is registered until Telegram confirms the delivery (#15311)."""

    @pytest.mark.asyncio
    async def test_failed_send_registers_nothing(self, keyboard):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(side_effect=BadRequest("chat not found"))

        result = await adapter.send(
            "12345", "Deploy?", metadata={"buttons": [{"text": "Go", "value": "go"}]},
        )

        assert result.success is False
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_refused_dm_topic_send_registers_nothing(self, keyboard):
        """The fail-loud missing-anchor exit happens after the markup is built."""
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=_tg_message())
        adapter._is_private_dm_topic_send = lambda *a, **kw: True

        result = await adapter.send(
            "12345", "Deploy?", metadata={"buttons": [{"text": "Go", "value": "go"}]},
        )

        assert result.success is False
        adapter._bot.send_message.assert_not_awaited()
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_retried_send_does_not_accumulate_nonces(self, keyboard):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(side_effect=BadRequest("chat not found"))
        metadata = {"buttons": [{"text": "Go", "value": "go"}]}

        for _ in range(3):
            await adapter.send("12345", "Deploy?", metadata=metadata)
        assert adapter._action_button_state == {}

        adapter._bot.send_message = AsyncMock(return_value=_tg_message())
        await adapter.send("12345", "Deploy?", metadata=metadata)
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_send_without_a_message_id_registers_nothing(self, keyboard):
        """Half an identity is not an identity: a record missing the message id
        would make that half a wildcard for every later tap."""
        adapter = _make_adapter()

        result = await _send_with_buttons(
            adapter, [{"text": "Go", "value": "go"}], sent=_tg_message(message_id=None),
        )

        assert result.success is True
        assert adapter._action_button_state == {}


class TestActionButtonNonceReservations:
    """A nonce is unique against sends still IN FLIGHT, not just committed ones.

    Nothing is committed until Telegram confirms a delivery, so the registry is
    empty for the whole window in which a second send builds its own markup.
    """

    def test_uncommitted_sends_hold_their_nonces(self, keyboard):
        adapter = _make_adapter()

        _, first = adapter._build_reply_markup({"buttons": [{"text": "A", "value": "a"}]})
        _, second = adapter._build_reply_markup({"buttons": [{"text": "B", "value": "b"}]})

        assert adapter._action_button_state == {}  # neither has delivered
        assert first[0]["nonce"] != second[0]["nonce"]
        assert adapter._action_button_reserved == {first[0]["nonce"], second[0]["nonce"]}

    def test_a_reserved_nonce_is_regenerated_not_reissued(self, keyboard, monkeypatch):
        """The collision loop consults the reservations, not only the registry."""
        adapter = _make_adapter()
        _, first = adapter._build_reply_markup({"buttons": [{"text": "A", "value": "a"}]})
        # Hand the next mint the value the in-flight send already holds.
        values = iter([first[0]["nonce"], "feedfacefeedface"])
        monkeypatch.setattr(secrets, "token_hex", lambda _n: next(values))

        _, second = adapter._build_reply_markup({"buttons": [{"text": "B", "value": "b"}]})

        assert second[0]["nonce"] == "feedfacefeedface"

    @pytest.mark.asyncio
    async def test_failed_send_releases_its_reservations(self, keyboard):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(side_effect=BadRequest("chat not found"))

        result = await adapter.send(
            "12345", "Deploy?", metadata={"buttons": [{"text": "Go", "value": "go"}]},
        )

        assert result.success is False
        assert adapter._action_button_state == {}
        assert adapter._action_button_reserved == set()

    @pytest.mark.asyncio
    async def test_delivered_send_hands_its_reservation_to_the_registry(self, keyboard):
        adapter = _make_adapter()

        await _send_with_buttons(adapter, [{"text": "Go", "value": "go"}])

        assert len(adapter._action_button_state) == 1
        assert adapter._action_button_reserved == set()

    def test_markup_construction_failure_leaks_no_reservation(self, keyboard, monkeypatch):
        """A markup constructor raise must not leak the just-minted nonce.

        The reservation is only made after ``InlineKeyboardMarkup`` is built,
        so a failure there leaves the adapter-wide reservation set empty.
        """

        def _boom(rows):
            raise ValueError("bad keyboard")

        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup", _boom,
        )
        adapter = _make_adapter()
        with pytest.raises(ValueError):
            adapter._build_reply_markup({"buttons": [{"text": "A", "value": "a"}]})

        assert adapter._action_button_reservations() == set()

    def test_noncanonical_username_fallback_refuses_to_register(self, keyboard):
        """An ``@username`` send with no numeric chat.id in the response binds nothing.

        The tap would report a numeric chat id that a username-bound record can
        never match, so registration is refused rather than leaving an
        unmatchable record that can only read as "expired".
        """
        adapter = _make_adapter()
        _, pending = adapter._build_reply_markup(
            {"buttons": [{"text": "A", "value": "a"}]},
        )

        adapter._commit_action_buttons(
            pending,
            {"result": {"message_id": "12345"}},  # no "chat" -> sent_chat_id None
            chat_id="@some_user",
        )

        assert adapter._action_button_state == {}


# ===========================================================================
# Callback side — tap → gateway_platform_event
# ===========================================================================

class TestActionButtonCallback:
    @pytest.mark.asyncio
    async def test_tap_emits_gateway_event_and_answers(self, keyboard):
        adapter = _make_adapter()
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}], button_producer="deploy-webhook",
        )
        callback_data = _minted_callback_data(adapter)
        seen = _observe(adapter)
        update, query = _query(adapter, callback_data)

        await _tap(adapter, update)

        assert len(seen) == 1
        event, source = seen[0]
        assert event["platform"] == "telegram"
        assert event["event_type"] == "action_button"
        assert event["payload"] == {
            "chat_id": "12345",
            "message_id": "100",
            "thread_id": None,
            "user_id": "777",
            "producer": "deploy-webhook",
            "label": "Ship it",
            "value": "ship",
        }
        # The authorized identity is the tapping user.
        assert source.user_id == "777"
        assert source.chat_id == "12345"
        # Spinner cleared, and the nonce is consumed.
        query.answer.assert_awaited_once_with(text="Ship it")
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_replayed_tap_is_a_no_op(self, keyboard):
        adapter = _make_adapter()
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        callback_data = _minted_callback_data(adapter)
        seen = _observe(adapter)

        first, _ = _query(adapter, callback_data)
        update, query = _query(adapter, callback_data)
        await _tap(adapter, first)
        await _tap(adapter, update)

        assert len(seen) == 1  # single-use: the second tap fires nothing
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")

    @pytest.mark.asyncio
    async def test_unknown_nonce_is_graceful(self):
        adapter = _make_adapter()
        seen = _observe(adapter)
        update, query = _query(adapter, "hb1:deadbeefdeadbeef")

        await _tap(adapter, update)  # no raise

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")

    @pytest.mark.asyncio
    async def test_ttl_expiry_drops_the_button(self, keyboard):
        adapter = _make_adapter()
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        callback_data = _minted_callback_data(adapter)
        seen = _observe(adapter)
        # Age the entry past its TTL rather than sleeping 10 minutes. TTLs are
        # monotonic, so a wall-clock jump can neither extend nor expire one.
        for record in adapter._action_button_state.values():
            record["expires_at"] = time.monotonic() - 1
        update, query = _query(adapter, callback_data)

        await _tap(adapter, update)

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_answer_runs_even_when_the_handler_is_cancelled(self, keyboard):
        """A hanging/cancelled plugin dispatch must not leave the spinner on."""
        import asyncio

        adapter = _make_adapter()
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        callback_data = _minted_callback_data(adapter)

        async def handler(event, source):
            raise asyncio.CancelledError()

        adapter.set_platform_event_handler(handler)
        update, query = _query(adapter, callback_data)

        with pytest.raises(asyncio.CancelledError):
            await _tap(adapter, update)
        query.answer.assert_awaited_once_with(text="Ship it")


class TestActionButtonIdentityBinding:
    """A nonce is bound to the chat/topic/message it was delivered on."""

    @pytest.mark.asyncio
    async def test_tap_from_another_chat_is_refused(self, keyboard):
        adapter = _make_adapter()
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        seen = _observe(adapter)
        update, query = _query(
            adapter, _minted_callback_data(adapter), message=_tg_message(chat_id=99999),
        )

        await _tap(adapter, update)

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")
        # A foreign tap must not burn the button for its own chat.
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_forum_general_topic_round_trips(self, keyboard):
        """General-topic messages carry no thread id but ARE thread "1"."""
        adapter = _make_adapter()
        general = _tg_message(
            chat_id=-100999, chat_type="supergroup", is_forum=True, thread_id=None,
        )
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}],
            chat_id="-100999", sent=general,
        )
        record = next(iter(adapter._action_button_state.values()))
        assert record["thread_id"] == "1"

        seen = _observe(adapter)
        update, query = _query(adapter, _minted_callback_data(adapter), message=general)
        await _tap(adapter, update)

        assert len(seen) == 1
        assert seen[0][0]["payload"]["thread_id"] == "1"
        query.answer.assert_awaited_once_with(text="Ship it")

    @pytest.mark.asyncio
    async def test_reply_anchor_thread_id_is_not_a_topic(self, keyboard):
        """A plain group reply populates message_thread_id spuriously."""
        adapter = _make_adapter()
        replied = _tg_message(
            chat_id=-100777, chat_type="supergroup", is_forum=False,
            thread_id=4242, is_topic=False,
        )
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}],
            chat_id="-100777", sent=replied,
        )
        record = next(iter(adapter._action_button_state.values()))
        assert record["thread_id"] is None  # anchor id, not a topic

        seen = _observe(adapter)
        update, query = _query(adapter, _minted_callback_data(adapter), message=replied)
        await _tap(adapter, update)

        assert len(seen) == 1
        query.answer.assert_awaited_once_with(text="Ship it")

    @pytest.mark.asyncio
    async def test_tap_from_another_topic_in_the_same_chat_is_refused(self, keyboard):
        adapter = _make_adapter()
        topic = _tg_message(
            chat_id=-100999, message_id=100, chat_type="supergroup",
            is_forum=True, thread_id=42, is_topic=True,
        )
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}],
            chat_id="-100999", sent=topic,
        )
        seen = _observe(adapter)
        elsewhere = _tg_message(
            chat_id=-100999, message_id=101, chat_type="supergroup",
            is_forum=True, thread_id=99, is_topic=True,
        )
        update, query = _query(adapter, _minted_callback_data(adapter), message=elsewhere)

        await _tap(adapter, update)

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_username_send_binds_the_numeric_chat_id(self, keyboard):
        """An @username send must not leave the button permanently "expired"."""
        adapter = _make_adapter()
        delivered = _tg_message(chat_id=-1001234, chat_type="channel")
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}],
            chat_id="@hermes_news", sent=delivered,
        )
        record = next(iter(adapter._action_button_state.values()))
        assert record["chat_id"] == "-1001234"

        seen = _observe(adapter)
        update, query = _query(adapter, _minted_callback_data(adapter), message=delivered)
        await _tap(adapter, update)

        assert len(seen) == 1
        assert seen[0][0]["payload"]["chat_id"] == "-1001234"
        query.answer.assert_awaited_once_with(text="Ship it")

    @pytest.mark.asyncio
    async def test_tap_without_a_message_id_is_refused(self, keyboard):
        """A missing message id is a mismatch, never a wildcard."""
        adapter = _make_adapter()
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        seen = _observe(adapter)
        update, query = _query(
            adapter, _minted_callback_data(adapter), message=_tg_message(message_id=None),
        )

        await _tap(adapter, update)

        assert seen == []
        assert len(adapter._action_button_state) == 1


class TestActionButtonInaccessibleMessage:
    """PTB delivers a tap on a deleted / too-old message as an
    ``InaccessibleMessage``: it reports its chat and message id but NO thread."""

    @pytest.mark.asyncio
    async def test_inaccessible_forum_tap_recovers_its_bound_topic(self, keyboard):
        """Reading the absent thread as the General topic would falsely expire
        every button in a forum's other topics."""
        adapter = _make_adapter()
        topic = _tg_message(
            chat_id=-100999, message_id=100, chat_type="supergroup",
            is_forum=True, thread_id=42, is_topic=True,
        )
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}],
            chat_id="-100999", sent=topic,
        )
        assert next(iter(adapter._action_button_state.values()))["thread_id"] == "42"

        seen = _observe(adapter)
        update, query = _query(
            adapter, _minted_callback_data(adapter),
            message=_inaccessible_message(chat_id=-100999, message_id=100),
        )

        await _tap(adapter, update)

        assert len(seen) == 1
        event, source = seen[0]
        # The trusted topic comes from the record, not from the tap's silence.
        assert event["payload"]["thread_id"] == "42"
        assert source.thread_id == "42"
        query.answer.assert_awaited_once_with(text="Ship it")

    @pytest.mark.asyncio
    async def test_inaccessible_tap_from_another_chat_is_refused(self, keyboard):
        """The ids it CAN report are still matched exactly — the recovery is not
        a way to borrow another record's topic."""
        adapter = _make_adapter()
        topic = _tg_message(
            chat_id=-100999, message_id=100, chat_type="supergroup",
            is_forum=True, thread_id=42, is_topic=True,
        )
        await _send_with_buttons(
            adapter, [{"text": "Ship it", "value": "ship"}],
            chat_id="-100999", sent=topic,
        )
        seen = _observe(adapter)
        update, query = _query(
            adapter, _minted_callback_data(adapter),
            message=_inaccessible_message(chat_id=-100777, message_id=100),
        )

        await _tap(adapter, update)

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")
        assert len(adapter._action_button_state) == 1


class TestActionButtonProfileBinding:
    """A button is bound to the profile LANE it was minted for, and that binding
    is enforced — the tap is dispatched into a session key derived from the same
    ``_session_key_profile`` answer, so a lane that has drifted must not act."""

    @pytest.mark.asyncio
    async def test_secondary_owner_profile_round_trips(self, keyboard):
        """A per-credential adapter owned by a profile binds to that profile."""
        adapter = _make_adapter()
        adapter.set_owner_profile("coder")
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        assert next(iter(adapter._action_button_state.values()))["profile"] == "coder"

        seen = _observe(adapter)
        update, query = _query(adapter, _minted_callback_data(adapter))
        await _tap(adapter, update)

        assert len(seen) == 1
        query.answer.assert_awaited_once_with(text="Ship it")
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_tap_after_the_adapter_is_re_owned_is_refused(self, keyboard):
        adapter = _make_adapter()
        adapter.set_owner_profile("coder")
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        seen = _observe(adapter)
        update, query = _query(adapter, _minted_callback_data(adapter))

        adapter.set_owner_profile("finances")  # this bot now serves another lane
        await _tap(adapter, update)

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_primary_routed_profile_round_trips(self, keyboard):
        """The primary adapter owns no profile — ``profile_routes`` decides, and
        the send side must resolve the SAME route the tap resolves."""
        adapter = _make_adapter()

        with _profile_routed(adapter, [_route("finances")]):
            await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
            assert next(iter(adapter._action_button_state.values()))["profile"] == "finances"

            seen = _observe(adapter)
            update, query = _query(adapter, _minted_callback_data(adapter))
            await _tap(adapter, update)

        assert len(seen) == 1
        # The tap is dispatched into the lane it was minted for.
        assert seen[0][1].profile == "finances"
        query.answer.assert_awaited_once_with(text="Ship it")
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_tap_after_the_route_is_re_pointed_is_refused(self, keyboard):
        """Re-pointing the route makes this chat another profile's — the pending
        button must expire rather than act in a lane it never belonged to."""
        adapter = _make_adapter()

        with _profile_routed(adapter, [_route("finances")]) as runner:
            await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
            seen = _observe(adapter)
            update, query = _query(adapter, _minted_callback_data(adapter))

            runner.config.profile_routes = [_route("other")]
            await _tap(adapter, update)

        assert seen == []
        query.answer.assert_awaited_once_with(text="⌛ This action has expired.")
        assert len(adapter._action_button_state) == 1


class TestActionButtonAuthorizationGate:
    @pytest.mark.asyncio
    async def test_unauthorized_user_never_reaches_the_registry(self, keyboard):
        adapter = _make_adapter()
        await _send_with_buttons(adapter, [{"text": "Ship it", "value": "ship"}])
        seen = _observe(adapter)
        update, query = _query(adapter, _minted_callback_data(adapter), user_id="999")

        await _tap(adapter, update, allowed="777")

        assert seen == []
        assert "not authorized" in query.answer.await_args[1]["text"].lower()
        # The button survives so the legitimate user can still use it.
        assert len(adapter._action_button_state) == 1
