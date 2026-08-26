"""Twilio platform adapter — outbound-only.

Umbrella Twilio plugin, intended to grow into more channels over time
(SMS, MMS, WhatsApp, Voice, Email). This module is intentionally thin —
it only does ``BasePlatformAdapter`` plumbing (connect/disconnect
lifecycle, ``SendResult`` shape) and delegates every channel-specific
decision to the active channel object (``channels/rcs.py`` today). See
``channels/base.py`` for the interface a new channel implements, and the
README's "Architecture notes" for how channel selection is expected to
evolve when a second channel is added.

Env vars (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN shared with the built-in
SMS platform and the optional telephony skill):
  - TWILIO_ACCOUNT_SID
  - TWILIO_AUTH_TOKEN
  - TWILIO_MESSAGING_SERVICE_SID   (MGxxxx... with an RCS Sender attached)
  - TWILIO_RCS_HOME_CHANNEL        (optional — destination for cron delivery)

There is no inbound channel, so ``connect()``/``disconnect()`` are no-ops.
Delivery always goes through ``send()`` (live gateway) or
``_standalone_send()`` (out-of-process ``hermes send`` / cron delivery).
"""

import logging
import os
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .channels.rcs import RcsChannel
from .core.credentials import get_account_credentials
from .core.messages_api import aiohttp_available, send_message_requests

logger = logging.getLogger(__name__)

# The single active channel for this platform today. Adding a second
# channel means writing channels/<name>.py against channels.base's
# MessagingChannel interface, then deciding a selection strategy here
# (e.g. a directive prefix in the message, or per-channel target syntax) —
# nothing in Hermes's register_platform() distinguishes channels within
# one platform name, so that decision is this plugin's to make deliberately
# rather than bolting a second channel on ad hoc.
_CHANNEL = RcsChannel()


def parse_target_ref(target_ref: str):
    return _CHANNEL.parse_target_ref(target_ref)


def validate_target_ref(chat_id: str):
    return _CHANNEL.validate_target_ref(chat_id)


def check_requirements() -> bool:
    """Passive probe: dependencies + minimal config present right now."""
    return aiohttp_available() and _CHANNEL.check_requirements()


class TwilioAdapter(BasePlatformAdapter):
    """Outbound-only Twilio adapter. Delegates all channel-specific
    behavior to the active MessagingChannel (RcsChannel today)."""

    MAX_MESSAGE_LENGTH = _CHANNEL.max_message_length

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("twilio"))
        self._http_session: Optional[Any] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        ready, error_msg = _CHANNEL.connect_requirements_ok()
        if not ready:
            msg = f"[twilio] {error_msg}"
            logger.error(msg)
            self._set_fatal_error("twilio_channel_not_configured", msg, retryable=False)
            return False
        self._mark_connected()
        logger.info("[twilio] Ready (outbound-only, no inbound channel)")
        return True

    async def disconnect(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()
        logger.info("[twilio] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        account_sid, auth_token = get_account_credentials()
        messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()

        try:
            form_fields_list = _CHANNEL.build_send_requests(chat_id, content, messaging_service_sid)
        except ValueError as e:
            return SendResult(success=False, error=str(e))

        import aiohttp

        owns_session = self._http_session is None
        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True,
        )
        try:
            result = await send_message_requests(
                account_sid, auth_token, form_fields_list, chat_id,
                session=session, log_prefix="[twilio]",
            )
        finally:
            if owns_session:
                await session.close()

        if result.get("success"):
            return SendResult(success=True, message_id=result.get("message_id", ""))
        return SendResult(success=False, error=result.get("error", "unknown error"))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        return _CHANNEL.format_message(content)


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process delivery for `hermes send` and cron `deliver=twilio`
    when no live gateway adapter is present in this process."""
    account_sid, auth_token = get_account_credentials(pconfig)
    if not (account_sid and auth_token):
        return {
            "error": "Twilio credentials not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN required)"
        }

    ready, error_msg = _CHANNEL.connect_requirements_ok()
    if not ready:
        return {"error": f"Twilio {_CHANNEL.name} not configured: {error_msg}"}

    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    try:
        form_fields_list = _CHANNEL.build_send_requests(chat_id, message, messaging_service_sid)
    except ValueError as e:
        return {"error": str(e)}

    from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

    proxy = resolve_proxy_url()
    sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)

    result = await send_message_requests(
        account_sid, auth_token, form_fields_list, chat_id,
        session_kwargs=sess_kw, request_kwargs=req_kw, log_prefix="[twilio]",
    )
    if result.get("success"):
        return {
            "success": True,
            "platform": "twilio",
            "chat_id": chat_id,
            "message_id": result.get("message_id", ""),
        }
    return {"error": result.get("error", "unknown error")}


def _is_connected(config) -> bool:
    return _CHANNEL.is_connected()


def _build_adapter(config):
    return TwilioAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="twilio",
        label="Twilio",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        is_connected=_is_connected,
        required_env=_CHANNEL.required_env,
        install_hint="pip install aiohttp",
        cron_deliver_env_var=_CHANNEL.cron_deliver_env_var,
        parse_target_ref_fn=parse_target_ref,
        validate_target_ref_fn=validate_target_ref,
        standalone_sender_fn=_standalone_send,
        max_message_length=_CHANNEL.max_message_length,
        pii_safe=True,
        emoji="💬",
        allow_update_command=False,
        platform_hint=_CHANNEL.platform_hint,
    )
