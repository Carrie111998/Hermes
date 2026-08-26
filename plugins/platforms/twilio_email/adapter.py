"""Twilio Email platform adapter — outbound-only.

Sends messages through Twilio's Email API — SendGrid's Mail Send API under
the hood. Twilio's Email product uses a completely separate credential
surface from core Twilio (SMS/Voice): a bearer ``SG.``-prefixed SendGrid
API key, not the ``TWILIO_ACCOUNT_SID``/``TWILIO_AUTH_TOKEN`` pair the
built-in SMS platform uses.

Env vars:
  - SENDGRID_API_KEY        (starts with SG.)
  - SENDGRID_FROM_EMAIL     (default sender — must be a Verified Sender or
                              part of an authenticated domain in SendGrid)
  - SENDGRID_FROM_NAME      (optional sender display name)
  - SENDGRID_API_BASE       (optional; defaults to the public SendGrid API —
                              override to a staging host for a
                              progenitor/go_user staging test account)
  - SENDGRID_HOME_CHANNEL   (optional — destination for cron delivery)

There is no inbound channel, so connect()/disconnect() are readiness checks
only. Delivery always goes through send() (live gateway) or
_standalone_send() (out-of-process `hermes send` / cron delivery); in
practice `_standalone_send()` is the one that actually runs, since
`hermes send` and cron jobs run in their own process.

Every other platform this adapter sits alongside passes a single `content`
string with no subject concept. By convention here: the first line of
`content` is the subject and the remainder (after the first newline) is the
body; single-line content gets a generic default subject rather than
guessing one. Callers with more control (e.g. our own standalone CLI use)
can override via `metadata={"subject": ..., "html": True}`.

Unlike the SMS adapter's `send()`, this adapter never splits a message into
multiple chunks/sends: an email is one document, not a multi-part SMS train,
so `truncate_message()` is deliberately not called here.
"""

import logging
import os
import re
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret

logger = logging.getLogger(__name__)

SENDGRID_API_BASE_DEFAULT = "https://api.sendgrid.com/v3"
# Generous — SendGrid's real cap is far larger than any agent-generated
# message; this just guards against something pathological.
MAX_EMAIL_LENGTH = 200_000
DEFAULT_SUBJECT = "Message from Hermes Agent"

