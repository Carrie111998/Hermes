"""Discord structured inbound message model (M1).

Pure typed projection of a Discord message payload (REST v10
resources/message.mdx) into a small read-only dataclass. No network access.
"""

from dataclasses import dataclass, field
from typing import Any


class MessageProjectionError(ValueError):
    """Raised when a message payload cannot be projected."""


@dataclass
class MessageContent:
    """Read projection of a Discord message payload."""

    content: str
    embeds: list[dict] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    replied_to: str | None = None
    thread_starter: bool = False
    flags: int = 0
    type: int = 0


def project_message(payload: dict) -> MessageContent:
    """Project a Discord message payload into a :class:`MessageContent`.

    Raises:
        MessageProjectionError: if ``payload`` is not a dict.
    """
    if not isinstance(payload, dict):
        raise MessageProjectionError(
            "message payload must be a dict, got "
            f"{type(payload).__name__}"
        )

    content = payload.get("content")
    content = content if isinstance(content, str) and content else ""

    embeds = payload.get("embeds", [])
    embeds = embeds if isinstance(embeds, list) else []

    attachments: list[str] = []
    raw_attachments = payload.get("attachments", [])
    if isinstance(raw_attachments, list):
        attachments = [
            att["url"]
            for att in raw_attachments
            if isinstance(att, dict) and isinstance(att.get("url"), str)
        ]

    replied_to: str | None = None
    referenced = payload.get("referenced_message")
    if isinstance(referenced, dict) and isinstance(referenced.get("id"), str):
        replied_to = referenced["id"]

    msg_type = payload.get("type", 0)
    msg_type = msg_type if isinstance(msg_type, int) else 0
    thread_starter = msg_type == 21

    flags = payload.get("flags", 0)
    flags = flags if isinstance(flags, int) else 0

    return MessageContent(
        content=content,
        embeds=embeds,
        attachments=attachments,
        replied_to=replied_to,
        thread_starter=thread_starter,
        flags=flags,
        type=msg_type,
    )
