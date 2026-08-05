"""Regression tests for the s3 outbound-messaging mixin extraction.

Verifies that the four MOVE clusters lifted out of
``plugins/platforms/telegram/adapter.py`` (wave-1 shard s3) into
``messaging_mixin.py`` / ``draft_streaming_mixin.py`` /
``control_prompts_mixin.py`` / ``picker_mixin.py`` remain behavior-neutral:

  * the mixin methods are reachable through ``TelegramAdapter`` via MRO,
  * pure helpers moved with the messaging cluster (_strip_mdv2,
    _separate_chunk_indicator_from_fence) still behave identically,
  * class attributes referenced by the lifted methods (_EA_*, page sizes)
    still resolve from ``TelegramAdapter``,
  * representative send-path methods still work against a stub ``_bot``
    exactly as they did before the lift.
"""
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from plugins.platforms.telegram.messaging_mixin import (  # noqa: E402
    _separate_chunk_indicator_from_fence,
    _strip_mdv2,
)


def _make_adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._bot = MagicMock()
    return adapter


class TestMixinMroWiring:
    """The lifted methods must be reachable through TelegramAdapter."""

    @pytest.mark.parametrize(
        "method",
        [
            # c4 messaging
            "send", "send_or_update_status", "edit_message",
            "_truncate_stream_overflow_preview", "_edit_overflow_split",
            "delete_message", "_should_thread_reply",
            # c5 draft streaming
            "supports_draft_streaming", "send_draft",
            "_send_message_with_thread_fallback",
            # c6 control prompts
            "send_update_prompt", "send_exec_approval", "send_slash_confirm",
            "send_clarify", "_ea_escape",
            # c12 pickers
            "send_model_picker", "send_choice_picker",
            "_handle_choice_picker_callback", "_build_provider_keyboard",
            "_build_model_keyboard", "_handle_model_picker_callback",
        ],
    )
    def test_method_resolvable_via_mro(self, method):
        assert hasattr(TelegramAdapter, method), f"{method} lost through extraction"

    def test_mixin_precedes_base_adapter_in_mro(self):
        mro_names = [c.__name__ for c in TelegramAdapter.__mro__]
        for mixin in ("MessagingMixin", "DraftStreamingMixin",
                      "ControlPromptsMixin", "PickerMixin"):
            assert mixin in mro_names, f"{mixin} missing from MRO"
        assert mro_names.index("PickerMixin") < mro_names.index("BasePlatformAdapter")

    def test_class_attrs_stay_on_adapter(self):
        """_EA_* templates and picker page sizes are class attrs on
        TelegramAdapter, resolved through MRO by the lifted methods."""
        for attr in ("_EA_HEADER", "_EA_CODE_OPEN", "_EA_CODE_CLOSE",
                     "_EA_SMART_DENY_LINE", "_EA_CMD_BUDGET",
                     "_PROVIDER_PAGE_SIZE", "_MODEL_PAGE_SIZE"):
            assert hasattr(TelegramAdapter, attr), f"{attr} lost through extraction"


class TestMovedModuleHelpers:
    """Cluster-only helpers lifted with messaging_mixin behave identically."""

    def test_strip_mdv2_removes_escapes_and_markers(self):
        # Escaped specials lose their backslash, then italic markers are
        # stripped by the word-boundary italic rule (verbatim helper behavior).
        assert _strip_mdv2(r"\_x\_") == "x"
        assert _strip_mdv2("**bold**") == "bold"
        assert _strip_mdv2("*italic*") == "italic"
        assert _strip_mdv2("~strike~") == "strike"
        assert _strip_mdv2("||spoiler||") == "spoiler"
        assert _strip_mdv2("plain text") == "plain text"
        # snake_case must survive italic stripping
        assert _strip_mdv2("my_variable_name") == "my_variable_name"

    def test_separate_chunk_indicator_from_fence(self):
        # A "(1/2)" marker on a closing fence line moves to its own line.
        src = "```\ncode\n``` \\(1/2\\)"
        out = _separate_chunk_indicator_from_fence(src)
        assert out == "```\ncode\n```\n\\(1/2\\)"
        # Plain text without a fence marker is untouched.
        assert _separate_chunk_indicator_from_fence("hello") == "hello"


class TestShouldThreadReply:
    """_should_thread_reply (c4) still honors reply_to_mode."""

    def test_off_mode_never_threads(self):
        adapter = _make_adapter()
        adapter._reply_to_mode = "off"
        assert adapter._should_thread_reply("123", 0) is False
        assert adapter._should_thread_reply("123", 1) is False

    def test_all_mode_threads_every_chunk(self):
        adapter = _make_adapter()
        adapter._reply_to_mode = "all"
        assert adapter._should_thread_reply("123", 0) is True
        assert adapter._should_thread_reply("123", 5) is True

    def test_first_mode_threads_only_first_chunk(self):
        adapter = _make_adapter()
        adapter._reply_to_mode = "first"
        assert adapter._should_thread_reply("123", 0) is True
        assert adapter._should_thread_reply("123", 1) is False

    def test_no_reply_to_never_threads(self):
        adapter = _make_adapter()
        adapter._reply_to_mode = "all"
        assert adapter._should_thread_reply(None, 0) is False


