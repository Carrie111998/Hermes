"""Settings resolution — especially the two-stage answer budget."""

from __future__ import annotations

from kakao_legal_bot.app.config import Settings


def test_default_budget_is_ninety_seconds_plus_three_minutes(monkeypatch):
    # The shared `settings` fixture shortens the budget so tests run fast;
    # this one is about the shipped defaults, so build a bare Settings.
    monkeypatch.delenv("ANSWER_TIMEOUT_S", raising=False)
    monkeypatch.delenv("ANSWER_EXTENSION_S", raising=False)
    fresh = Settings()
    assert fresh.answer_timeout_s == 90.0
    assert fresh.answer_extension_s == 180.0
    assert fresh.total_answer_budget_s == 270.0
    assert "3분내로" in fresh.patience_message()


def test_patience_message_fills_in_the_real_minutes(settings):
    object.__setattr__(settings, "answer_extension_s", 180.0)
    assert settings.patience_message() == (
        "답변을 생성하느라 시간이 걸리고 있습니다. "
        "3분내로 답변드리도록 하겠습니다. 잠시만 기다려주세요."
    )

    object.__setattr__(settings, "answer_extension_s", 600.0)
    assert "10분내로" in settings.patience_message()


def test_patience_message_never_promises_zero_minutes(settings):
    object.__setattr__(settings, "answer_extension_s", 20.0)
    assert "1분내로" in settings.patience_message()


def test_custom_patience_text_with_stray_braces_still_sends(monkeypatch):
    monkeypatch.setenv("PATIENCE_TEXT", "조금만 더 기다려주세요 {이런 중괄호}")
    custom = Settings()
    assert custom.patience_message() == "조금만 더 기다려주세요 {이런 중괄호}"


def test_custom_patience_text_without_a_placeholder_is_used_verbatim(monkeypatch):
    monkeypatch.setenv("PATIENCE_TEXT", "곧 답변드리겠습니다.")
    assert Settings().patience_message() == "곧 답변드리겠습니다."


def test_negative_values_cannot_produce_a_negative_budget(monkeypatch):
    monkeypatch.setenv("ANSWER_TIMEOUT_S", "-5")
    monkeypatch.setenv("ANSWER_EXTENSION_S", "-5")
    assert Settings().total_answer_budget_s == 0.0
