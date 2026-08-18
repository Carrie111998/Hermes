"""Tests for the Telegram model-rate-limit reroute callback (Task 7).

Mirrors tests/gateway/test_telegram_approval_buttons.py's
TestTelegramApprovalCallback shape for the new ``rl:action:token`` branch
of ``_handle_callback_query``.

The token is deliberately opaque -- it never carries the model name (see
events/override_buttons.py). These tests drive the handler through
events.override_callback_state exactly the way
events/subscribers/telegram_notifier.py populates it when it sends the
buttons, then simulate the tap and assert on the resulting
events.model_override store.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported (mirrors
# test_telegram_approval_buttons.py / test_telegram_clarify_buttons.py)
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(data, user_id="12345", first_name="Norbert"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = first_name
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _make_update(query):
    update = MagicMock()
    update.callback_query = query
    return update


@pytest.fixture(autouse=True)
def _isolated_override_state(tmp_path, monkeypatch):
    """Isolate events.model_override's file store and
    events.override_callback_state's in-memory map for every test in this
    module (mirrors tests/events/test_model_override.py's ``ov`` fixture)."""
    store_path = tmp_path / "model_overrides.json"
    monkeypatch.setattr("events.model_override._store_path", lambda: store_path)
    from events import model_override, override_callback_state

    model_override.reset_cache()
    override_callback_state.reset()
    yield
    override_callback_state.reset()
    model_override.reset_cache()


def _record_target(token, provider="deepseek", model="deepseek-v4-pro",
                    replacement_provider="openai-codex", replacement_model="gpt-5.6-sol"):
    from events import override_callback_state
    override_callback_state.record(
        token,
        provider=provider,
        model=model,
        replacement_provider=replacement_provider,
        replacement_model=replacement_model,
    )


class TestRateLimitDivertCallback:
    """rl:divert:token — authorized tap writes the override and retires
    the buttons."""

    @pytest.mark.asyncio
    async def test_authorized_tap_writes_override_and_retires_buttons(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok1")
        query = _make_query("rl:divert:tok1")
        update = _make_update(query)
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        rec = get_override("deepseek", "deepseek-v4-pro")
        assert rec is not None, "authorized divert tap must write an override"
        assert rec["replacement_provider"] == "openai-codex"
        assert rec["replacement_model"] == "gpt-5.6-sol"
        assert rec["set_by"] == "telegram:12345"

        query.answer.assert_called_once()
        assert "diverted" in query.answer.call_args[1]["text"].lower()

        query.edit_message_text.assert_called_once()
        edit_kwargs = query.edit_message_text.call_args[1]
        assert edit_kwargs["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_set_by_identifies_the_tapping_user(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-user")
        query = _make_query("rl:divert:tok-user", user_id="98765")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        rec = get_override("deepseek", "deepseek-v4-pro")
        assert rec["set_by"] == "telegram:98765"


class TestRateLimitUnauthorizedCallback:
    """An unauthorized tap must write NOTHING."""

    @pytest.mark.asyncio
    async def test_unauthorized_tap_writes_nothing(self):
        from events.model_override import get_override
        from events import override_callback_state

        adapter = _make_adapter()
        _record_target("tok-unauth")
        query = _make_query("rl:divert:tok-unauth", user_id="222", first_name="Mallory")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "67890"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.edit_message_text.assert_not_called()
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()

        # The token must still be there for a legitimate follow-up tap —
        # an unauthorized attempt must not consume or corrupt state.
        assert override_callback_state.pop("tok-unauth") is not None


class TestRateLimitUnknownTokenCallback:
    """An unknown/expired token must answer 'already resolved' and write
    nothing."""

    @pytest.mark.asyncio
    async def test_unknown_token_writes_nothing(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        # No _record_target call — token was never issued.
        query = _make_query("rl:divert:ghost-token")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.answer.assert_called_once()
        assert "already been resolved" in query.answer.call_args[1]["text"]
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_double_tap_is_idempotent(self):
        """Second tap on the same token finds nothing and no-ops."""
        from events.model_override import get_override, clear_override

        adapter = _make_adapter()
        _record_target("tok-double")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            query1 = _make_query("rl:divert:tok-double")
            await adapter._handle_callback_query(_make_update(query1), MagicMock())
            assert get_override("deepseek", "deepseek-v4-pro") is not None

            # Simulate the operator un-diverting in between — a second tap
            # on the same (already-consumed) token must NOT re-write it.
            clear_override(provider="deepseek", model="deepseek-v4-pro", cleared_by="test")
            assert get_override("deepseek", "deepseek-v4-pro") is None

            query2 = _make_query("rl:divert:tok-double")
            await adapter._handle_callback_query(_make_update(query2), MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None, (
            "a second tap on an already-consumed token must not write anything"
        )
        query2.answer.assert_called_once()
        assert "already been resolved" in query2.answer.call_args[1]["text"]
        query2.edit_message_text.assert_not_called()


class TestRateLimitRejectedWrite:
    """A refused write (target already limited / self-target) must surface
    the reason, not silently do nothing."""

    @pytest.mark.asyncio
    async def test_self_target_rejection_surfaces_reason(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        # A self-target: replacement is the same as the original — set_override
        # rejects this outright (routing loop).
        _record_target(
            "tok-self", provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="deepseek", replacement_model="deepseek-v4-pro",
        )
        query = _make_query("rl:divert:tok-self")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.answer.assert_called_once()
        toast = query.answer.call_args[1]["text"]
        assert "not diverted" in toast.lower()
        assert "loop" in toast.lower() or "itself" in toast.lower()

        # Buttons are still retired -- the token was consumed either way.
        query.edit_message_text.assert_called_once()
        edit_kwargs = query.edit_message_text.call_args[1]
        assert edit_kwargs["reply_markup"] is None


class TestRateLimitBlankTargetRejection:
    """Review Important-1 regression guard: a recorded target with a blank
    replacement (e.g. a runtime detector that emitted outcome="diverted"
    without fallback_provider/fallback_model) must never reach
    set_override -- writing a "/" override would pass enforcement read #1
    (agent_init.py, which rejects a blank replacement) but still trip
    enforcement read #2 (agent_runtime_helpers.py's bare `if override:
    return False`), blocking restoration of the primary model for the
    full 6h TTL while diverting to nothing."""

    @pytest.mark.asyncio
    async def test_blank_replacement_provider_is_refused_and_writes_nothing(self):
        from events.model_override import get_override, list_overrides

        adapter = _make_adapter()
        _record_target(
            "tok-blank", provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="", replacement_model="",
        )
        query = _make_query("rl:divert:tok-blank")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        assert list_overrides() == [], (
            "a blank-target token must never produce ANY override record"
        )

        query.answer.assert_called_once()
        toast = query.answer.call_args[1]["text"]
        assert "not diverted" in toast.lower()

        # The toast must not lie by rendering "Diverted 6h -> /".
        assert "diverted 6h" not in toast.lower()

        # Buttons are still retired -- the token was consumed either way.
        query.edit_message_text.assert_called_once()
        assert query.edit_message_text.call_args[1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_blank_replacement_model_only_is_also_refused(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target(
            "tok-blank-model", provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex", replacement_model="",
        )
        query = _make_query("rl:divert:tok-blank-model")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        toast = query.answer.call_args[1]["text"]
        assert "not diverted" in toast.lower()


class TestRateLimitUnknownAction:
    """Review Minor-5 regression guard: an unrecognized action must be
    rejected BEFORE the token is popped, so it cannot silently consume
    (disarm) a legitimate button."""

    @pytest.mark.asyncio
    async def test_unknown_action_does_not_consume_the_token(self):
        from events import override_callback_state
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-unknown-action")
        query = _make_query("rl:bogus:tok-unknown-action")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.edit_message_text.assert_not_called()
        query.answer.assert_called_once()

        # The real button for this token must still be tappable -- an
        # unknown action must not have disarmed it.
        assert override_callback_state.pop("tok-unknown-action") is not None


class TestRateLimitPayloadCannotNameAModel:
    """Missing-security-test: constraint 3 ("a tap can never name a
    model") currently holds structurally -- the handler never reads a
    fourth field from callback_data. Pin that so a future refactor that
    starts trusting one gets caught."""

    @pytest.mark.asyncio
    async def test_extra_colon_fields_cannot_inject_a_model(self):
        from events.model_override import get_override, list_overrides

        adapter = _make_adapter()
        _record_target(
            "tok-inject", provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="openai-codex", replacement_model="gpt-5.6-sol",
        )
        # A payload that tries to smuggle a target through extra fields
        # after the token. split(":", 2) folds everything past the second
        # colon into a single opaque "token" string -- there is no fourth
        # field the handler ever reads.
        query = _make_query("rl:divert:tok-inject:evilprovider:evilmodel")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        # Whatever happened (token mismatch -> no-op, or a match -> the
        # recorded target), the attacker-supplied strings must never
        # appear in any written override.
        for rec in list_overrides():
            assert rec.get("replacement_provider") != "evilprovider"
            assert rec.get("replacement_model") != "evilmodel"
        assert get_override("evilprovider", "evilmodel") is None
        assert get_override("deepseek", "evilmodel") is None

        # And the legitimately recorded target, if it was written at all,
        # must be exactly the recorded one -- never the payload-supplied
        # strings.
        rec = get_override("deepseek", "deepseek-v4-pro")
        if rec is not None:
            assert rec["replacement_provider"] == "openai-codex"
            assert rec["replacement_model"] == "gpt-5.6-sol"


class TestRateLimitChooseAndDismiss:
    @pytest.mark.asyncio
    async def test_choose_does_not_write_an_override(self):
        """"Choose model…" is not wired up yet, so it must be a true no-op:
        acknowledge, write nothing, and leave the message (and therefore the
        keyboard) alone. Replacing the message body with reply_markup=None
        would strip the buttons -- see the test below for why that matters.
        """
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-choose")
        query = _make_query("rl:choose:tok-choose")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.answer.assert_called_once()
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_choose_does_not_consume_the_token(self):
        """The token is single-use and popped BEFORE the action dispatch. If
        "choose" is handled after that pop it burns the token, so the
        alert's only functioning control is gone."""
        from events import override_callback_state

        adapter = _make_adapter()
        _record_target("tok-choose-token")
        query = _make_query("rl:choose:tok-choose-token")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(_make_update(query), MagicMock())

        # No public peek() (and events/override_callback_state.py is out of
        # scope), so read the map directly rather than pop()ping it here.
        assert "tok-choose-token" in override_callback_state._state

    @pytest.mark.asyncio
    async def test_choose_then_divert_still_diverts(self):
        """THE BLOCKER (I3): tapping "Choose model…" first — the most likely
        first tap — must not permanently disarm the working Divert button.

        Before the fix the token was pop()ed before the dispatch and the
        choose branch fell through to edit_message_text(reply_markup=None),
        so the first tap destroyed both the alert body and the only working
        control, and the follow-up Divert tap got "This prompt has already
        been resolved."

        Mutation check: delete the early ``if action == "choose"`` return in
        plugins/platforms/telegram/adapter.py and this test fails on the
        get_override assertion.
        """
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-choose-then-divert")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            choose = _make_query("rl:choose:tok-choose-then-divert")
            await adapter._handle_callback_query(_make_update(choose), MagicMock())

            divert = _make_query("rl:divert:tok-choose-then-divert")
            await adapter._handle_callback_query(_make_update(divert), MagicMock())

        rec = get_override("deepseek", "deepseek-v4-pro")
        assert rec is not None, (
            "the Divert button must still work after a 'Choose model' tap")
        assert rec["replacement_model"] == "gpt-5.6-sol"
        toast = divert.answer.call_args[1]["text"]
        assert "already been resolved" not in toast.lower()
        assert "diverted" in toast.lower()
        # The divert tap is the one that legitimately retires the buttons.
        divert.edit_message_text.assert_called_once()
        assert divert.edit_message_text.call_args[1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_dismiss_retires_buttons_and_writes_nothing(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-dismiss")
        query = _make_query("rl:dismiss:tok-dismiss")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.edit_message_text.assert_called_once()
        assert query.edit_message_text.call_args[1]["reply_markup"] is None
