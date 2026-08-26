"""Turning a KakaoTalk message into an answer.

The persona file is the only place the bot's voice and rules live; this
module just assembles it with the room's recent history and hands the
tools to the model.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Message
from .knowledge import case_type_index
from .lawapi.client import LawApiClient
from .llm import LlmClient, LlmError, LlmResult
from .rag.store import RagStore
from .tools import TurnState, build_action_tools, build_intake_tools, build_tools

log = logging.getLogger(__name__)

FALLBACK_PERSONA = """
너는 변호사 사무실의 카카오톡 상담방에서 1차 응대를 맡는 어시스턴트다.
존댓말로, 결론을 먼저 말하고 근거 조문·판례를 인용하며, 사실관계가 부족하면
꼭 필요한 것만 되묻는다. 상담자의 개인정보는 되묻지도 옮겨 적지도 않는다.
최종 법률판단은 담당 변호사의 검토를 거친다는 점을 상담 초기에 한 번 밝힌다.
""".strip()

# Appended to the persona at runtime. Operational facts the persona file
# should not have to hard-code (they change per deployment).
_RUNTIME_BRIEF = """
## 운영 정보

- 지금은 카카오톡 상담방이다. 답변은 한 번에 {max_chars}자 이내로 쓴다.
- 담당 변호사: {lawyer_name}
- 오늘 날짜: {today}
- 도구 호출 결과에 없는 조문 번호·사건번호는 절대 지어내지 않는다.
""".strip()


def load_persona(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        log.warning("persona file not found at %s — using the built-in fallback", path)
        return FALLBACK_PERSONA
    return text or FALLBACK_PERSONA


@dataclass
class AnswerResult:
    text: str
    state: TurnState
    latency_ms: int
    tools_used: list[str] = field(default_factory=list)
    error: str = ""


def build_messages(history: list[Message], question: str, bot_name: str) -> list[dict[str, Any]]:
    """History as an alternating transcript, question last.

    Kakao rooms are multi-party and bursty, so instead of trying to force
    the log into strict user/assistant alternation we hand the model one
    labelled transcript block plus the current question. It keeps who said
    what without inventing turns that never happened.
    """
    lines: list[str] = []
    for message in history:
        if not message.text.strip():
            continue
        if message.role == "bot":
            speaker = bot_name
        elif message.role == "lawyer":
            speaker = "변호사"
        elif message.role == "system":
            speaker = "시스템"
        else:
            speaker = message.sender or "상담자"
        lines.append(f"{speaker}: {message.text.strip()}")

    blocks: list[dict[str, Any]] = []
    if lines:
        transcript = "\n".join(lines[-40:])
        blocks.append(
            {
                "role": "user",
                "content": f"[지금까지의 대화 기록]\n{transcript}",
            }
        )
        blocks.append({"role": "assistant", "content": "대화 기록을 확인했습니다."})
    blocks.append({"role": "user", "content": question})
    return blocks


class LegalAgent:
    def __init__(
        self,
        settings: Settings,
        llm: LlmClient,
        *,
        rag: RagStore | None = None,
        law: LawApiClient | None = None,
        embed_query: Any = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.rag = rag
        self.law = law
        self.embed_query = embed_query
        self._persona = load_persona(settings.persona_path)
        self._playbook = load_persona(settings.intake_playbook_path)

    def reload_persona(self) -> None:
        self._persona = load_persona(self.settings.persona_path)
        self._playbook = load_persona(self.settings.intake_playbook_path)

    def stable_prefix(self) -> str:
        """The part of the prompt that never changes between requests.

        Kept byte-identical and first so provider-side caching can hit it —
        the persona, the intake playbook and the case-type index together are
        a few thousand tokens that would otherwise be re-billed every message.
        The date and other volatile facts go *after* this, never inside it.
        """
        return (
            f"{self._persona}\n\n"
            f"{self._playbook}\n\n"
            f"## 다룰 수 있는 사건유형\n\n{case_type_index()}"
        )

    def system_prompt(self) -> str:
        brief = _RUNTIME_BRIEF.format(
            max_chars=self.settings.kakao_max_chars,
            lawyer_name=self.settings.lawyer_name,
            today=time.strftime("%Y년 %m월 %d일"),
        )
        return f"{self.stable_prefix()}\n\n{brief}"

    async def answer(self, question: str, history: list[Message]) -> AnswerResult:
        started = time.monotonic()
        state = TurnState()
        tools = build_tools(
            state=state,
            rag=self.rag,
            law=self.law,
            rag_top_k=self.settings.rag_top_k,
            embed_query=self.embed_query,
        )
        tools.extend(build_intake_tools(state, self.settings.lawyer_name))
        tools.extend(build_action_tools(state))

        messages = build_messages(history, question, self.settings.bot_name)
        try:
            result: LlmResult = await self.llm.complete(self.system_prompt(), messages, tools)
        except LlmError as exc:
            log.warning("llm failed: %s", exc)
            return AnswerResult(
                text="",
                state=state,
                latency_ms=int((time.monotonic() - started) * 1000),
                error=str(exc),
            )

        text = (result.text or "").strip()
        if not text and state.escalation is not None:
            text = "지금 답변드리기 어려운 부분이 있어 담당 변호사님께 바로 전달드렸습니다. 확인 후 안내드리겠습니다."
        return AnswerResult(
            text=text,
            state=state,
            latency_ms=int((time.monotonic() - started) * 1000),
            tools_used=result.tools_used,
        )

    async def draft_document(
        self, kind: str, title: str, instructions: str, history: list[Message]
    ) -> str:
        """Generate the document body. Slow path — never on the reply budget."""
        transcript = "\n".join(
            f"{message.sender or message.role}: {message.text}" for message in history if message.text
        )
        system = (
            f"{self._persona}\n\n"
            "지금은 상담방이 아니라 문서 작성 단계다. 담당 변호사가 검토할 "
            f"{kind} 초안을 작성한다.\n"
            "- 실제 문서 서식 그대로, 제목·당사자·본문·날짜·서명란까지 포함한다.\n"
            "- 사실관계가 비어 있는 자리는 [ ] 로 표시해 변호사가 채우게 한다. 지어내지 않는다.\n"
            "- 근거 조문이 확실한 것만 인용한다.\n"
            "- 카카오톡 말투가 아니라 문서 문어체로 쓴다. 인사말·설명은 붙이지 않는다."
        )
        prompt = (
            f"[문서 종류] {kind}\n[제목] {title}\n\n"
            f"[작성 지시]\n{instructions}\n\n"
            f"[상담 대화 기록]\n{transcript[-6000:]}\n\n"
            "위 내용으로 문서 초안 본문만 출력하라."
        )
        result = await self.llm.complete(
            system,
            [{"role": "user", "content": prompt}],
            tools=(),
            model=self.settings.draft_model,
            max_tokens=4000,
        )
        return (result.text or "").strip()
