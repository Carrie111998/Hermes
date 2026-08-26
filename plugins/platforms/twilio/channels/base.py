"""Shared contract for a Twilio *messaging* channel (RCS today; SMS, MMS,
WhatsApp are expected to fit this same shape later — they're all "send
text-ish content to a chat_id via the Messages API").

Voice and Email are NOT expected to implement this interface — a phone
call isn't "sent text" and an email needs a subject/address rather than a
chat_id. When those get added, give them their own interface (and their
own transport module in ``core/``) rather than stretching this one to
cover shapes it wasn't designed for.

``adapter.py`` only ever calls the methods declared here — it never
reaches into a channel module's private helpers. That boundary is the
whole point: a bug or change in one channel's module can't reach into
another's.
"""

from typing import Any, Dict, List, Optional, Tuple


class MessagingChannel:
    """Base class for a Messages-API-based Twilio channel.

    Subclasses set the class attributes and implement every method below.
    Methods read credentials/env fresh on each call rather than caching at
    construction time (mirrors how the original standalone-send path
    worked), so there's no stale-credential risk across profile switches.
    """

    name: str = ""
    max_message_length: int = 0
    platform_hint: str = ""
    required_env: List[str] = []
    cron_deliver_env_var: str = ""

    def check_requirements(self) -> bool:
        """Passive probe: are this channel's env vars set right now?
        Must be side-effect free — called from status displays."""
        raise NotImplementedError

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        """Return (ready, error_message). error_message is set iff not ready."""
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def parse_target_ref(self, target_ref: str) -> Optional[Tuple[str, Optional[str]]]:
        """Return (chat_id, thread_id) if `target_ref` is valid native syntax
        for this channel, else None."""
        raise NotImplementedError

    def validate_target_ref(self, chat_id: str):
        """Return True to accept, False to reject, or a string diagnostic."""
        raise NotImplementedError

    def format_message(self, content: str) -> str:
        raise NotImplementedError

    def build_send_requests(
        self, chat_id: str, content: str, messaging_service_sid: str
    ) -> List[Dict[str, str]]:
        """Return the Messages.json form-field dicts for this content — one
        dict per API call. May raise ValueError for malformed content (e.g.
        a bad rich-content directive)."""
        raise NotImplementedError
