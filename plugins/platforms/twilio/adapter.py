"""Twilio platform adapter — outbound-only. Thin BasePlatformAdapter glue;
dispatches to whichever channel matches the target format (see
channels/base.py, README "Architecture notes").

Channels: RCS (phone number), Email (email address). More (SMS, MMS,
WhatsApp, Voice) are expected to land in _CHANNELS over time — see
README "Adding a new channel" for the disambiguation concern once a
channel shares an existing one's target format.

Env vars: TWILIO_ACCOUNT_SID/AUTH_TOKEN (shared with the built-in sms
platform), TWILIO_MESSAGING_SERVICE_SID (RCS), TWILIO_EMAIL_FROM
(Email), TWILIO_RCS_HOME_CHANNEL / TWILIO_EMAIL_HOME_CHANNEL (optional
per-channel cron targets — see "cron_deliver_env_var" below).

No inbound channel — connect()/disconnect() are no-ops. Delivery is
send() (live gateway) or _standalone_send() (hermes send / cron).
"""

import logging
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .channels.base import Channel
from .channels.email import EmailChannel
from .channels.rcs import RcsChannel
from .core.messages_api import aiohttp_available

logger = logging.getLogger(__name__)

# Channels this platform hosts — see README "Adding a new channel".
_CHANNELS: List[Channel] = [RcsChannel(), EmailChannel()]

# Largest max_message_length across channels — see README for why this
# must be the max, not any individual channel's limit, once there's more
# than one channel with different limits.
_MAX_MESSAGE_LENGTH = max(c.max_message_length for c in _CHANNELS)


def _channel_for_target(chat_id: str) -> Optional[Channel]:
    for channel in _CHANNELS:
        if channel.validate_target_ref(chat_id) is True:
            return channel
    return None


def parse_target_ref(target_ref: str):
    for channel in _CHANNELS:
        parsed = channel.parse_target_ref(target_ref)
        if parsed is not None:
            return parsed
    return None


def validate_target_ref(chat_id: str):
    if _channel_for_target(chat_id) is not None:
        return True
    return "not a valid target for any configured Twilio channel (E.164 phone number or email address)"


def check_requirements() -> bool:
    """Passive probe: dependencies + at least one channel minimally configured."""
    return aiohttp_available() and any(c.check_requirements() for c in _CHANNELS)


def _union_required_env() -> List[str]:
    seen: List[str] = []
    for channel in _CHANNELS:
        for var in channel.required_env:
            if var not in seen:
                seen.append(var)
    return seen


