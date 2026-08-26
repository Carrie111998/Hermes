"""Channel contracts for the Twilio plugin.

Channel: minimal shape every channel implements. MessagingChannel: for
channels on Twilio's Messages API resource (RCS now; SMS/MMS/WhatsApp
later) — implements send()/standalone_send() via core/messages_api.py,
so subclasses only need format_message() + build_send_requests().

Voice/Email don't extend MessagingChannel (no MessagingServiceSid, own
transport) — they implement Channel directly.
"""

import os
from typing import Any, Dict, List, Optional, Tuple


class Channel:
    name: str = ""
    max_message_length: int = 0
    platform_hint: str = ""
    required_env: List[str] = []
    cron_deliver_env_var: str = ""

    def check_requirements(self) -> bool:
        """Side-effect-free: are this channel's env vars set right now?"""
        raise NotImplementedError

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        """(ready, error_message); error_message set if not ready."""
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def parse_target_ref(self, target_ref: str) -> Optional[Tuple[str, Optional[str]]]:
        """(chat_id, thread_id) if target_ref matches this channel's native
        syntax, else None."""
        raise NotImplementedError

    def validate_target_ref(self, chat_id: str):
        """True to accept, False to reject, or a string diagnostic."""
        raise NotImplementedError

    async def send(self, chat_id: str, content: str, *, metadata: Optional[dict] = None, session=None) -> Dict[str, Any]:
        """{"success": True, "message_id": ...} or {"success": False, "error": ...}."""
        raise NotImplementedError

    async def standalone_send(self, pconfig, chat_id: str, message: str, **kwargs) -> Dict[str, Any]:
        """{"success": True, "platform", "chat_id", "message_id"} or {"error": ...}."""
        raise NotImplementedError


class MessagingChannel(Channel):
    def format_message(self, content: str) -> str:
        raise NotImplementedError

    def build_send_requests(
        self, chat_id: str, content: str, messaging_service_sid: str
    ) -> List[Dict[str, str]]:
        """Messages.json form-field dicts, one per API call. May raise
        ValueError for malformed content."""
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
