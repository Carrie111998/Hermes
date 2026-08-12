"""Regression: an Agent Mail wake cannot create a second public voice reply.

This reproduces the incident ordering (internal wake first, then a Discord
voice attachment) at the gateway wake boundary.  The harness uses the real
``deliver_wake`` event construction, real internal session key construction,
and the real non-public control-plane predicate.  It deliberately uses
barriers rather than sleeps so the overlap is deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.agent_mail_watchers import _agent_mail_source
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import _is_nonpublic_control_plane_event
from gateway.session import SessionSource, build_session_key
from gateway.wake import deliver_wake


VOICE_FINAL = "VOICE FINAL"
VOICE_PLATFORM_ID = "discord-voice-final-1537099202968948737"


class _OverlapHarness:
    """Minimal push adapter recording the public final-delivery contract."""

    supports_async_delivery = True

    def __init__(self) -> None:
        self.mail_started = asyncio.Event()
        self.release_mail = asyncio.Event()
        self.public_texts: list[str] = []
        self.public_message_ids: list[str] = []
        self.internal_outcomes: list[tuple[str, str]] = []
        self.handled_events: list[MessageEvent] = []

    async def handle_message(self, event: MessageEvent) -> None:
        self.handled_events.append(event)
        if _is_nonpublic_control_plane_event(event):
            self.mail_started.set()
            await self.release_mail.wait()
            self.internal_outcomes.append((str(event.message_id), "handled"))
            return

        assert event.message_type is MessageType.VOICE
        self.public_texts.append(VOICE_FINAL)
        self.public_message_ids.append(VOICE_PLATFORM_ID)


@pytest.mark.asyncio
async def test_agent_mail_wake_before_discord_voice_has_one_public_final() -> None:
    """The original collision ordering produces one public voice final only."""
    adapter = _OverlapHarness()
    human_source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1477305880146870497",
        chat_type="group",
        user_id="terry-discord-user",
        profile="daery",
    )
    mail_source = _agent_mail_source(
        channel_id="1477305880146870497",
        profile="daery",
        identity="BrightTower",
    )
    voice_event = MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=human_source,
        message_id="1537099202968948737",
        media_urls=["/tmp/voice-1537099202968948737.ogg"],
        media_types=["audio/ogg"],
    )

    assert build_session_key(mail_source) != build_session_key(human_source)

    mail_task = asyncio.create_task(
        deliver_wake(
            adapter,
            text="[INTERNAL AGENT MAIL WAKE] mail 11226",
            source=mail_source,
            message_id="agent-mail:11226",
            suppress_public_delivery=True,
        )
    )
    await asyncio.wait_for(adapter.mail_started.wait(), timeout=1)

    # The human voice arrives while the internal wake remains active.
    await adapter.handle_message(voice_event)
    adapter.release_mail.set()
    await mail_task

    assert len(adapter.handled_events) == 2
    assert adapter.handled_events[0].metadata["suppress_public_delivery"] is True
    assert adapter.public_texts == [VOICE_FINAL]
    assert adapter.public_message_ids == [VOICE_PLATFORM_ID]
    assert adapter.internal_outcomes == [("agent-mail:11226", "handled")]
    assert voice_event.media_urls == ["/tmp/voice-1537099202968948737.ogg"]
