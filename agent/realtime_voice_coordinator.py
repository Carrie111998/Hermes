"""Hermes-owned coordination for provider-neutral realtime voice sessions."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from agent.realtime_voice import (
    RealtimeEvent,
    RealtimeEventType,
    RealtimeSession,
    RealtimeVoiceProvider,
)

ToolDispatcher = Callable[[str, dict[str, Any]], str | Awaitable[str]]


class RealtimeVoiceCoordinator:
    """Relay audio while dispatching every tool call through the Hermes host."""

    def __init__(
        self, provider: RealtimeVoiceProvider, *, dispatch_tool: ToolDispatcher
    ) -> None:
        self._provider = provider
        self._dispatch_tool = dispatch_tool
        self._session: RealtimeSession | None = None

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

    def _require_session(self) -> RealtimeSession:
        if self._session is None:
            raise RuntimeError("Realtime voice session is not open")
        return self._session

    async def send_audio(self, pcm: bytes) -> None:
        await self._require_session().send_audio(pcm)

    async def cancel_response(self) -> None:
        await self._require_session().cancel_response()

    async def events(self) -> AsyncIterator[RealtimeEvent]:
        session = self._require_session()
        async for event in session.events():
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
            output = f"Error: {exc}"
        await session.submit_tool_result(event.call_id, output)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
