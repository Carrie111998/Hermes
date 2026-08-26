"""Email channel for the Twilio plugin.

Sends through Twilio's Email API — SendGrid's Mail Send API under the
hood. Twilio's Email product uses a completely separate credential
surface from core Twilio (SMS/RCS/Voice): a bearer ``SG.``-prefixed
SendGrid API key, not the ``TWILIO_ACCOUNT_SID``/``TWILIO_AUTH_TOKEN``
pair every Messages-API channel uses. That's why this channel implements
``Channel`` directly rather than ``MessagingChannel`` — it doesn't use
``MessagingServiceSid`` or the Messages.json resource at all, and its
transport lives entirely in this file rather than in
``core/messages_api.py``.

Every other channel in this plugin passes a single `content` string with
no subject concept. By convention here: the first line of `content` is
the subject and the remainder (after the first newline) is the body;
single-line content gets a generic default subject rather than guessing
one. Callers with more control (e.g. our own standalone CLI use) can
override via `metadata={"subject": ..., "html": True}`.

Unlike the RCS channel, this one never splits a message into multiple
chunks/sends: an email is one document, not a multi-part SMS train.

Self-contained: nothing outside this file needs to change to modify Email
behavior, and this file never reaches into another channel's module. If a
future channel also needs SendGrid, extract the shared bits into
``core/sendgrid_api.py`` then — not preemptively, for one consumer.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple

from ..core.credentials import get_scoped_secret
from .base import Channel

logger = logging.getLogger(__name__)

SENDGRID_API_BASE_DEFAULT = "https://api.sendgrid.com/v3"
# Generous — SendGrid's real cap is far larger than any agent-generated
# message; this just guards against something pathological.
MAX_EMAIL_LENGTH = 200_000
DEFAULT_SUBJECT = "Message from Hermes Agent"

# Mirrors the RCS channel's _E164_TARGET_RE role — this isn't a phone
# number at all, so it declares its own parser to accept bare email
# addresses as targets. Phone vs email formats are mutually exclusive by
# construction, so adapter.py can safely try each channel in turn.
_EMAIL_TARGET_RE = re.compile(r"^\s*[^@\s]+@[^@\s]+\.[^@\s]+\s*$")
_EMAIL_IN_TEXT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


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


def _split_subject_and_body(content: str) -> Tuple[str, str]:
    if "\n" in content:
        first_line, _, rest = content.partition("\n")
        first_line = first_line.strip()
        rest = rest.strip()
        if first_line and rest:
            return first_line, rest
    return DEFAULT_SUBJECT, content.strip()


class EmailChannel(Channel):
    name = "email"
    max_message_length = MAX_EMAIL_LENGTH
    required_env = ["SENDGRID_API_KEY", "SENDGRID_FROM_EMAIL"]
    cron_deliver_env_var = "SENDGRID_HOME_CHANNEL"
    platform_hint = (
        "You are sending via Twilio Email (SendGrid). The first line of your "
        "message becomes the subject; the rest becomes the body. Plain text "
        "unless the caller explicitly requests HTML."
    )

    def _api_base(self) -> str:
        override = (get_scoped_secret("SENDGRID_API_BASE") or "").strip()
        return (override or SENDGRID_API_BASE_DEFAULT).rstrip("/")

    def check_requirements(self) -> bool:
        return bool(
            get_scoped_secret("SENDGRID_API_KEY") and get_scoped_secret("SENDGRID_FROM_EMAIL")
        )

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        if not get_scoped_secret("SENDGRID_API_KEY"):
            return False, (
                "SENDGRID_API_KEY not set — cannot send. Create an API key "
                "with Mail Send permission in the SendGrid dashboard and set "
                "it here."
            )
        if not get_scoped_secret("SENDGRID_FROM_EMAIL"):
            return False, (
                "SENDGRID_FROM_EMAIL not set — cannot send. Verify a sender "
                "(Single Sender or Domain Authentication) in the SendGrid "
                "dashboard and set its address here."
            )
        return True, None

    def is_connected(self) -> bool:
        return bool((get_scoped_secret("SENDGRID_FROM_EMAIL") or "").strip()) and bool(
            (get_scoped_secret("SENDGRID_API_KEY") or "").strip()
        )

    def parse_target_ref(self, target_ref: str):
        if _EMAIL_TARGET_RE.fullmatch(target_ref):
            return target_ref.strip(), None
        return None

    def validate_target_ref(self, chat_id: str):
        return True if _EMAIL_TARGET_RE.fullmatch(chat_id) else "not a valid email address"

    def format_message(self, content: str) -> str:
        # Email renders rich content properly, unlike SMS/RCS -- no markdown stripping.
        return content

    def _build_payload(
        self, chat_id: str, subject: str, body: str, html: bool, from_email: str, from_name: str
    ) -> Dict[str, Any]:
        return {
            "personalizations": [{"to": [{"email": chat_id}]}],
            "from": {"email": from_email, **({"name": from_name} if from_name else {})},
            "subject": subject,
            "content": [{"type": "text/html" if html else "text/plain", "value": body}],
        }

    async def send(
        self, chat_id: str, content: str, *, metadata: Optional[dict] = None, session=None
    ) -> Dict[str, Any]:
        import aiohttp

        api_key = get_scoped_secret("SENDGRID_API_KEY", "") or ""
        from_email = get_scoped_secret("SENDGRID_FROM_EMAIL", "") or ""
        from_name = get_scoped_secret("SENDGRID_FROM_NAME", "") or ""

        explicit_subject = (metadata or {}).get("subject")
        html = bool((metadata or {}).get("html"))
        if isinstance(explicit_subject, str) and explicit_subject:
            subject, body = explicit_subject, content
        else:
            subject, body = _split_subject_and_body(content)
        subject = _sanitize_subject(subject)
        body = self.format_message(body)

        if not body.strip():
            return {"success": False, "error": "Refusing to send an email with an empty body"}

        url = f"{self._api_base()}/mail/send"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = self._build_payload(chat_id, subject, body, html, from_email, from_name)

        owns_session = session is None
        session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True,
        )
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    error_body = _redact_emails_in_text(await resp.text())
                    logger.error(
                        "[twilio:email] send failed to %s: %s %s",
                        _mask_email(chat_id), resp.status, error_body,
                    )
                    return {"success": False, "error": f"SendGrid {resp.status}: {error_body}"}
                return {"success": True, "message_id": resp.headers.get("X-Message-Id", "")}
        except Exception as e:
            logger.error("[twilio:email] send error to %s: %s", _mask_email(chat_id), e)
            return {"success": False, "error": str(e)}
        finally:
            if owns_session:
                await session.close()

    async def standalone_send(self, pconfig, chat_id: str, message: str, **kwargs) -> Dict[str, Any]:
        import aiohttp

        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        api_key = get_scoped_secret("SENDGRID_API_KEY", "") or ""
        from_email = get_scoped_secret("SENDGRID_FROM_EMAIL", "") or ""
        from_name = get_scoped_secret("SENDGRID_FROM_NAME", "") or ""
        if not (api_key and from_email):
            return {
                "error": "Twilio Email not configured (SENDGRID_API_KEY, SENDGRID_FROM_EMAIL required)"
            }

        subject, body = _split_subject_and_body(message)
        subject = _sanitize_subject(subject)
        if not body.strip():
            return {"error": "Refusing to send an email with an empty body"}

        try:
            proxy = resolve_proxy_url()
            sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
            url = f"{self._api_base()}/mail/send"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = self._build_payload(chat_id, subject, body, False, from_email, from_name)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), **sess_kw) as session:
                async with session.post(url, json=payload, headers=headers, **req_kw) as resp:
                    if resp.status >= 400:
                        error_body = _redact_emails_in_text(await resp.text())
                        logger.error(
                            "[twilio:email] standalone send failed to %s: %s %s",
                            _mask_email(chat_id), resp.status, error_body,
                        )
                        return {"error": f"SendGrid API error ({resp.status}): {error_body}"}
                    return {
                        "success": True,
                        "platform": "twilio",
                        "chat_id": chat_id,
                        "message_id": resp.headers.get("X-Message-Id", ""),
                    }
        except Exception as e:
            logger.error("[twilio:email] standalone send error to %s: %s", _mask_email(chat_id), e)
            return {"error": f"Twilio Email send failed: {e}"}