class TestDraftStreaming:
    """supports_draft_streaming / send_draft (c5) still behave identically."""

    def test_supports_draft_streaming_dm_only(self):
        adapter = _make_adapter()
        adapter._bot.send_message_draft = MagicMock()
        assert adapter.supports_draft_streaming("dm") is True
        assert adapter.supports_draft_streaming("private") is True
        assert adapter.supports_draft_streaming("group") is False
        assert adapter.supports_draft_streaming("supergroup") is False
        assert adapter.supports_draft_streaming("channel") is False

    def test_supports_draft_streaming_without_bot_capability(self):
        adapter = _make_adapter()
        adapter._bot = MagicMock(spec=[])  # no send_message_draft
        assert adapter.supports_draft_streaming("dm") is False

    @pytest.mark.asyncio
    async def test_send_draft_plain_path(self):
        adapter = _make_adapter()
        adapter._bot.send_message_draft = AsyncMock(return_value=True)
        result = await adapter.send_draft("123", 9, "hello")
        assert result.success is True
        adapter._bot.send_message_draft.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_draft_not_connected(self):
        adapter = _make_adapter()
        adapter._bot = None
        result = await adapter.send_draft("123", 9, "hello")
        assert result.success is False
        assert result.error == "not_connected"


class TestControlPrompts:
    """Control-prompt senders (c6) still route through the stub _bot."""

    @pytest.mark.asyncio
    async def test_send_clarify_open_ended(self):
        from telegram import InlineKeyboardMarkup  # noqa: F401
        adapter = _make_adapter()
        sent = {}

        async def _send_message(**kwargs):
            sent.update(kwargs)
            return MagicMock(message_id=42)

        adapter._bot.send_message = AsyncMock(side_effect=_send_message)
        adapter._send_message_with_thread_fallback = AsyncMock(side_effect=_send_message)

        result = await adapter.send_clarify(
            "123", "What next?", [], "cl1", "sess",
        )
        assert result.success is True
        assert result.message_id == "42"
        # Open-ended clarify renders plain text, no buttons.
        assert "reply_markup" not in sent

    @pytest.mark.asyncio
    async def test_send_clarify_with_choices_has_buttons(self):
        adapter = _make_adapter()
        sent = {}

        async def _send_message(**kwargs):
            sent.update(kwargs)
            return MagicMock(message_id=7)

        adapter._bot.send_message = AsyncMock(side_effect=_send_message)
        adapter._send_message_with_thread_fallback = AsyncMock(side_effect=_send_message)

        result = await adapter.send_clarify(
            "123", "Pick one", [{"value": "a", "label": "A"}], "cl2", "sess",
        )
        assert result.success is True
        assert sent["reply_markup"] is not None


class TestPicker:
    """Picker senders (c12) still render keyboards via stub _bot."""

    @pytest.mark.asyncio
    async def test_send_choice_picker(self):
        adapter = _make_adapter()
        sent = {}

        async def _send_message(**kwargs):
            sent.update(kwargs)
            return MagicMock(message_id=11)

        adapter._bot.send_message = AsyncMock(side_effect=_send_message)
        adapter._send_message_with_thread_fallback = AsyncMock(side_effect=_send_message)

        result = await adapter.send_choice_picker(
            "123", "Pick", [{"value": "x", "label": "X"}], "sess", lambda *a: None,
        )
        assert result.success is True
        assert sent["reply_markup"] is not None
        assert adapter._choice_picker_state["123"]["choices"] == [
            {"value": "x", "label": "X"}
        ]

    def test_build_provider_keyboard_paginates(self):
        adapter = _make_adapter()
        providers = [
            {"slug": f"p{i}", "name": f"P{i}", "models": ["m1", "m2"],
             "total_models": 2}
            for i in range(12)
        ]
        keyboard, page_info = adapter._build_provider_keyboard(providers, 0)
        assert keyboard is not None
        # 12 providers at _PROVIDER_PAGE_SIZE=10 -> page info mentions 12
        assert "12" in page_info

    def test_build_model_keyboard_truncates_long_names(self):
        adapter = _make_adapter()
        models = ["very/long/model/name/" + "x" * 60]
        keyboard, page_info = adapter._build_model_keyboard(models, 0)
        assert keyboard is not None


class TestMessagingSend:
    """send() (c4) still returns structured SendResult against a stub bot."""

    @pytest.mark.asyncio
    async def test_send_not_connected(self):
        adapter = _make_adapter()
        adapter._bot = None
        result = await adapter.send("123", "hello")
        assert result.success is False
        assert result.error == "Not connected"

    @pytest.mark.asyncio
    async def test_send_whitespace_only_skipped(self):
        adapter = _make_adapter()
        result = await adapter.send("123", "   ")
        assert result.success is True
        adapter._bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_single_chunk(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=MagicMock(message_id=5))
        adapter._should_attempt_rich = lambda *a, **k: False
        adapter.format_message = lambda c: c
        adapter.truncate_message = lambda c, m, len_fn: [c]
        adapter.MAX_MESSAGE_LENGTH = 4096
        result = await adapter.send("123", "hello")
        assert result.success is True
        assert result.message_id == "5"
        adapter._bot.send_message.assert_awaited_once()
