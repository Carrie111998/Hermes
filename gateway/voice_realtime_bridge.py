"""Discord gateway ↔ realtime-voice supervisor bridge.

The surface-agnostic consult/steer brain lives in
:class:`agent.voice_supervisor.VoiceSupervisorController`; this module
supplies the Discord gateway's :class:`~agent.voice_supervisor.TurnRunner`
implementation. It re-enters the gateway's normal voice-input pipeline for
submissions (auth, session binding, transcript echo, agent turn) and reads
the base adapter's session guards for busy/queue state, so a consult behaves
exactly like a spoken utterance would.

Controller methods run on realtime-session threads; everything that touches
the gateway is bridged onto the event loop captured at construction.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class DiscordVoiceTurnRunner:
    """TurnRunner backed by the gateway's Discord voice-message pipeline."""

    def __init__(self, runner: Any, adapter: Any, guild_id: int, loop: asyncio.AbstractEventLoop):
        self._runner = runner
        self._adapter = adapter
        self._guild_id = guild_id
        self._loop = loop

    # -- helpers -------------------------------------------------------------

    def _bound_user_id(self) -> int:
        """The /voice join initiator — consults run under their identity."""
        source_data = getattr(self._adapter, "_voice_sources", {}).get(self._guild_id) or {}
        try:
            return int(source_data.get("user_id") or 0)
        except (TypeError, ValueError):
            return 0

    def _session_key(self) -> Optional[str]:
        user_id = self._bound_user_id()
        if not user_id:
            return None
        source = self._runner._voice_channel_source(self._adapter, self._guild_id, user_id)
        if source is None:
            return None
        try:
            return self._runner._session_key_for_source(source)
        except Exception:
            logger.debug("voice turn runner: session key resolution failed", exc_info=True)
            return None

    # -- TurnRunner protocol ---------------------------------------------------

    def submit(self, task: str) -> None:
        user_id = self._bound_user_id()
        if not user_id:
            logger.warning(
                "voice consult dropped: no bound user for guild %d", self._guild_id
            )
            return
        asyncio.run_coroutine_threadsafe(
            self._runner._handle_voice_channel_input(self._guild_id, user_id, task),
            self._loop,
        )

    def interrupt(self) -> None:
        session_key = self._session_key()
        chat_id = getattr(self._adapter, "_voice_text_channels", {}).get(self._guild_id)
        if not session_key or not chat_id:
            return
        asyncio.run_coroutine_threadsafe(
            self._adapter.interrupt_session_activity(session_key, str(chat_id)),
            self._loop,
        )

    def is_busy(self) -> bool:
        session_key = self._session_key()
        active = getattr(self._adapter, "_active_sessions", {})
        return bool(session_key) and session_key in active

    def is_queue_empty(self) -> bool:
        session_key = self._session_key()
        pending = getattr(self._adapter, "_pending_messages", {})
        return not session_key or session_key not in pending


__all__ = ["DiscordVoiceTurnRunner"]
