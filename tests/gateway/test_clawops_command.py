from __future__ import annotations

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from hermes_cli import kanban_db as kb


def _make_event(text: str = "/clawops inspect runtime queue") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_id="msg-1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            user_id="kj",
            user_name="KJ",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "main"
    return runner


def test_clawops_command_is_registered_with_alias():
    from hermes_cli.commands import resolve_command

    assert resolve_command("clawops").name == "clawops"
    assert resolve_command("claw").name == "clawops"


@pytest.mark.asyncio
async def test_clawops_command_rejects_raw_text_without_creating_task(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_CLAWOPS_ASSIGNEE", "clawops-test")

    result = await _make_runner()._handle_clawops_command(
        _make_event("/clawops verify Codex runtime health")
    )

    assert "不會建立任務" in result
    assert "clawops_delegate" in result
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_clawops_command_never_routes_secondhand_raw_text(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.delenv("HERMES_CLAWOPS_ASSIGNEE", raising=False)

    result = await _make_runner()._handle_clawops_command(
        _make_event("/clawops 繼續追加 Facebook 社團群組發佈，再10個。之前發佈文案 Hermes 已經傳給 KJ 確認過；後續自動發佈")
    )

    assert "不會建立任務" in result
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_clawops_command_requires_objective():
    result = await _make_runner()._handle_clawops_command(_make_event("/clawops"))

    assert "原文派工已停用" in result
