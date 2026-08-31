"""Tests for gateway-generated suggested follow-up actions.

Covers the three seams the feature is built from:

  * ``gateway/suggested_actions.py`` — normalization, response parsing,
    the non-blocking registry, and the auxiliary-model call being routed
    through the ``suggested_actions`` task rather than the main model.
  * ``BasePlatformAdapter`` — the opt-in gate and the numbered-list
    fallback used by platforms without buttons.
  * ``TelegramAdapter`` — inline-keyboard rendering and the ``sa:`` tap
    turning into an ordinary user turn.

Assertions target behavior contracts (what reaches the user, what reaches
the agent) rather than the exact wording of labels.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway import suggested_actions as sa  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _source(chat_id="12345", user_id="777"):
    from gateway.session import SessionSource
    from gateway.platforms.base import Platform

    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="dm",
        user_id=user_id,
        user_name="Tester",
    )


# ===========================================================================
# Normalization
# ===========================================================================

class TestNormalizeActions:

    def test_plain_strings_pass_through(self):
        assert sa.normalize_actions(["Deploy", "Run tests"]) == ["Deploy", "Run tests"]

    def test_dict_shaped_actions_are_flattened(self):
        # Models emit {"label": ...} about as often as bare strings.
        assert sa.normalize_actions(
            [{"label": "Confirm"}, {"text": "Cancel"}]
        ) == ["Confirm", "Cancel"]

    def test_unusable_dict_is_dropped_not_stringified(self):
        # A repr like "{'foo': 'bar'}" on a button is worse than no button.
        assert sa.normalize_actions([{"foo": "bar"}]) == []

    def test_duplicates_collapse_case_insensitively(self):
        assert sa.normalize_actions(["Run", "run", "RUN"]) == ["Run"]

    def test_overlong_label_is_dropped_not_truncated(self):
        # Truncation would let the user tap text they cannot fully read.
        assert sa.normalize_actions(["x" * (sa.MAX_LABEL_LEN + 1)]) == []
        assert sa.normalize_actions(["x" * sa.MAX_LABEL_LEN]) != []

    def test_control_characters_are_rejected(self):
        assert sa.normalize_actions(["bad\x07label"]) == []

    def test_bidi_override_is_rejected(self):
        # A bidi override can make the rendered label differ from what is sent.
        assert sa.normalize_actions(["safe‮label"]) == []

    def test_newlines_collapse_to_single_line(self):
        assert sa.normalize_actions(["multi\nline  text"]) == ["multi line text"]

    def test_single_action_is_valid(self):
        # "Confirm and proceed" is the case a button most clearly beats typing.
        assert sa.normalize_actions(["Confirm and proceed"]) == ["Confirm and proceed"]

    def test_limit_is_enforced(self):
        assert len(sa.normalize_actions(["a", "b", "c", "d", "e", "f"])) == sa.MAX_ACTIONS

    def test_non_list_input_is_safe(self):
        assert sa.normalize_actions(None) == []
        assert sa.normalize_actions(42) == []


# ===========================================================================
# Response parsing
# ===========================================================================

class TestParseResponse:

    def test_plain_json_array(self):
        assert sa._parse_response('["A", "B"]', 4) == ["A", "B"]

    def test_code_fenced_array(self):
        # The prompt forbids fences; small models add them anyway.
        assert sa._parse_response('```json\n["A", "B"]\n```', 4) == ["A", "B"]

    def test_array_embedded_in_prose(self):
        assert sa._parse_response('Sure! ["A"] hope that helps', 4) == ["A"]

    def test_empty_array_means_no_suggestions(self):
        assert sa._parse_response("[]", 4) == []

    def test_garbage_yields_nothing(self):
        assert sa._parse_response("no json here", 4) == []

    def test_empty_response_yields_nothing(self):
        assert sa._parse_response("", 4) == []

    def test_multiple_bracketed_spans_do_not_over_capture(self):
        # The greedy version of this fallback spanned from the first "["
        # to the LAST "]" in the text, so a reply narrating with brackets
        # elsewhere produced a non-JSON span and lost real suggestions.
        assert sa._parse_response(
            'sure! ["A", "B"] (note: item [1] was skipped)', 4
        ) == ["A", "B"]


# ===========================================================================
# Registry
# ===========================================================================

class TestRegistry:

    def setup_method(self):
        sa.reset()

    def test_register_and_resolve_returns_action_text(self):
        set_id = sa.register("sess1", "chat1", ["A", "B"])
        assert sa.resolve(set_id, 1) == "B"

    def test_resolve_is_pop_once(self):
        # Two people tapping one message must start one turn, not two.
        set_id = sa.register("sess1", "chat1", ["A", "B"])
        assert sa.resolve(set_id, 0) == "A"
        assert sa.resolve(set_id, 1) is None

    def test_new_set_supersedes_the_previous_one_for_a_session(self):
        old = sa.register("sess1", "chat1", ["old"])
        new = sa.register("sess1", "chat1", ["new"])
        assert sa.resolve(old, 0) is None
        assert sa.resolve(new, 0) == "new"

    def test_empty_actions_register_to_nothing(self):
        assert sa.register("sess1", "chat1", []) is None

    def test_out_of_range_index_does_not_consume_the_set(self):
        set_id = sa.register("sess1", "chat1", ["A"])
        assert sa.resolve(set_id, 9) is None
        assert sa.resolve(set_id, 0) == "A"

    def test_unknown_set_id_resolves_to_none(self):
        assert sa.resolve("nope", 0) is None

    def test_clear_session_drops_pending_set(self):
        set_id = sa.register("sess1", "chat1", ["A"])
        sa.clear_session("sess1")
        assert sa.resolve(set_id, 0) is None

    def test_registry_is_bounded_without_a_reaper(self):
        for i in range(sa.MAX_TRACKED_SESSIONS + 25):
            sa.register(f"sess{i}", "chat", ["A"])
        assert len(sa._sets) <= sa.MAX_TRACKED_SESSIONS

    def test_expired_set_resolves_to_none(self):
        # A quiet chat that never gets a follow-up reply must not leave a
        # button tappable indefinitely.
        set_id = sa.register("sess1", "chat1", ["A"], ttl_seconds=0.0)
        assert sa.resolve(set_id, 0) is None

    def test_expired_set_is_invisible_to_get(self):
        set_id = sa.register("sess1", "chat1", ["A"], ttl_seconds=0.0)
        assert sa.get(set_id) is None

    def test_unexpired_set_survives_get_and_resolve(self):
        set_id = sa.register("sess1", "chat1", ["A"], ttl_seconds=600.0)
        assert sa.get(set_id) is not None
        assert sa.resolve(set_id, 0) == "A"

    def test_expiry_also_clears_the_session_pointer(self):
        # An expired set must not block a later set for the same session
        # from being tracked as the live one.
        set_id = sa.register("sess1", "chat1", ["A"], ttl_seconds=0.0)
        sa.get(set_id)  # triggers the drop
        assert sa._by_session.get("sess1") is None

    def test_default_ttl_is_positive(self):
        # A set registered with the real default must not expire instantly.
        set_id = sa.register("sess1", "chat1", ["A"])
        assert sa.resolve(set_id, 0) == "A"

    def test_source_is_retained_for_the_tap(self):
        src = _source()
        set_id = sa.register("sess1", "chat1", ["A"], source=src)
        assert sa.get(set_id).source is src


# ===========================================================================
# Generation — routed to the auxiliary model, never the main one
# ===========================================================================

class TestGenerate:

    def _response(self, content):
        message = MagicMock()
        message.content = content
        choice = MagicMock()
        choice.message = message
        response = MagicMock()
        response.choices = [choice]
        return response

    def test_uses_the_suggested_actions_auxiliary_task(self):
        # The whole point of the feature: the expensive main model is not
        # asked to decide whether to offer buttons.
        with patch("agent.auxiliary_client.call_llm") as call_llm:
            call_llm.return_value = self._response('["Retry", "Show logs"]')
            actions = sa.generate("Deploy failed.", "deploy it")

        assert actions == ["Retry", "Show logs"]
        assert call_llm.call_args.kwargs["task"] == "suggested_actions"

    def test_reply_and_user_text_reach_the_prompt(self):
        with patch("agent.auxiliary_client.call_llm") as call_llm:
            call_llm.return_value = self._response("[]")
            sa.generate("THE REPLY", "THE QUESTION")

        prompt = call_llm.call_args.kwargs["messages"][0]["content"]
        assert "THE REPLY" in prompt
        assert "THE QUESTION" in prompt

    def test_long_reply_is_clipped(self):
        with patch("agent.auxiliary_client.call_llm") as call_llm:
            call_llm.return_value = self._response("[]")
            sa.generate("x" * (sa.MAX_CONTEXT_CHARS * 3), "q")

        prompt = call_llm.call_args.kwargs["messages"][0]["content"]
        assert len(prompt) < sa.MAX_CONTEXT_CHARS * 3

    def test_backend_failure_yields_no_suggestions(self):
        # The reply is already delivered; a failure here must be invisible.
        with patch("agent.auxiliary_client.call_llm", side_effect=RuntimeError("no backend")):
            assert sa.generate("Some reply", "q") == []

    def test_empty_reply_skips_the_call_entirely(self):
        with patch("agent.auxiliary_client.call_llm") as call_llm:
            assert sa.generate("   ", "q") == []
        call_llm.assert_not_called()

    def test_truncated_response_is_reported_not_silent(self, caplog):
        # Truncation and "the model had no suggestions" are indistinguishable
        # downstream, but only one is a problem the operator can fix.
        import logging

        response = self._response('["Retry", "Show lo')
        response.choices[0].finish_reason = "length"
        with patch("agent.auxiliary_client.call_llm", return_value=response), \
                caplog.at_level(logging.INFO, logger="gateway.suggested_actions"):
            sa.generate("Deploy failed.", "q")

        assert any("truncated" in r.message for r in caplog.records)

    def test_untruncated_response_logs_nothing_about_truncation(self, caplog):
        import logging

        response = self._response('["Retry"]')
        response.choices[0].finish_reason = "stop"
        with patch("agent.auxiliary_client.call_llm", return_value=response), \
                caplog.at_level(logging.INFO, logger="gateway.suggested_actions"):
            assert sa.generate("Deploy failed.", "q") == ["Retry"]

        assert not any("truncated" in r.message for r in caplog.records)

    def test_think_blocks_are_stripped(self):
        with patch("agent.auxiliary_client.call_llm") as call_llm:
            call_llm.return_value = self._response(
                '<think>hmm, maybe retry</think>["Retry"]'
            )
            assert sa.generate("Deploy failed.", "q") == ["Retry"]


# ===========================================================================
# Anchor resolution — which message the keyboard hangs off
# ===========================================================================

class TestAnchorResolution:

    def _result(self, success=True, message_id=None, raw=None):
        r = MagicMock()
        r.success = success
        r.message_id = message_id
        r.raw_response = raw
        return r

    def test_chunked_reply_anchors_on_the_last_chunk(self):
        # message_id names the FIRST chunk; the buttons belong on the last,
        # which is what the user is looking at when they finish reading.
        from gateway.platforms.base import _last_delivered_message_id

        result = self._result(
            message_id="10", raw={"message_ids": ["10", "11", "12"]},
        )
        assert _last_delivered_message_id(result) == "12"

    def test_single_message_uses_its_own_id(self):
        from gateway.platforms.base import _last_delivered_message_id

        assert _last_delivered_message_id(self._result(message_id="42")) == "42"

    def test_failed_send_has_no_anchor(self):
        from gateway.platforms.base import _last_delivered_message_id

        assert _last_delivered_message_id(
            self._result(success=False, message_id="42")
        ) is None

    def test_missing_result_has_no_anchor(self):
        from gateway.platforms.base import _last_delivered_message_id

        assert _last_delivered_message_id(None) is None


# ===========================================================================
# Base adapter — opt-in gate and text fallback
# ===========================================================================

class TestBaseAdapterIntegration:

    def setup_method(self):
        sa.reset()

    @pytest.mark.asyncio
    async def test_disabled_by_default_makes_no_llm_call(self):
        adapter = _make_adapter()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"

        with patch.object(sa, "is_enabled", return_value=False), \
                patch.object(sa, "generate") as generate:
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="sk", reply_text="Done.",
            )
        generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_queued_follow_up_skips_generation(self):
        # The user already typed the next message; suggestions for the
        # previous one are stale, and generating them would delay the drain.
        adapter = _make_adapter()
        adapter._pending_messages["sk"] = MagicMock()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate") as generate:
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="sk", reply_text="Done.",
            )
        generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_generation_failure_never_propagates(self):
        adapter = _make_adapter()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate", side_effect=RuntimeError("boom")):
            # Must not raise: the reply is already delivered.
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="sk", reply_text="Done.",
            )

    @pytest.mark.asyncio
    async def test_buttons_are_delivered_silently(self):
        # The reply they hang off already notified the user; a second ping
        # for an optional shortcut is noise.
        adapter = _make_adapter()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"
        seen = {}

        async def _capture(chat_id, actions, set_id, session_key, metadata=None,
                          anchor_message_id=None):
            seen["metadata"] = metadata
            return MagicMock(success=True)

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate", return_value=["Retry"]), \
                patch.object(adapter, "send_suggested_actions", _capture):
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="sk-quiet", reply_text="Done.",
                metadata={"notify": True, "thread_id": "7"},
            )

        assert "notify" not in seen["metadata"]
        # Unrelated routing metadata must survive the strip.
        assert seen["metadata"]["thread_id"] == "7"

    @pytest.mark.asyncio
    async def test_telegram_send_disables_notification_in_important_mode(self):
        adapter = _make_adapter()
        adapter._notifications_mode = "important"
        msg = MagicMock()
        msg.message_id = 7
        adapter._bot.send_message = AsyncMock(return_value=msg)

        await adapter.send_suggested_actions(
            chat_id="12345", actions=["Retry"], set_id="sid", session_key="sk",
        )

        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs.get("disable_notification") is True

    @pytest.mark.asyncio
    async def test_failed_send_leaves_no_tappable_set_behind(self):
        # A registered id nothing can render is a tap that will only ever
        # report "no longer available".
        adapter = _make_adapter()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate", return_value=["Retry"]), \
                patch.object(adapter, "send_suggested_actions",
                             AsyncMock(return_value=MagicMock(success=False, error="nope"))):
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="sk-fail", reply_text="Done.",
            )

        assert all(e.session_key != "sk-fail" for e in sa._sets.values())

    @pytest.mark.asyncio
    async def test_text_fallback_lists_actions(self):
        # Platforms without buttons must still surface the suggestions.
        from gateway.platforms.base import BasePlatformAdapter

        sent = {}

        async def _send(chat_id, content, metadata=None):
            sent["content"] = content
            return MagicMock(success=True)

        adapter = _make_adapter()
        with patch.object(TelegramAdapter, "send_suggested_actions",
                          BasePlatformAdapter.send_suggested_actions), \
                patch.object(adapter, "send", _send):
            await adapter.send_suggested_actions(
                chat_id="1", actions=["Retry", "Show logs"],
                set_id="sid", session_key="sk",
            )

        assert "Retry" in sent["content"]
        assert "Show logs" in sent["content"]


# ===========================================================================
# Usage accounting — the feature must be visible in session_model_usage
# ===========================================================================

class TestAccountingAttribution:

    def setup_method(self):
        sa.reset()

    def _adapter_with_store(self, session_id="sess-db-1"):
        adapter = _make_adapter()
        store = MagicMock()
        store.peek_session_id = MagicMock(return_value=session_id)
        store._open_session_db_for_active_scope = MagicMock(
            return_value=MagicMock(name="SessionDB")
        )
        adapter._session_store = store
        return adapter

    def test_handles_resolve_from_the_gateway_session_key(self):
        adapter = self._adapter_with_store()
        db, sid = adapter._suggestion_accounting_handles("agent:main:telegram:dm:1")
        assert sid == "sess-db-1"
        assert db is not None

    def test_unmapped_session_key_yields_no_handles(self):
        adapter = self._adapter_with_store(session_id=None)
        assert adapter._suggestion_accounting_handles("nope") == (None, None)

    def test_missing_store_yields_no_handles(self):
        adapter = _make_adapter()
        adapter._session_store = None
        assert adapter._suggestion_accounting_handles("k") == (None, None)

    def test_db_open_failure_is_not_fatal(self):
        # An unrecorded call is a reporting gap; raising would cost the
        # user their buttons.
        adapter = self._adapter_with_store()
        adapter._session_store._open_session_db_for_active_scope.side_effect = (
            RuntimeError("db locked")
        )
        assert adapter._suggestion_accounting_handles("k") == (None, None)

    @pytest.mark.asyncio
    async def test_generation_runs_with_the_accounting_context_published(self):
        from agent import aux_accounting

        adapter = self._adapter_with_store()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"
        seen = {}

        def _generate(reply, user):
            seen["ctx"] = aux_accounting.get_accounting_context()
            return ["Retry"]

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate", _generate), \
                patch.object(adapter, "send_suggested_actions",
                             AsyncMock(return_value=MagicMock(success=True))):
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="k", reply_text="Done.",
            )

        assert seen["ctx"] is not None
        assert seen["ctx"][1] == "sess-db-1"

    @pytest.mark.asyncio
    async def test_accounting_context_is_reset_afterwards(self):
        # A stale handle would misattribute every later aux call on this task.
        from agent import aux_accounting

        adapter = self._adapter_with_store()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate", return_value=["Retry"]), \
                patch.object(adapter, "send_suggested_actions",
                             AsyncMock(return_value=MagicMock(success=True))):
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="k", reply_text="Done.",
            )

        assert aux_accounting.get_accounting_context() is None

    @pytest.mark.asyncio
    async def test_context_is_reset_even_when_generation_raises(self):
        from agent import aux_accounting

        adapter = self._adapter_with_store()
        event = MagicMock()
        event.source = _source()
        event.text = "hi"

        with patch.object(sa, "is_enabled", return_value=True), \
                patch.object(sa, "generate", side_effect=RuntimeError("boom")):
            await adapter._maybe_offer_suggested_actions(
                event=event, session_key="k", reply_text="Done.",
            )

        assert aux_accounting.get_accounting_context() is None


# ===========================================================================
# Telegram — rendering
# ===========================================================================

class TestTelegramRendering:

    def setup_method(self):
        sa.reset()

    @pytest.mark.asyncio
    async def test_renders_one_button_per_action(self):
        adapter = _make_adapter()
        msg = MagicMock()
        msg.message_id = 500
        adapter._bot.send_message = AsyncMock(return_value=msg)

        result = await adapter.send_suggested_actions(
            chat_id="12345", actions=["Retry", "Show logs"],
            set_id="sid1", session_key="sk1",
        )

        assert result.success is True
        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_keyboard_attaches_to_the_reply_not_a_new_message(self):
        # The actions belong to the answer; a carrier message would be a
        # line of chat noise on every turn.
        adapter = _make_adapter()
        adapter._bot.edit_message_reply_markup = AsyncMock()
        adapter._bot.send_message = AsyncMock()

        result = await adapter.send_suggested_actions(
            chat_id="12345", actions=["Retry"], set_id="sid", session_key="sk",
            anchor_message_id="777",
        )

        assert result.success is True
        assert result.message_id == "777"
        adapter._bot.send_message.assert_not_called()
        kwargs = adapter._bot.edit_message_reply_markup.call_args[1]
        assert kwargs["message_id"] == 777
        assert kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_falls_back_to_a_message_without_an_anchor(self):
        adapter = _make_adapter()
        msg = MagicMock()
        msg.message_id = 500
        adapter._bot.send_message = AsyncMock(return_value=msg)

        result = await adapter.send_suggested_actions(
            chat_id="12345", actions=["Retry"], set_id="sid", session_key="sk",
            anchor_message_id=None,
        )

        assert result.success is True
        adapter._bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_when_the_edit_is_refused(self):
        # A reply delivered as media, or one Telegram will no longer edit,
        # still deserves its shortcuts.
        adapter = _make_adapter()
        adapter._bot.edit_message_reply_markup = AsyncMock(
            side_effect=RuntimeError("message can't be edited")
        )
        msg = MagicMock()
        msg.message_id = 501
        adapter._bot.send_message = AsyncMock(return_value=msg)

        result = await adapter.send_suggested_actions(
            chat_id="12345", actions=["Retry"], set_id="sid", session_key="sk",
            anchor_message_id="777",
        )

        assert result.success is True
        adapter._bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_actions_send_nothing(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock()

        result = await adapter.send_suggested_actions(
            chat_id="12345", actions=[], set_id="sid", session_key="sk",
        )

        assert result.success is True
        adapter._bot.send_message.assert_not_called()


# ===========================================================================
# Telegram — tapping a suggestion
# ===========================================================================

class TestTelegramCallback:

    def setup_method(self):
        sa.reset()

    def _query(self, data, user_id="777"):
        query = AsyncMock()
        query.data = data
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.message_id = 900
        query.message.chat.type = "private"
        query.from_user = MagicMock()
        query.from_user.id = user_id
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()
        return query

    @pytest.mark.asyncio
    async def test_tap_starts_a_normal_turn_with_the_action_text(self):
        adapter = _make_adapter()
        set_id = sa.register("sk-cb", "12345", ["Retry", "Show logs"],
                             source=_source())

        query = self._query(f"sa:{set_id}:1")
        update = MagicMock()
        update.callback_query = query

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
                patch.object(adapter, "handle_message", AsyncMock()) as handle:
            await adapter._handle_callback_query(update, MagicMock())

        handle.assert_awaited_once()
        event = handle.await_args[0][0]
        # The injected text is exactly the button label — no hidden payload.
        assert event.text == "Show logs"
        assert event.source.chat_id == "12345"

    @pytest.mark.asyncio
    async def test_tap_removes_the_keyboard_but_never_the_reply_text(self):
        # The keyboard rides on the agent's own reply, so rewriting the
        # message body would erase the answer the user is reading.
        adapter = _make_adapter()
        set_id = sa.register("sk-cb", "12345", ["Retry"], source=_source())
        query = self._query(f"sa:{set_id}:0")
        update = MagicMock()
        update.callback_query = query

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
                patch.object(adapter, "handle_message", AsyncMock()):
            await adapter._handle_callback_query(update, MagicMock())

        query.edit_message_reply_markup.assert_awaited()
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_double_tap_starts_only_one_turn(self):
        adapter = _make_adapter()
        set_id = sa.register("sk-cb", "12345", ["Retry"], source=_source())

        update = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
                patch.object(adapter, "handle_message", AsyncMock()) as handle:
            update.callback_query = self._query(f"sa:{set_id}:0")
            await adapter._handle_callback_query(update, MagicMock())
            update.callback_query = self._query(f"sa:{set_id}:0")
            await adapter._handle_callback_query(update, MagicMock())

        assert handle.await_count == 1

    @pytest.mark.asyncio
    async def test_stale_set_tells_the_user_and_starts_nothing(self):
        adapter = _make_adapter()
        query = self._query("sa:gone:0")
        update = MagicMock()
        update.callback_query = query

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
                patch.object(adapter, "handle_message", AsyncMock()) as handle:
            await adapter._handle_callback_query(update, MagicMock())

        handle.assert_not_awaited()
        query.answer.assert_awaited()

    @pytest.mark.asyncio
    async def test_malformed_callback_data_is_rejected(self):
        adapter = _make_adapter()
        query = self._query("sa:sid:notanumber")
        update = MagicMock()
        update.callback_query = query

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False), \
                patch.object(adapter, "handle_message", AsyncMock()) as handle:
            await adapter._handle_callback_query(update, MagicMock())

        handle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unauthorized_user_cannot_drive_the_agent(self):
        adapter = _make_adapter()
        set_id = sa.register("sk-cb", "12345", ["Retry"], source=_source())
        query = self._query(f"sa:{set_id}:0", user_id="999")
        update = MagicMock()
        update.callback_query = query

        with patch.object(adapter, "_is_callback_user_authorized", return_value=False), \
                patch.object(adapter, "handle_message", AsyncMock()) as handle:
            await adapter._handle_callback_query(update, MagicMock())

        handle.assert_not_awaited()
        # And the suggestion must survive for the legitimate user.
        assert sa.get(set_id) is not None
