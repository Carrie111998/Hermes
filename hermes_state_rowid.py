"""Message row-id / role lookups for SessionDB.

Mixin contract: this is a plain mixin class consumed by
``hermes_state.SessionDB``. It defines no ``__init__`` and no state of its
own; methods access the host's attributes (``self._conn``, ``self._lock``)
established by ``SessionDB.__init__``. It must never import hermes_state
(cycle) — shared module-level constants live in hermes_state_common.
"""

import logging

from typing import Optional

# Moved methods logged under the "hermes_state" logger before the split;
# keep that logger identity so log filtering/capture behavior is unchanged.
logger = logging.getLogger("hermes_state")


class SessionRowIdMixin:
    def latest_message_row_id(
        self, session_id: str, *, role: str = "user", offset: int = 0, require_text: bool = True
    ) -> Optional[int]:
        """Row id of the most recent active message with *role*, or ``None``.

        Two callers, same need — "the message I mean, without an id": the agent
        defaulting to the turn that triggered it, and the desktop reacting to a
        live message that hasn't round-tripped through a resume yet.
        ``offset`` steps to earlier turns (1 = the one before the latest) so a
        reaction can land retroactively — "two messages ago" is how the caller
        thinks about it.

        ``require_text`` (default) skips rows with no plain-text content —
        tool-call-only assistant turns and attachment stubs don't render as
        bubbles, so "the latest message" as a HUMAN means it must never
        resolve to one (a reaction landing on an invisible row looks dropped,
        and its annotation quotes an empty string).
        """
        if not session_id or role not in {"user", "assistant"} or offset < 0:
            return None

        text_filter = (
            "AND content IS NOT NULL AND TRIM(content) != '' " if require_text else ""
        )

        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = ? "
                f"AND active = 1 {text_filter}ORDER BY id DESC LIMIT 1 OFFSET ?",
                (session_id, role, int(offset)),
            ).fetchone()

        return row[0] if row else None

    def latest_user_message_row_id(self, session_id: str) -> Optional[int]:
        """Row id of the most recent active user message, or ``None``.

        The agent's default reaction target: "the message that triggered me",
        so the model never has to thread row ids through a tool call (mirrors
        the photon adapter's ``_record_last_inbound``).
        """
        return self.latest_message_row_id(session_id, role="user")

    def get_message_role(self, session_id: str, row_id: int) -> Optional[str]:
        """Role of the active message at *row_id* in *session_id*, or ``None``.

        Lets a reaction event carry the target's role so a renderer can match
        a live message that doesn't know its durable row id yet.
        """
        if not session_id:
            return None

        with self._lock:
            row = self._conn.execute(
                "SELECT role FROM messages WHERE id = ? AND session_id = ? AND active = 1",
                (int(row_id), session_id),
            ).fetchone()

        return row[0] if row else None
