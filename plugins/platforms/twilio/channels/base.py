"""Shared contracts for a Twilio channel.

``Channel`` is the minimal contract every channel implements regardless of
transport — it's what ``adapter.py`` uses to dispatch target parsing,
readiness checks, and connection status without knowing which channel it's
talking to.

``MessagingChannel`` extends it for channels transported over Twilio's
Messages API resource (RCS today; SMS, MMS, WhatsApp are expected to fit
this same shape later — they're all "send text-ish content to a chat_id
via the Messages API"). It provides ``send()``/``standalone_send()`` for
free via ``core/messages_api.py``, so a Messages-API channel only needs to
implement ``format_message()`` + ``build_send_requests()``.

Voice and Email do NOT extend ``MessagingChannel`` — a phone call isn't
"sent text" and email needs a subject/from-address, not
``MessagingServiceSid``. They implement ``Channel`` directly and own their
own transport (see ``channels/email.py``, which talks to SendGrid's Mail
Send API, not Twilio's Messages resource). When Voice is added, give it
its own transport module in ``core/`` (Twilio's Calls.json resource)
rather than stretching ``messages_api.py`` to cover it.

``adapter.py`` only ever calls the methods declared here — it never
reaches into a channel module's private helpers. That boundary is the
whole point: a bug or change in one channel's module can't reach into
another's.
"""

import os
from typing import Any, Dict, List, Optional, Tuple


class Channel:
    """Minimal contract every Twilio channel implements, regardless of
    transport (Messages API, SendGrid, or something else entirely)."""

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
        for this channel, else None. RCS and Email target formats (phone
        number vs email address) are mutually exclusive by construction, so
        adapter.py can safely try each channel in turn."""
        raise NotImplementedError

    def validate_target_ref(self, chat_id: str):
        """Return True to accept, False to reject, or a string diagnostic."""
        raise NotImplementedError

    async def send(self, chat_id: str, content: str, *, metadata: Optional[dict] = None, session=None) -> Dict[str, Any]:
        """Live-gateway send. Return {"success": True, "message_id": ...} or
        {"success": False, "error": "..."}. `session` (when given) is an
        already-open aiohttp session to reuse."""
        raise NotImplementedError

    async def standalone_send(self, pconfig, chat_id: str, message: str, **kwargs) -> Dict[str, Any]:
        """Out-of-process send (`hermes send` / cron), no live gateway
        adapter present. Return {"success": True, "platform": ..., "chat_id":
        ..., "message_id": ...} or {"error": "..."}."""
        raise NotImplementedError


class MessagingChannel(Channel):
    """Channels transported over Twilio's Messages API resource.

    Implements send()/standalone_send() once, generically, via
    core/messages_api.py — subclasses only need format_message() and
    build_send_requests().
    """

    def format_message(self, content: str) -> str:
        raise NotImplementedError

    def build_send_requests(
        self, chat_id: str, content: str, messaging_service_sid: str
    ) -> List[Dict[str, str]]:
        """Return the Messages.json form-field dicts for this content — one
        dict per API call. May raise ValueError for malformed content (e.g.
        a bad rich-content directive)."""
        raise NotImplementedError

    async def send(self, chat_id: str, content: str, *, metadata: Optional[dict] = None, session=None) -> Dict[str, Any]:
        from ..core.credentials import get_account_credentials
        from ..core.messages_api import send_message_requests

        account_sid, auth_token = get_account_credentials()
        messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
        try:
            form_fields_list = self.build_send_requests(chat_id, content, messaging_service_sid)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        return await send_message_requests(
            account_sid, auth_token, form_fields_list, chat_id,
            session=session, log_prefix=f"[twilio:{self.name}]",
        )

    async def standalone_send(self, pconfig, chat_id: str, message: str, **kwargs) -> Dict[str, Any]:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        from ..core.credentials import get_account_credentials
        from ..core.messages_api import send_message_requests

        account_sid, auth_token = get_account_credentials(pconfig)
        if not (account_sid and auth_token):
            return {
                "error": "Twilio credentials not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN required)"
            }

        ready, error_msg = self.connect_requirements_ok()
        if not ready:
            return {"error": f"Twilio {self.name} not configured: {error_msg}"}

        messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
        try:
            form_fields_list = self.build_send_requests(chat_id, message, messaging_service_sid)
        except ValueError as e:
            return {"error": str(e)}

        proxy = resolve_proxy_url()
        sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
        result = await send_message_requests(
            account_sid, auth_token, form_fields_list, chat_id,
            session_kwargs=sess_kw, request_kwargs=req_kw, log_prefix=f"[twilio:{self.name}]",
        )
        if result.get("success"):
            return {
                "success": True,
                "platform": "twilio",
                "chat_id": chat_id,
                "message_id": result.get("message_id", ""),
            }
        return {"error": result.get("error", "unknown error")}