class TwilioAdapter(BasePlatformAdapter):
    """Outbound-only Twilio adapter. Delegates every channel-specific
    decision to whichever channel matches the send target's format."""

    MAX_MESSAGE_LENGTH = _MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("twilio"))
        self._http_session: Optional[Any] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        errors = []
        for channel in _CHANNELS:
            ready, error_msg = channel.connect_requirements_ok()
            if ready:
                self._mark_connected()
                logger.info(
                    "[twilio] Ready (outbound-only, no inbound channel; %s configured)",
                    channel.name,
                )
                return True
            errors.append(f"{channel.name}: {error_msg}")

        msg = "[twilio] No channel is configured — " + "; ".join(errors)
        logger.error(msg)
        self._set_fatal_error("twilio_no_channel_configured", msg, retryable=False)
        return False

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
        channel = _channel_for_target(chat_id)
        if channel is None:
            return SendResult(
                success=False,
                error=f"'{chat_id}' is not a valid target for any configured Twilio channel",
            )

        owns_session = self._http_session is None
        session = self._http_session
        if owns_session:
            import aiohttp

            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), trust_env=True
            )
        try:
            result = await channel.send(
                chat_id, content, metadata=metadata, session=session
            )
        finally:
            if owns_session:
                await session.close()

        if result.get("success"):
            return SendResult(success=True, message_id=result.get("message_id", ""))
        return SendResult(success=False, error=result.get("error", "unknown error"))

    async def _dispatch_attachment_call(
        self, chat_id: str, method_name: str, *args, fallback, **kwargs
    ):
        """Shared plumbing for send_image()/send_document()/send_multiple_images():
        dispatch to the matched channel's own method if it has one (Email
        does; RCS doesn't — it has no attachment concept beyond its
        CONTENT: directive), else fall back to BasePlatformAdapter's default.
        """
        channel = _channel_for_target(chat_id)
        method = getattr(channel, method_name, None) if channel else None
        if method is None:
            return await fallback()
        return await method(chat_id, *args, **kwargs)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        result = await self._dispatch_attachment_call(
            chat_id,
            "send_image",
            image_url,
            caption=caption,
            metadata=metadata,
            fallback=lambda: super(TwilioAdapter, self).send_image(
                chat_id,
                image_url,
                caption=caption,
                reply_to=reply_to,
                metadata=metadata,
            ),
        )
        if isinstance(result, SendResult):
            return result
        if result.get("success"):
            return SendResult(success=True, message_id=result.get("message_id", ""))
        return SendResult(success=False, error=result.get("error", "unknown error"))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        result = await self._dispatch_attachment_call(
            chat_id,
            "send_document",
            file_path,
            caption=caption,
            file_name=file_name,
            metadata=metadata,
            fallback=lambda: super(TwilioAdapter, self).send_document(
                chat_id,
                file_path,
                caption=caption,
                file_name=file_name,
                reply_to=reply_to,
                metadata=metadata,
                **kwargs,
            ),
        )
        if isinstance(result, SendResult):
            return result
        if result.get("success"):
            return SendResult(success=True, message_id=result.get("message_id", ""))
        return SendResult(success=False, error=result.get("error", "unknown error"))

    async def send_multiple_images(
        self,
        chat_id: str,
        images,
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        result = await self._dispatch_attachment_call(
            chat_id,
            "send_multiple_images",
            images,
            metadata=metadata,
            fallback=lambda: super(TwilioAdapter, self).send_multiple_images(
                chat_id, images, metadata, human_delay
            ),
        )
        if isinstance(result, dict) and not result.get("success", True):
            logger.error("[twilio] multi-image send failed: %s", result.get("error"))

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        # No single channel to format for outside of a real send target;
        # callers that need channel-specific formatting go through send().
        return content


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
    when no live gateway adapter is present in this process. Forwards
    media_files/force_document/thread_id through **kwargs per the Channel
    contract — RCS's standalone_send() (inherited from MessagingChannel)
    ignores them via its own **kwargs, Email's uses media_files."""
    channel = _channel_for_target(chat_id)
    if channel is None:
        return {
            "error": f"'{chat_id}' is not a valid target for any configured Twilio channel"
        }
    return await channel.standalone_send(
        pconfig,
        chat_id,
        message,
        thread_id=thread_id,
        media_files=media_files,
        force_document=force_document,
    )


def _is_connected(config) -> bool:
    return any(c.is_connected() for c in _CHANNELS)


def _build_adapter(config):
    return TwilioAdapter(config)


def register(ctx) -> None:
    """Plugin entry point. cron_deliver_env_var is one static var per
    platform in Hermes core — a future second channel needing its own
    cron target will need to share or contest this slot (see README)."""
    ctx.register_platform(
        name="twilio",
        label="Twilio",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        is_connected=_is_connected,
        required_env=_union_required_env(),
        install_hint="pip install aiohttp",
        cron_deliver_env_var=RcsChannel.cron_deliver_env_var,
        parse_target_ref_fn=parse_target_ref,
        validate_target_ref_fn=validate_target_ref,
        standalone_sender_fn=_standalone_send,
        max_message_length=_MAX_MESSAGE_LENGTH,
        pii_safe=True,
        emoji="💬",
        allow_update_command=False,
        platform_hint=(
            "You are sending via Twilio. For a phone-number target: RCS with "
            "automatic SMS/MMS fallback, plain text only, no markdown. For an "
            "email-address target: the first line of your message becomes the "
            "subject, the rest becomes the body, plain text unless HTML is "
            "explicitly requested."
        ),
    )
