"""Twilio platform adapter — outbound-only.

Umbrella Twilio plugin, intended to grow into more channels over time
(SMS, MMS, WhatsApp, Voice, Email). Currently implements only the **RCS**
channel: sends through a Twilio Messaging Service that has an RCS Sender
(approved by Google) attached. Twilio automatically selects RCS for
capable recipients and falls back to SMS/MMS otherwise — the send call
looks identical either way, just with ``MessagingServiceSid`` instead of
a raw ``From`` phone number.

Env vars (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN shared with the built-in
SMS platform and the optional telephony skill):
  - TWILIO_ACCOUNT_SID
  - TWILIO_AUTH_TOKEN
  - TWILIO_MESSAGING_SERVICE_SID   (MGxxxx... with an RCS Sender attached)
  - TWILIO_RCS_HOME_CHANNEL        (optional — destination for cron delivery)

There is no inbound channel, so ``connect()``/``disconnect()`` are no-ops.
Delivery always goes through ``send()`` (live gateway) or
``_standalone_send()`` (out-of-process ``hermes send`` / cron delivery).

Rich content (RCS cards, carousels) is sent by referencing a pre-created
Twilio Content API template through a ``CONTENT:`` directive in the
message text, mirroring the existing ``MEDIA:<path>`` convention used
elsewhere in Hermes cross-platform messaging:

    hermes send --to twilio:+15551234567 "CONTENT:HXxxxxxxxxxxxx"
    hermes send --to twilio:+15551234567 'CONTENT:HXxxxxxxxxxxxx:{"1":"Alice"}'

Create templates with ``scripts/manage_content.py`` (create-card /
create-carousel / create-quick-reply), which prints the resulting
Content SID.
"""

import base64
import json
import logging
import os
import re
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.helpers import redact_phone, strip_markdown

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
# Twilio's documented RCS text body limit — verify against current Twilio
# docs if recipients start seeing truncated messages.
MAX_RCS_LENGTH = 3072

# Mirrors tools/send_message_tool._E164_TARGET_RE — this platform isn't in
# core's hardcoded _PHONE_PLATFORMS set, so it must declare its own parser
# (see parse_target_ref_fn below) to accept bare E.164 numbers as targets.
_E164_TARGET_RE = re.compile(r"^\s*\+(\d{7,15})\s*$")


def parse_target_ref(target_ref: str):
    """Accept a bare E.164 phone number (e.g. '+15551234567') as a target."""
    match = _E164_TARGET_RE.fullmatch(target_ref)
    if match:
        return target_ref.strip(), None
    return None


def validate_target_ref(chat_id: str):
    return True if _E164_TARGET_RE.fullmatch(chat_id) else "not a valid E.164 phone number"


# 'CONTENT:<ContentSid>' or 'CONTENT:<ContentSid>:<json ContentVariables>' —
# references a Content API template created via scripts/manage_content.py.
_CONTENT_DIRECTIVE_RE = re.compile(r"^CONTENT:(?P<sid>HX[0-9a-fA-F]{32})(?::(?P<vars>.+))?$", re.DOTALL)


def _parse_content_directive(message: str):
    """Return (content_sid, content_variables_json_or_None), or None if `message`
    isn't a CONTENT: directive. Raises ValueError if the variables aren't valid JSON."""
    match = _CONTENT_DIRECTIVE_RE.match(message.strip())
    if not match:
        return None
    raw_vars = match.group("vars")
    if raw_vars is not None:
        try:
            json.loads(raw_vars)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid ContentVariables JSON in CONTENT: directive: {e}")
    return match.group("sid"), raw_vars


def _build_content_form(aiohttp_module, messaging_service_sid: str, chat_id: str, content_sid: str, content_variables: Optional[str]):
    form_data = aiohttp_module.FormData()
    form_data.add_field("MessagingServiceSid", messaging_service_sid)
    form_data.add_field("To", chat_id)
    form_data.add_field("ContentSid", content_sid)
    if content_variables:
        form_data.add_field("ContentVariables", content_variables)
    return form_data


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Mirrors plugins/platforms/sms/adapter.py::_get_scoped_secret.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _basic_auth_header(account_sid: str, auth_token: str) -> str:
    creds = f"{account_sid}:{auth_token}".encode("ascii")
    return f"Basic {base64.b64encode(creds).decode('ascii')}"


def check_rcs_requirements() -> bool:
    """Passive probe: dependencies + minimal config present right now.

    Named for the RCS channel specifically — when a second channel (SMS,
    WhatsApp, Voice, Email) is added to this plugin, each will likely need
    its own check_fn/required_env, since they won't all share
    TWILIO_MESSAGING_SERVICE_SID as a hard requirement.
    """
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(
        _get_scoped_secret("TWILIO_ACCOUNT_SID")
        and _get_scoped_secret("TWILIO_AUTH_TOKEN")
        and os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    )


