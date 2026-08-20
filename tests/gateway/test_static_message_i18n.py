"""Regression tests for static gateway text that previously ignored display.language."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

def test_non_english_reset_omits_untranslated_english_tip(monkeypatch):
    from agent import i18n
    from gateway import slash_commands

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    try:
        helper = getattr(slash_commands, "_localized_reset_tip", None)
        assert helper is not None, "reset-tip localization helper is missing"
        assert helper() == ""
    finally:
        i18n.reset_language_cache()


def test_telegram_slash_confirm_labels_follow_german_language(monkeypatch):
    from agent import i18n
    from plugins.platforms.telegram import adapter

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    try:
        helper = getattr(adapter, "_slash_confirm_labels", None)
        assert helper is not None, "Telegram slash-confirm localization helper is missing"
        labels = helper()
    finally:
        i18n.reset_language_cache()

    assert labels["button_once"] == "✅ Einmal genehmigen"
    assert labels["button_always"] == "🔒 Dauerhaft genehmigen"
    assert labels["button_cancel"] == "❌ Abbrechen"
    assert labels["result_always"] == "🔒 Dauerhaft genehmigt"
    assert labels["by_user"].format(label=labels["result_always"], user="Semih") == (
        "🔒 Dauerhaft genehmigt von Semih"
    )


@pytest.mark.asyncio
async def test_telegram_send_slash_confirm_uses_localized_button_labels(monkeypatch):
    from agent import i18n
    from gateway.config import Platform
    from plugins.platforms.telegram import adapter as adapter_module

    class FakeButton:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

    class FakeMarkup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", FakeButton)
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", FakeMarkup)
    monkeypatch.setattr(
        adapter_module,
        "ParseMode",
        SimpleNamespace(MARKDOWN_V2="MarkdownV2"),
    )
    i18n.reset_language_cache()

    adapter = object.__new__(adapter_module.TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter._bot = object()
    adapter._reply_to_mode = "off"
    adapter._slash_confirm_state = {}
    adapter.format_message = lambda text: text
    adapter._truncate_preview = lambda text, _limit: text
    adapter._metadata_thread_id = lambda _metadata: None
    adapter._reply_to_message_id_for_send = lambda *_args, **_kwargs: None
    adapter._thread_kwargs_for_send = lambda *_args, **_kwargs: {}
    adapter._link_preview_kwargs = lambda: {}
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=42)
    )

    try:
        result = await adapter.send_slash_confirm(
            chat_id="123",
            title="/new",
            message="Bestätigen?",
            session_key="session-1",
            confirm_id="confirm-1",
        )
    finally:
        i18n.reset_language_cache()

    assert result.success
    markup = adapter._send_message_with_thread_fallback.call_args.kwargs["reply_markup"]
    assert [[button.text for button in row] for row in markup.inline_keyboard] == [
        ["✅ Einmal genehmigen", "🔒 Dauerhaft genehmigen"],
        ["❌ Abbrechen"],
    ]


@pytest.mark.asyncio
async def test_telegram_send_clarify_uses_localized_other_button(monkeypatch):
    from agent import i18n
    from gateway.config import Platform
    from plugins.platforms.telegram import adapter as adapter_module

    class FakeButton:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

    class FakeMarkup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    monkeypatch.setattr(adapter_module, "InlineKeyboardButton", FakeButton)
    monkeypatch.setattr(adapter_module, "InlineKeyboardMarkup", FakeMarkup)
    monkeypatch.setattr(
        adapter_module,
        "ParseMode",
        SimpleNamespace(HTML="HTML"),
    )
    i18n.reset_language_cache()

    adapter = object.__new__(adapter_module.TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter._bot = object()
    adapter._clarify_state = {}
    adapter._link_preview_kwargs = lambda: {}
    adapter._metadata_thread_id = lambda _metadata: None
    adapter._reply_to_message_id_for_send = lambda *_args, **_kwargs: None
    adapter._thread_kwargs_for_send = lambda *_args, **_kwargs: {}
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=43)
    )

    try:
        result = await adapter.send_clarify(
            chat_id="123",
            question="Welche Option?",
            choices=["Eins", "Zwei"],
            clarify_id="clarify-1",
            session_key="session-1",
        )
    finally:
        i18n.reset_language_cache()

    assert result.success
    markup = adapter._send_message_with_thread_fallback.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[-1][0].text == "✏️ Andere Antwort eingeben"


def test_additional_visible_telegram_status_text_is_german(monkeypatch):
    from agent import i18n
    from gateway import run as gateway_run
    from plugins.platforms.telegram import adapter

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    try:
        clarify_helper = getattr(adapter, "_clarify_labels", None)
        heartbeat_helper = getattr(gateway_run, "_format_long_running_heartbeat", None)
        assert clarify_helper is not None, "Telegram clarify localization helper is missing"
        assert heartbeat_helper is not None, "heartbeat localization helper is missing"
        labels = clarify_helper()
        heartbeat = heartbeat_helper(
            minutes=6,
            detail=" — " + i18n.t("gateway.working.iteration", current=4, maximum=500),
        )
        restart_success = i18n.t("gateway.restart.success")
        restart_online = i18n.t("gateway.restart.online")
    finally:
        i18n.reset_language_cache()

    assert labels["other_button"] == "✏️ Andere Antwort eingeben"
    assert labels["type_answer"] == "✏️ Gib deine Antwort im Chat ein."
    assert labels["awaiting_typed"].format(user="Semih") == (
        "Antwort von Semih wird erwartet …"
    )
    assert heartbeat == "⏳ Läuft seit 6 Min. — Durchlauf 4/500"
    assert restart_success == "♻ Gateway erfolgreich neu gestartet. Deine Sitzung wird fortgesetzt."
    assert restart_online == "♻️ Gateway online – Hermes ist wieder bereit."


def test_english_heartbeat_and_restart_text_stay_backward_compatible(monkeypatch):
    from agent import i18n
    from gateway.run import _format_long_running_heartbeat

    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    i18n.reset_language_cache()
    try:
        heartbeat = _format_long_running_heartbeat(
            minutes=6,
            detail=" — " + i18n.t("gateway.working.iteration", current=4, maximum=500),
        )
        restart_success = i18n.t("gateway.restart.success")
        restart_online = i18n.t("gateway.restart.online")
    finally:
        i18n.reset_language_cache()

    assert heartbeat == "⏳ Working — 6 min — iteration 4/500"
    assert restart_success == "♻ Gateway restarted successfully. Your session continues."
    assert restart_online == "♻️ Gateway online — Hermes is back and ready."


def test_german_telegram_topic_guidance_is_localized(monkeypatch):
    from agent import i18n
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._is_telegram_topic_lane = lambda _source: True
    try:
        lobby = runner._telegram_topic_root_lobby_message()
        root_new = runner._telegram_topic_root_new_message()
        topic_new = runner._telegram_topic_new_header(SimpleNamespace())
    finally:
        i18n.reset_language_cache()

    assert "Hauptchat" in lobby and "Alle Nachrichten" in lobby
    assert "parallelen Hermes-Chat" in root_new
    assert "eigenständige Hermes-Sitzung" in root_new
    assert topic_new is not None
    assert "Neue Hermes-Sitzung" in topic_new
    assert "Started a new" not in topic_new


def test_german_generic_heartbeat_uses_localized_structured_fallback(monkeypatch):
    from agent import i18n
    from gateway.run import _format_long_running_notification

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    generic_calls = []
    try:
        rendered = _format_long_running_notification(
            mode="generic",
            minutes=6,
            detail=" — Durchlauf 4/500",
            generic_phrase=lambda: generic_calls.append(True) or "still on it",
        )
    finally:
        i18n.reset_language_cache()

    assert rendered == "⏳ Läuft seit 6 Min. — Durchlauf 4/500"
    assert generic_calls == []


def test_english_generic_heartbeat_keeps_existing_phrase_catalog(monkeypatch):
    from agent import i18n
    from gateway.run import _format_long_running_notification

    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    i18n.reset_language_cache()
    try:
        rendered = _format_long_running_notification(
            mode="generic",
            minutes=6,
            detail="",
            generic_phrase=lambda: "still on it",
        )
    finally:
        i18n.reset_language_cache()

    assert rendered == "still on it"


def test_german_generic_heartbeat_respects_custom_phrase_catalog(monkeypatch):
    from agent import i18n
    from gateway.run import _format_long_running_notification

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    try:
        rendered = _format_long_running_notification(
            mode="generic",
            minutes=6,
            detail="",
            generic_phrase=lambda: "arbeite noch",
            has_custom_phrase_catalog=True,
        )
    finally:
        i18n.reset_language_cache()

    assert rendered == "arbeite noch"


def test_german_restart_and_destructive_wording_uses_natural_du(monkeypatch):
    from agent import i18n

    monkeypatch.setenv("HERMES_LANGUAGE", "de")
    i18n.reset_language_cache()
    try:
        restart = i18n.t("gateway.restart.restarting")
        new_detail = i18n.t("gateway.destructive_slash.new_detail")
        undo_many = i18n.t("gateway.destructive_slash.undo_many_detail", count=3)
        prompt = i18n.t(
            "gateway.destructive_slash.confirm_prompt",
            command="undo",
            detail=undo_many,
            prefix="/",
        )
    finally:
        i18n.reset_language_cache()

    assert "Falls du innerhalb von 60 Sekunden keine Benachrichtigung erhältst" in restart
    assert " Sie " not in f" {restart} "
    assert "eine neue Sitzung" in new_detail
    assert "frische Sitzung" not in new_detail
    assert "deine letzten 3 eingaben und die zugehörigen antworten" in undo_many.lower()
    assert "Nutzerrunden" not in undo_many
    assert "Dauerhaft genehmigen" in prompt
    assert "Sicherheitsabfrage künftig überspringen" in prompt