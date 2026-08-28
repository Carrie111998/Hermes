"""Hermes-owned coordination for provider-neutral realtime voice sessions."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import UUID

from agent.realtime_voice import (
    HeardAudioBoundary,
    RealtimeEvent,
    RealtimeEventType,
    RealtimeSession,
    RealtimeVoiceProvider,
)

ToolDispatcher = Callable[[str, dict[str, Any]], str | Awaitable[str]]
logger = logging.getLogger(__name__)


class RealtimeVoiceCoordinator:
    """Relay audio while dispatching every tool call through the Hermes host."""

    def __init__(
        self, provider: RealtimeVoiceProvider, *, dispatch_tool: ToolDispatcher
    ) -> None:
        self._provider = provider
        self._dispatch_tool = dispatch_tool
        self._session: RealtimeSession | None = None
        self._current_item_id: str | None = None
        self._current_audio_events: dict[UUID, RealtimeEvent] = {}
        self._heard_boundary: HeardAudioBoundary | None = None

    async def open(
        self,
        *,
        instructions: str,
        tools: list[dict[str, Any]],
        voice: str | None = None,
    ) -> None:
        if self._session is not None:
            raise RuntimeError("Realtime voice session is already open")
        self._session = await self._provider.open_session(
            instructions=instructions, tools=tools, voice=voice
        )
        self._reset_output_state()

    def _require_session(self) -> RealtimeSession:
        if self._session is None:
            raise RuntimeError("Realtime voice session is not open")
        return self._session

    async def send_audio(self, pcm: bytes) -> None:
        await self._require_session().send_audio(pcm)

    def report_audio_heard(self, event: RealtimeEvent, *, audio_end_ms: int) -> bool:
        """Record playback progress only for audio emitted by this open epoch."""
        if (
            self._session is None
            or event.type is not RealtimeEventType.AUDIO
            or not event.item_id
            or event.item_id != self._current_item_id
            or self._current_audio_events.get(event.emission_id) is not event
            or audio_end_ms < 0
        ):
            return False
        boundary = HeardAudioBoundary(event.item_id, audio_end_ms)
        if self._heard_boundary and audio_end_ms < self._heard_boundary.audio_end_ms:
            return False
        self._heard_boundary = boundary
        return True

    async def cancel_response(self) -> None:
        session = self._require_session()
        boundary, self._heard_boundary = self._heard_boundary, None
        if boundary is not None:
            await session.truncate_response(boundary)
        await session.cancel_response()

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        session = self._require_session()
        async for event in session.events():
            if event.type is RealtimeEventType.AUDIO and event.item_id:
                if event.item_id != self._current_item_id:
                    self._current_item_id = event.item_id
                    self._current_audio_events.clear()
                    self._heard_boundary = None
                self._current_audio_events[event.emission_id] = event
            if event.type is RealtimeEventType.TOOL_CALL:
                await self._dispatch(event, session)
            yield event

    async def _dispatch(
        self, event: RealtimeEvent, session: RealtimeSession
    ) -> None:
        if not event.call_id or not event.tool_name:
            raise ValueError("Realtime tool_call events require call_id and tool_name")
        try:
            result = self._dispatch_tool(event.tool_name, dict(event.arguments))
            if inspect.isawaitable(result):
                result = await result
            output = str(result)
        except Exception as exc:
            logger.warning(
                "Realtime voice tool dispatch failed",
                extra={"tool_name": event.tool_name, "call_id": event.call_id},
                exc_info=True,
            )
            output = f"Error: {exc}"
        await session.submit_tool_result(event.call_id, output)

    async def close(self) -> None:
        session, self._session = self._session, None
        self._reset_output_state()
        if session is not None:
            await session.close()

    def _reset_output_state(self) -> None:
        self._current_item_id = None
        self._current_audio_events.clear()
        self._heard_boundary = None
