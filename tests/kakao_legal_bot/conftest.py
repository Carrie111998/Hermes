"""Fixtures for the KakaoTalk legal bot tests.

The bot's Settings read os.environ at construction, and the repo-wide
conftest scrubs credential-shaped env vars before every test, so each
fixture sets exactly what it needs and nothing leaks between tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kakao_legal_bot.app.config import Settings
from kakao_legal_bot.app.db import Database
from kakao_legal_bot.app.iris import IrisEvent


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    env = {
        "DATA_DIR": str(tmp_path / "data"),
        "BOT_NAME": "모아",
        "BOT_ALIASES": "모아,moa",
        "LAWYER_NAME": "김변호사",
        "LAWYER_ROOM_ID": "lawyer-room",
        "LAWYER_KAKAO_IDS": "lawyer-uid",
        "IRIS_BASE_URL": "http://iris.test",
        "IRIS_SEND_MODE": "direct",
        "OUTBOX_TOKEN": "outbox-token",
        "ADMIN_TOKEN": "admin-token",
        "ANTHROPIC_API_KEY": "test-key",
        "ACK_DEADLINE_MS": "100",
        "ANSWER_TIMEOUT_S": "5",
        "ROOM_COOLDOWN_S": "0",
        "PERSONA_PATH": str(Path(__file__).resolve().parents[2] / "kakao_legal_bot" / "persona.md"),
        "PSEUDONYM_SALT": "test-salt",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


def make_event(
    text: str,
    *,
    room_id: str = "room-1",
    room_name: str = "테스트방",
    sender: str = "홍길동",
    sender_id: str = "uid-1",
    msg_type: str = "1",
    log_id: str = "",
    direct: bool | None = None,
) -> IrisEvent:
    return IrisEvent(
        room_id=room_id,
        room_name=room_name,
        sender_name=sender,
        sender_id=sender_id,
        text=text,
        msg_type=msg_type,
        log_id=log_id or f"log-{abs(hash(text)) % 10**8}",
        created_at=0.0,
        is_direct_chat=direct,
        raw={},
    )


class FakeSender:
    """Records what would have gone to KakaoTalk."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.lawyer_notes: list[str] = []

    async def send(self, room_id: str, text: str, *, record_role: str = "bot") -> bool:
        self.sent.append((room_id, text))
        return True

    async def notify_lawyer(self, text: str) -> bool:
        self.lawyer_notes.append(text)
        return True

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.sent]


class FakeAgent:
    """Stands in for LegalAgent — answers after a controllable delay."""

    def __init__(self, text: str = "답변입니다.", delay: float = 0.0, **kwargs: Any) -> None:
        self.text = text
        self.delay = delay
        self.calls: list[str] = []
        self.draft_body = "초안 본문"
        self.extra = kwargs

    async def answer(self, question: str, history: list[Any]) -> Any:
        import asyncio

        from kakao_legal_bot.app.agent import AnswerResult
        from kakao_legal_bot.app.tools import TurnState

        self.calls.append(question)
        if self.delay:
            await asyncio.sleep(self.delay)
        state = TurnState()
        state.draft_request = self.extra.get("draft_request")
        state.escalation = self.extra.get("escalation")
        state.citations = list(self.extra.get("citations") or [])
        return AnswerResult(
            text=self.text, state=state, latency_ms=1, tools_used=list(self.extra.get("tools") or [])
        )

    async def draft_document(self, kind: str, title: str, instructions: str, history: list[Any]) -> str:
        return self.draft_body
