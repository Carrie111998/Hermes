"""Should the bot say anything at all?

Cheap, pure, fully unit-testable. Nothing here touches the network — the
webhook path calls ``decide()`` before spending a single token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .config import Settings
from .iris import IrisEvent

# Bot commands are prefixed. Both the slash and the Korean full-width
# variants people actually type on mobile are accepted.
_COMMAND_PREFIXES = ("/", "!", "／")

# Lawyer-only commands. Anything else is treated as a client message.
LAWYER_COMMANDS = {
    "개입": "takeover_on",
    "개입해제": "takeover_off",
    "복귀": "takeover_off",
    "조용": "mute_on",
    "음소거": "mute_on",
    "재개": "mute_off",
    "자동": "auto_on",
    "수동": "auto_off",
    "초안": "draft_list",
    "초안목록": "draft_list",
    "승인": "draft_approve",
    "발송": "draft_send",
    "상태": "status",
}

CLIENT_COMMANDS = {
    "도움말": "help",
    "help": "help",
    "이메일": "set_email",
    # 변호사 셀프 등록 — ADMIN_TOKEN 을 아는 사람만 통과합니다.
    "등록": "register_lawyer",
    "메일": "set_email",
    "변호사": "escalate",
    "변호사님": "escalate",
}


class Action(str, Enum):
    IGNORE = "ignore"
    ANSWER = "answer"
    COMMAND = "command"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""
    question: str = ""
    command: str = ""
    args: str = ""
    is_lawyer: bool = False
    mentioned: bool = False
    extras: dict[str, str] = field(default_factory=dict)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def strip_mention(text: str, names: list[str]) -> str:
    """Remove the leading bot name / @mention from a question."""
    cleaned = _normalise(text)
    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        pattern = rf"^\s*@?{re.escape(name)}\s*[,:!?~아야님]*\s*"
        new = re.sub(pattern, "", cleaned, count=1, flags=re.IGNORECASE)
        if new != cleaned:
            return new.strip()
    # Trailing call ("...알려줘 모아") is also common in Korean.
    for name in sorted(names, key=len, reverse=True):
        if not name:
            continue
        new = re.sub(rf"\s*@?{re.escape(name)}\s*[.!?~]*$", "", cleaned, count=1, flags=re.IGNORECASE)
        if new != cleaned:
            return new.strip()
    return cleaned


def mentions_bot(text: str, names: list[str]) -> bool:
    lowered = (text or "").lower()
    return any(name and name.lower() in lowered for name in names)


def parse_command(text: str) -> tuple[str, str] | None:
    """``/승인 12`` → ``("승인", "12")``; not a command → ``None``."""
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in _COMMAND_PREFIXES:
        return None
    body = stripped[1:].strip()
    if not body:
        return None
    head, _, rest = body.partition(" ")
    # "/모아 도움말" — the bot name may be part of the command.
    return head.strip(), rest.strip()


def bot_names(settings: Settings) -> list[str]:
    names = [settings.bot_name, *settings.bot_aliases]
    return [name for name in dict.fromkeys(names) if name]


def is_lawyer(
    event: IrisEvent, settings: Settings, extra_ids: frozenset[str] | set[str] = frozenset()
) -> bool:
    """변호사인가 — 설정의 아이디 목록에 더해, /등록 으로 등록한 계정도.

    ``extra_ids`` 는 서버가 켜진 뒤 `/등록 <토큰>` 으로 들어온 아이디입니다.
    환경변수를 고치고 재배포하지 않아도 변호사 권한이 붙습니다.
    """
    if settings.lawyer_room_id and event.room_id == settings.lawyer_room_id:
        return True
    ids = {value.lower() for value in settings.lawyer_kakao_ids}
    ids.update(value.lower() for value in extra_ids)
    if not ids:
        return False
    return event.sender_id.lower() in ids or event.sender_name.lower() in ids


def decide(
    event: IrisEvent,
    settings: Settings,
    *,
    room_kind: str = "unknown",
    muted: bool = False,
    lawyer_takeover: bool = False,
    extra_lawyer_ids: frozenset[str] | set[str] = frozenset(),
) -> Decision:
    names = bot_names(settings)
    lawyer = is_lawyer(event, settings, extra_lawyer_ids)

    if not event.is_text:
        return Decision(Action.IGNORE, reason="non-text message")

    text = _normalise(event.text)
    if not text:
        return Decision(Action.IGNORE, reason="empty message")

    # Never answer ourselves. Iris echoes the bot's own sends back through
    # the webhook, and without this the bot talks to itself forever.
    if event.sender_name and event.sender_name in names:
        return Decision(Action.IGNORE, reason="own message")
    if event.sender_id and event.sender_id in settings.ignore_senders:
        return Decision(Action.IGNORE, reason="ignored sender")
    if event.sender_name and event.sender_name in settings.ignore_senders:
        return Decision(Action.IGNORE, reason="ignored sender")

    parsed = parse_command(text)
    if parsed is not None:
        head, rest = parsed
        # Allow "/모아 도움말" as well as "/도움말".
        if head in names and rest:
            head, _, rest = rest.partition(" ")
            head, rest = head.strip(), rest.strip()
        if lawyer and head in LAWYER_COMMANDS:
            return Decision(
                Action.COMMAND,
                command=LAWYER_COMMANDS[head],
                args=rest,
                is_lawyer=True,
                reason=f"lawyer command {head}",
            )
        if head in CLIENT_COMMANDS:
            return Decision(
                Action.COMMAND,
                command=CLIENT_COMMANDS[head],
                args=rest,
                is_lawyer=lawyer,
                reason=f"client command {head}",
            )
        if head in LAWYER_COMMANDS and not lawyer:
            return Decision(Action.IGNORE, reason="lawyer-only command from non-lawyer")
        # Unknown slash command: fall through and treat it as a question.

    mentioned = mentions_bot(text, names)

    if muted:
        return Decision(Action.IGNORE, reason="room muted", mentioned=mentioned)

    # While the lawyer is in the room the bot stays quiet unless called by
    # name — two voices answering the same client is worse than one.
    if lawyer_takeover and not mentioned:
        return Decision(Action.IGNORE, reason="lawyer takeover", mentioned=mentioned)

    # The lawyer's own chatter is not a question for the bot.
    if lawyer and not mentioned:
        return Decision(Action.IGNORE, reason="lawyer speaking", is_lawyer=True)

    auto_room = settings.auto_answer_direct_rooms and (
        room_kind == "direct" or (room_kind == "unknown" and event.is_direct_chat is True)
    )
    if not mentioned and not auto_room:
        return Decision(Action.IGNORE, reason="not addressed")

    question = strip_mention(text, names) if mentioned else text
    if len(question) < settings.min_question_chars:
        return Decision(
            Action.IGNORE, reason="question too short", mentioned=mentioned, question=question
        )

    return Decision(
        Action.ANSWER,
        question=question,
        mentioned=mentioned,
        is_lawyer=lawyer,
        reason="mention" if mentioned else "direct room",
    )


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def extract_email(text: str) -> str:
    match = EMAIL_RE.search(text or "")
    return match.group(0) if match else ""