# Mirrors tools/send_message_tool._E164_TARGET_RE's role for phone
# platforms — this platform isn't in core's hardcoded phone-platform set
# (it isn't a phone number at all), so it declares its own parser to
# accept bare email addresses as targets.
_EMAIL_TARGET_RE = re.compile(r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$")


def parse_target_ref(target_ref: str):
    """Accept a bare email address (e.g. 'customer@example.com') as a target."""
    if _EMAIL_TARGET_RE.fullmatch(target_ref):
        return target_ref.strip(), None
    return None


def validate_target_ref(chat_id: str):
    return True if _EMAIL_TARGET_RE.fullmatch(chat_id) else "not a valid email address"


def _get_scoped_secret(name, default=None):
    """Scope-aware credential read with the default-profile startup fallback.

    Mirrors plugins/platforms/sms/adapter.py::_get_scoped_secret.
    """
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _sendgrid_api_base() -> str:
    override = (_get_scoped_secret("SENDGRID_API_BASE") or "").strip()
    return (override or SENDGRID_API_BASE_DEFAULT).rstrip("/")


def _mask_email(address: str) -> str:
    if "@" not in address:
        return "***"
    local, _, domain = address.partition("@")
    masked_local = (
        "*" * len(local)
        if len(local) <= 2
        else local[0] + "*" * (len(local) - 2) + local[-1]
    )
    return f"{masked_local}@{domain}"


_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _redact_emails_in_text(text: str) -> str:
    """Mask any email addresses inside arbitrary text before it is logged or
    handed back to a caller. SendGrid's own validation errors commonly echo
    the offending to/from address back in ``errors[].message`` -- unlike
    ``chat_id``, that text was never run through ``_mask_email`` before, so
    a bad address could reach application logs, and from there
    ``tools/send_message_tool.py``'s ``{"error": f"Adapter send failed:
    {result.error}"}`` puts it straight into the agent's own context too.
    """
    return _EMAIL_IN_TEXT_RE.sub(lambda m: _mask_email(m.group(0)), text)


def _sanitize_subject(subject: str) -> str:
    """Strip CR/LF/tab so a subject can never inject extra headers into the
    outbound email (CWE-93), regardless of whether it came from the
    first-line convention (which can still carry a bare ``\\r`` even though
    ``partition("\\n")`` rules out ``\\n``) or an explicit metadata override
    (which has no structural protection at all).
    """
    cleaned = re.sub(r"[\r\n\t]+", " ", subject).strip()
    return cleaned or DEFAULT_SUBJECT


def _split_subject_and_body(content: str) -> tuple:
    if "\n" in content:
        first_line, _, rest = content.partition("\n")
        first_line = first_line.strip()
        rest = rest.strip()
        if first_line and rest:
            return first_line, rest
    return DEFAULT_SUBJECT, content.strip()


def check_email_requirements() -> bool:
    """Passive probe: dependencies + minimal config present right now."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(
        _get_scoped_secret("SENDGRID_API_KEY")
        and _get_scoped_secret("SENDGRID_FROM_EMAIL")
    )


class TwilioEmailAdapter(BasePlatformAdapter):
    """Outbound-only Twilio Email adapter (SendGrid Mail Send API)."""

    MAX_MESSAGE_LENGTH = MAX_EMAIL_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("twilio_email"))
        self._api_key: str = _get_scoped_secret("SENDGRID_API_KEY", "") or ""
        self._from_email: str = _get_scoped_secret("SENDGRID_FROM_EMAIL", "") or ""
        self._from_name: str = _get_scoped_secret("SENDGRID_FROM_NAME", "") or ""
        self._http_session: Optional[Any] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._api_key:
            msg = (
                "[twilio_email] SENDGRID_API_KEY not set -- cannot send. "
                "Create an API key with Mail Send permission in the SendGrid "
                "dashboard and set it here."
            )
            logger.error(msg)
            self._set_fatal_error("twilio_email_missing_api_key", msg, retryable=False)
            return False
        if not self._from_email:
            msg = (
                "[twilio_email] SENDGRID_FROM_EMAIL not set -- cannot send. "
                "Verify a sender (Single Sender or Domain Authentication) in "
                "the SendGrid dashboard and set its address here."
            )
            logger.error(msg)
            self._set_fatal_error(
                "twilio_email_missing_from_email", msg, retryable=False
            )
            return False
        self._mark_connected()
        logger.info("[twilio_email] Ready (outbound-only, no inbound channel)")
        return True

    async def disconnect(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()
        logger.info("[twilio_email] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        import aiohttp

        explicit_subject = (metadata or {}).get("subject")
        html = bool((metadata or {}).get("html"))
        if isinstance(explicit_subject, str) and explicit_subject:
            subject, body = explicit_subject, content
        else:
            subject, body = _split_subject_and_body(content)
        subject = _sanitize_subject(subject)
        body = self.format_message(body)

        if not body.strip():
            return SendResult(
                success=False, error="Refusing to send an email with an empty body"
            )

        url = f"{_sendgrid_api_base()}/mail/send"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "personalizations": [{"to": [{"email": chat_id}]}],
            "from": {
                "email": self._from_email,
                **({"name": self._from_name} if self._from_name else {}),
            },
            "subject": subject,
            "content": [{"type": "text/html" if html else "text/plain", "value": body}],
        }

        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    error_body = _redact_emails_in_text(await resp.text())
                    logger.error(
                        "[twilio_email] send failed to %s: %s %s",
                        _mask_email(chat_id),
                        resp.status,
                        error_body,
                    )
                    return SendResult(
                        success=False, error=f"SendGrid {resp.status}: {error_body}"
                    )
                return SendResult(
                    success=True, message_id=resp.headers.get("X-Message-Id", "")
                )
        except Exception as e:
            logger.error("[twilio_email] send error to %s: %s", _mask_email(chat_id), e)
            return SendResult(success=False, error=str(e))
        finally:
            if not self._http_session and session:
                await session.close()

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        # Email renders rich content properly, unlike SMS/RCS -- no markdown stripping.
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
    """Out-of-process Email delivery for `hermes send` and cron
    `deliver=twilio_email` when no live gateway adapter is present in this
    process."""
    import aiohttp

    api_key = _get_scoped_secret("SENDGRID_API_KEY", "") or ""
    from_email = _get_scoped_secret("SENDGRID_FROM_EMAIL", "") or ""
    from_name = _get_scoped_secret("SENDGRID_FROM_NAME", "") or ""
    if not (api_key and from_email):
        return {
            "error": "Twilio Email not configured (SENDGRID_API_KEY, SENDGRID_FROM_EMAIL required)"
        }

    subject, body = _split_subject_and_body(message)
    subject = _sanitize_subject(subject)
    if not body.strip():
        return {"error": "Refusing to send an email with an empty body"}

    try:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        proxy = resolve_proxy_url()
        sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
        url = f"{_sendgrid_api_base()}/mail/send"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {
            "personalizations": [{"to": [{"email": chat_id}]}],
            "from": {"email": from_email, **({"name": from_name} if from_name else {})},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), **sess_kw
        ) as session:
            async with session.post(
                url, json=payload, headers=headers, **req_kw
            ) as resp:
                if resp.status >= 400:
                    error_body = _redact_emails_in_text(await resp.text())
                    logger.error(
                        "[twilio_email] standalone send failed to %s: %s %s",
                        _mask_email(chat_id),
                        resp.status,
                        error_body,
                    )
                    return {
                        "error": f"SendGrid API error ({resp.status}): {error_body}"
                    }
                return {
                    "success": True,
                    "platform": "twilio_email",
                    "chat_id": chat_id,
                    "message_id": resp.headers.get("X-Message-Id", ""),
                }
    except Exception as e:
        logger.error(
            "[twilio_email] standalone send error to %s: %s", _mask_email(chat_id), e
        )
        return {"error": f"Twilio Email send failed: {e}"}


def _is_connected(config) -> bool:
    return bool((_get_scoped_secret("SENDGRID_FROM_EMAIL") or "").strip()) and bool(
        (_get_scoped_secret("SENDGRID_API_KEY") or "").strip()
    )


def _build_adapter(config):
    return TwilioEmailAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="twilio_email",
        label="Twilio Email",
        adapter_factory=_build_adapter,
        check_fn=check_email_requirements,
        is_connected=_is_connected,
        required_env=["SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"],
        install_hint="pip install aiohttp",
        cron_deliver_env_var="SENDGRID_HOME_CHANNEL",
        parse_target_ref_fn=parse_target_ref,
        validate_target_ref_fn=validate_target_ref,
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_EMAIL_LENGTH,
        pii_safe=True,
        emoji="📧",
        allow_update_command=False,
        platform_hint=(
            "You are sending via Twilio Email (SendGrid). The first line of your "
            "message becomes the subject; the rest becomes the body. Plain text "
            "unless the caller explicitly requests HTML."
        ),
    )
