"""Gateway-owned correlation of inbound edits with queued and active turns."""

from __future__ import annotations

import logging
from typing import Any

from gateway.event_sidecars import replace_correlated_event_text


logger = logging.getLogger(__name__)


class GatewayEditSupersedeMixin:
    """Bounded owner for queue replacement and in-flight edit redirection."""

    def _replace_queued_message(
        self,
        session_key: str,
        adapter: Any,
        message_id: str,
        new_text: str,
    ) -> bool:
        """Replace the first matching event in debounce, head, or overflow."""
        replace_debounced = getattr(adapter, "replace_text_debounce_message", None)
        if callable(replace_debounced) and replace_debounced(
            session_key, message_id, new_text
        ):
            return True

        pending_slot = (
            getattr(adapter, "_pending_messages", None)
            if adapter is not None
            else None
        )
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if pending_event is not None and replace_correlated_event_text(
                pending_event, message_id, new_text
            ):
                return True

        state = self._peek_session_state(session_key)
        overflow = state.conversation.queued_events if state else []
        return any(
            replace_correlated_event_text(event, message_id, new_text)
            for event in overflow
        )

    def _handle_edit_supersede(
        self,
        event: Any,
        session_key: str,
        adapter: Any,
    ) -> bool:
        """Supersede a queued or active turn correlated to an inbound edit."""
        is_edit = bool(
            event.metadata.get("is_edit", False) if event.metadata else False
        )
        message_id = event.message_id
        if not is_edit or not message_id:
            return False

        if self._replace_queued_message(
            session_key,
            adapter,
            message_id,
            event.text or "",
        ):
            logger.info(
                "Edit supersede — replaced queued message_id=%s for session %s",
                message_id,
                session_key,
            )
            return True

        state = self._peek_session_state(session_key)
        if state and (
            state.turn.active_message_id == message_id
            or message_id in state.turn.active_message_ids
        ):
            running_agent = state.turn.agent
            pending_sentinel = self._agent_pending_sentinel
            if running_agent is pending_sentinel:
                logger.info(
                    "Edit supersede — active turn is still initializing for "
                    "message_id=%s in session %s; dropping the edit rather "
                    "than misrouting it as a new turn",
                    message_id,
                    session_key,
                )
                return True
            if running_agent is not None:
                framing = (
                    "[User edited their earlier message. Corrected message: "
                    f'"{event.text}"]'
                )
                handled = False
                if (
                    getattr(running_agent, "_supports_active_turn_redirect", False)
                    is True
                    and hasattr(running_agent, "redirect")
                ):
                    try:
                        handled = bool(running_agent.redirect(framing))
                    except Exception as exc:
                        logger.warning(
                            "Edit supersede — redirect failed for session %s: %s",
                            session_key,
                            exc,
                        )
                if not handled and hasattr(running_agent, "steer"):
                    try:
                        handled = bool(running_agent.steer(framing))
                    except Exception as exc:
                        logger.warning(
                            "Edit supersede — steer failed for session %s: %s",
                            session_key,
                            exc,
                        )
                if handled:
                    logger.info(
                        "Edit supersede — redirected/steered in-flight turn "
                        "message_id=%s for session %s",
                        message_id,
                        session_key,
                    )
                    return True

                logger.warning(
                    "Edit supersede — redirect/steer unavailable for session %s, "
                    "queueing edit as normal pending event",
                    session_key,
                )
                self._queue_or_replace_pending_event(session_key, event)
                return True

        logger.info(
            "Edit supersede — uncorrelated edit dropped "
            "(platform=%s, chat=%s, message_id=%s)",
            getattr(getattr(event.source, "platform", None), "value", "?"),
            getattr(event.source, "chat_id", "?"),
            message_id,
        )
        return True
