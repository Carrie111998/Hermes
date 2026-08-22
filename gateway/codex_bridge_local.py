"""In-memory local origin used to exercise the Hermes-to-Codex bridge MVP."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource


@dataclass
class LocalCodexTestAdapter:
    """Small origin adapter that captures exactly what Gateway sends back."""

    messages: list[dict[str, Any]] = field(default_factory=list)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        self.messages.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": dict(metadata or {}),
            }
        )
        return SendResult(success=True, message_id=f"local-{len(self.messages)}")

    async def dispatch(self, gateway: Any, event: MessageEvent) -> str | None:
        """Submit through GatewayRunner and deliver its final response locally."""

        response = await gateway._handle_message(event)
        if response:
            await self.send(
                str(event.source.chat_id),
                response,
                reply_to=event.message_id,
                metadata={"codex_bridge_final": True},
            )
        return response


def make_local_codex_event(
    prompt: str,
    *,
    workspace: str,
    idempotency_key: str,
    message_id: str = "local-message-1",
    conversation_id: str = "local-codex-test",
) -> MessageEvent:
    """Build an authenticated-style local event for the real gateway boundary."""

    return MessageEvent(
        text=prompt,
        message_id=message_id,
        source=SessionSource(
            platform=Platform.LOCAL,
            chat_id=conversation_id,
            user_id="local-test-user",
            user_name="Local Codex Test",
            chat_type="dm",
        ),
        metadata={
            "codex_bridge_request": True,
            "workspace": workspace,
            "idempotency_key": idempotency_key,
        },
    )


def make_local_codex_reply_event(
    answer: str,
    *,
    prompt_id: str,
    idempotency_key: str,
    message_id: str = "local-reply-1",
    conversation_id: str = "local-codex-test",
    user_id: str = "local-test-user",
) -> MessageEvent:
    """Build a correlated local reply to a persisted ``needs_user`` prompt."""

    return MessageEvent(
        text=answer,
        message_id=message_id,
        source=SessionSource(
            platform=Platform.LOCAL,
            chat_id=conversation_id,
            user_id=user_id,
            user_name="Local Codex Test",
            chat_type="dm",
        ),
        metadata={
            "codex_bridge_prompt_id": prompt_id,
            "idempotency_key": idempotency_key,
        },
    )
