"""Per-turn sidecar-note methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the must-deliver sidecar-note staging cluster plus the Discord
voice-channel state-change note.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
"""


from __future__ import annotations

import logging

from typing import List, Optional

from gateway.config import Platform
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayTurnSidecarMixin:

    def _set_pending_turn_sidecar_notes(self, session_key: str, notes: List[str]) -> None:
        """Stage per-turn must-deliver notes for the next agent run (one-shot)."""
        if not session_key or not notes:
            return
        self._session_state(session_key).conversation.sidecar_notes = list(notes)

    def _consume_pending_turn_sidecar_notes(self, session_key: str) -> List[str]:
        if not session_key:
            return []
        state = self._peek_session_state(session_key)
        if state is None:
            return []
        staged = state.conversation.sidecar_notes
        state.conversation.sidecar_notes = []
        return list(staged) if isinstance(staged, list) else []

    def _voice_channel_sidecar_note(self, event, source: SessionSource, session_key: str) -> Optional[str]:
        """Return a ``[Voice channel now: ...]`` note when VC state changed.

        Compares the live Discord voice-channel context against the last
        value delivered for this session and returns a note only on change
        (including leaving the channel).  Unchanged state returns ``None`` so
        the per-turn member/speaking serialization cannot churn the prompt.
        """
        if source.platform != Platform.DISCORD:
            return None
        adapter = self.adapters.get(Platform.DISCORD)
        guild_id = self._get_guild_id(event)
        if not (guild_id and adapter and hasattr(adapter, "get_voice_channel_context")):
            return None
        try:
            vc_now = adapter.get_voice_channel_context(guild_id) or ""
        except Exception:
            logger.debug("voice-channel context read failed", exc_info=True)
            return None
        vc_prev = None
        if session_key:
            _vc_state = self._session_state(session_key)
            vc_prev = _vc_state.conversation.vc_last
            _vc_state.conversation.vc_last = vc_now
        if vc_now == (vc_prev if vc_prev is not None else ""):
            return None
        if not vc_now:
            return "[Voice channel now: not connected to a voice channel]"
        return f"[Voice channel now: {vc_now}]"