class TwilioAdapter(BasePlatformAdapter):
    """Outbound-only Twilio adapter. RCS channel only for now (with
    automatic SMS/MMS fallback) — more channels planned."""

    MAX_MESSAGE_LENGTH = MAX_RCS_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("twilio"))
        self._account_sid: str = _get_scoped_secret("TWILIO_ACCOUNT_SID", "")
        self._auth_token: str = _get_scoped_secret("TWILIO_AUTH_TOKEN", "")
        self._messaging_service_sid: str = os.getenv(
            "TWILIO_MESSAGING_SERVICE_SID", ""
        ).strip()
        self._http_session: Optional["Any"] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._messaging_service_sid:
            msg = (
                "[twilio] TWILIO_MESSAGING_SERVICE_SID not set — cannot send. "
                "Attach an RCS Sender to a Messaging Service in the Twilio "
                "Console and set its SID here."
            )
            logger.error(msg)
            self._set_fatal_error(
                "twilio_missing_messaging_service_sid", msg, retryable=False
            )
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
        import aiohttp

        try:
            directive = _parse_content_directive(content)
        except ValueError as e:
            return SendResult(success=False, error=str(e))

        url = f"{TWILIO_API_BASE}/{self._account_sid}/Messages.json"
        headers = {"Authorization": _basic_auth_header(self._account_sid, self._auth_token)}

        if directive:
            form_data_list = [_build_content_form(aiohttp, self._messaging_service_sid, chat_id, *directive)]
        else:
            formatted = self.format_message(content)
            chunks = self.truncate_message(formatted)
            form_data_list = []
            for chunk in chunks:
                form_data = aiohttp.FormData()
                form_data.add_field("MessagingServiceSid", self._messaging_service_sid)
                form_data.add_field("To", chat_id)
                form_data.add_field("Body", chunk)
                form_data_list.append(form_data)

        last_result = SendResult(success=True)
        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True,
        )
        try:
            for form_data in form_data_list:
                try:
                    async with session.post(url, data=form_data, headers=headers) as resp:
                        body = await resp.json()
                        if resp.status >= 400:
                            error_msg = body.get("message", str(body))
                            logger.error(
                                "[twilio] send failed to %s: %s %s",
                                redact_phone(chat_id), resp.status, error_msg,
                            )
                            return SendResult(
                                success=False,
                                error=f"Twilio {resp.status}: {error_msg}",
                            )
                        msg_sid = body.get("sid", "")
                        last_result = SendResult(success=True, message_id=msg_sid)
                except Exception as e:
                    logger.error("[twilio] send error to %s: %s", redact_phone(chat_id), e)
                    return SendResult(success=False, error=str(e))
        finally:
            if not self._http_session and session:
                await session.close()

        return last_result

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        """Strip markdown — the RCS/SMS Body field renders it as literal characters."""
        return strip_markdown(content)


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process RCS delivery for `hermes send` and cron `deliver=twilio`
    when no live gateway adapter is present in this process."""
    import aiohttp

    account_sid = _get_scoped_secret("TWILIO_ACCOUNT_SID", "")
    auth_token = getattr(pconfig, "api_key", None) or _get_scoped_secret("TWILIO_AUTH_TOKEN", "")
    messaging_service_sid = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()
    if not (account_sid and auth_token and messaging_service_sid):
        return {
            "error": (
                "Twilio RCS not configured (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                "TWILIO_MESSAGING_SERVICE_SID required)"
            )
        }

    try:
        directive = _parse_content_directive(message)
    except ValueError as e:
        return {"error": str(e)}

    try:
        from gateway.platforms.base import resolve_proxy_url, proxy_kwargs_for_aiohttp

        proxy = resolve_proxy_url()
        sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
        url = f"{TWILIO_API_BASE}/{account_sid}/Messages.json"
        headers = {"Authorization": _basic_auth_header(account_sid, auth_token)}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **sess_kw) as session:
            if directive:
                form_data = _build_content_form(aiohttp, messaging_service_sid, chat_id, *directive)
            else:
                form_data = aiohttp.FormData()
                form_data.add_field("MessagingServiceSid", messaging_service_sid)
                form_data.add_field("To", chat_id)
                form_data.add_field("Body", strip_markdown(message))
            async with session.post(url, data=form_data, headers=headers, **req_kw) as resp:
                payload = await resp.json()
                if resp.status >= 400:
                    error_msg = payload.get("message", str(payload))
                    return {"error": f"Twilio API error ({resp.status}): {error_msg}"}
                return {
                    "success": True,
                    "platform": "twilio",
                    "chat_id": chat_id,
                    "message_id": payload.get("sid", ""),
                }
    except Exception as e:
        return {"error": f"Twilio RCS send failed: {e}"}


def _is_connected(config) -> bool:
    return bool(os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip()) and bool(
        (_get_scoped_secret("TWILIO_ACCOUNT_SID") or "").strip()
    )


def _build_adapter(config):
    return TwilioAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="twilio",
        label="Twilio",
        adapter_factory=_build_adapter,
        check_fn=check_rcs_requirements,
        is_connected=_is_connected,
        required_env=["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_MESSAGING_SERVICE_SID"],
        install_hint="pip install aiohttp",
        cron_deliver_env_var="TWILIO_RCS_HOME_CHANNEL",
        parse_target_ref_fn=parse_target_ref,
        validate_target_ref_fn=validate_target_ref,
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_RCS_LENGTH,
        pii_safe=True,
        emoji="💬",
        allow_update_command=False,
        platform_hint=(
            "You are sending via Twilio RCS (with automatic SMS/MMS fallback). "
            "Plain text only — no markdown."
        ),
    )
