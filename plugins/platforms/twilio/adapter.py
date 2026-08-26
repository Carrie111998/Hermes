"""Twilio platform adapter — outbound-only.

Umbrella Twilio plugin, hosting multiple channels under a single
registered platform name (``"twilio"``). This module is intentionally
thin — it only does ``BasePlatformAdapter`` plumbing (connect/disconnect
lifecycle, ``SendResult`` shape) and dispatches every channel-specific
decision to the right channel object. See ``channels/base.py`` for the
interface a channel implements, and the README's "Architecture notes" for
why/how channel selection works.

Channels today: RCS (``channels/rcs.py``) and Email (``channels/email.py``).
Selection is by target format — a phone number routes to RCS, an email
address routes to Email — which works because the two formats are
mutually exclusive by construction. A future channel whose target format
collides with an existing one (unlikely for SMS/MMS/WhatsApp, which are
also phone numbers) would need an explicit disambiguation scheme instead;
see the "Architecture notes" section in the README before adding one.

Env vars (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN shared with the built-in
SMS platform and the optional telephony skill; SENDGRID_* is a completely
separate credential surface used only by the Email channel):
  - TWILIO_ACCOUNT_SID
  - TWILIO_AUTH_TOKEN
  - TWILIO_MESSAGING_SERVICE_SID   (MGxxxx... with an RCS Sender attached)
  - TWILIO_RCS_HOME_CHANNEL        (optional — destination for cron delivery)
  - SENDGRID_API_KEY
  - SENDGRID_FROM_EMAIL
  - SENDGRID_FROM_NAME             (optional)
  - SENDGRID_HOME_CHANNEL          (optional — NOT wired to cron; see README)

There is no inbound channel, so ``connect()``/``disconnect()`` are no-ops.
Delivery always goes through ``send()`` (live gateway) or
``_standalone_send()`` (out-of-process ``hermes send`` / cron delivery).
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

# Every channel this platform hosts. Adding a new one means writing
# channels/<name>.py against channels.base's Channel/MessagingChannel
# interface and appending an instance here — see the README's "Adding a
# new channel" section. Order only matters as a tie-breaker if two
# channels' target formats ever overlapped (they don't today).
_CHANNELS: List[Channel] = [RcsChannel(), EmailChannel()]

# The largest max_message_length across channels. Registered once for the
# whole platform because tools/send_message_tool.py pre-chunks by this
# single value BEFORE dispatching to any channel — using the smallest
# channel's limit here would silently split long emails into multiple
# separate sends. Each channel still enforces its own (smaller) limit
# internally where relevant (see RcsChannel.build_send_requests).
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
    return "not a valid E.164 phone number or email address"


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

            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), trust_env=True)
        try:
            result = await channel.send(chat_id, content, metadata=metadata, session=session)
        finally:
            if owns_session:
                await session.close()

        if result.get("success"):
            return SendResult(success=True, message_id=result.get("message_id", ""))
        return SendResult(success=False, error=result.get("error", "unknown error"))

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
    when no live gateway adapter is present in this process."""
    channel = _channel_for_target(chat_id)
    if channel is None:
        return {"error": f"'{chat_id}' is not a valid target for any configured Twilio channel"}
    return await channel.standalone_send(pconfig, chat_id, message)


def _is_connected(config) -> bool:
    return any(c.is_connected() for c in _CHANNELS)


def _build_adapter(config):
    return TwilioAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    # cron_deliver_env_var is a single static env var per platform in
    # Hermes core (cron/scheduler.py._resolve_home_env_var) — it can't
    # route to different channels' home-channel vars. RCS keeps the slot;
    # SENDGRID_HOME_CHANNEL exists as a plugin.yaml-documented env var for
    # future use but isn't wired to cron delivery yet. See README.
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
            "You are sending via Twilio — RCS (phone numbers, with SMS/MMS "
            "fallback) or Email (SendGrid), depending on the target's format."
        ),
    )
