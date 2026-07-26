"""Telegram restart-resume policy tests."""

from gateway.config import PlatformConfig
from gateway.run import build_resume_recovery_note
from plugins.platforms.telegram.adapter import TelegramAdapter


def test_telegram_restart_resume_remains_interactive_by_default():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test"))

    assert adapter.interactive_resume is True


def test_telegram_can_continue_interrupted_task_after_restart():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="test",
            extra={"resume_interrupted_tasks": True},
        )
    )

    assert adapter.interactive_resume is False
    note = build_resume_recovery_note(
        "restart_timeout",
        "",
        interactive=adapter.interactive_resume,
    )
    assert "CONTINUE the interrupted task" in note
    assert "ask questions" in note
    assert "skip any unfinished work" not in note
