"""Twilio Email platform adapter — outbound-only.

Sends messages through Twilio's Email API (One Console): a REST API at
``comms.twilio.com``, distinct from the older SendGrid ``api.sendgrid.com``
v3 Mail Send API. Auth is the same core Twilio credential pair used by
SMS/Voice — ``TWILIO_ACCOUNT_SID`` / ``TWILIO_AUTH_TOKEN`` via HTTP Basic
Auth — not a separate SendGrid API key.

Docs: https://www.twilio.com/docs/email/api/overview

Env vars:
  - TWILIO_ACCOUNT_SID      (shared with the sms plugin/telephony skill)
  - TWILIO_AUTH_TOKEN       (shared with the sms plugin/telephony skill)
  - TWILIO_EMAIL_FROM       (default sender — must be a verified sender
                              identity for the Email product in the Twilio
                              Console)
  - TWILIO_EMAIL_FROM_NAME  (optional sender display name)
  - TWILIO_EMAIL_API_BASE   (optional; defaults to the public Email API)
  - TWILIO_EMAIL_HOME_CHANNEL  (optional — destination for cron delivery)

There is no inbound channel, so connect()/disconnect() are readiness checks
only. Delivery always goes through send() (live gateway) or
_standalone_send() (out-of-process `hermes send` / cron delivery); in
practice `_standalone_send()` is the one that actually runs, since
`hermes send` and cron jobs run in their own process.

**Async by design, not a delivery guarantee.** A successful call returns
``202`` with an ``operationId`` — the send was accepted for processing, not
delivered. Actual delivery status lives behind the Email Operation resource
(``GET .../Operations/{operationId}``), which this adapter does not poll;
``SendResult.message_id`` / the standalone dict's ``message_id`` carry the
``operationId`` for anyone who wants to check later.

Every other platform this adapter sits alongside passes a single `content`
string with no subject concept. By convention here: the first line of
`content` is the subject and the remainder (after the first newline) is the
body; single-line content gets a generic default subject rather than
guessing one. Callers with more control (e.g. our own standalone CLI use)
can override via `metadata={"subject": ..., "html": True, "attachments":
[...]}`.

Attachments: `send_image()`/`send_document()`/`send_multiple_images()` (live
gateway) and `media_files` on `_standalone_send()` (the `hermes send`/cron
path) all attach local files as base64 content in the request's
`content.attachments` array. Remote (http/https) image URLs are not
downloaded — they're linked in the body text instead, matching the built-in
`email` plugin's own convention for remote images.

Known gaps (unconfirmed API shape or out of scope for this pass): no cc/bcc,
no scheduled send (`schedule.sendAt`), no inline `cid` image references —
add these once their exact request shape is confirmed against the live API.

Unlike the SMS adapter's `send()`, this adapter never splits a message into
multiple chunks/sends: an email is one document, not a multi-part SMS train,
so `truncate_message()` is deliberately not called here.
"""

import asyncio
import base64
import logging
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult

from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
from agent.secret_scope import get_secret as _scoped_get_secret

logger = logging.getLogger(__name__)

TWILIO_EMAIL_API_BASE_DEFAULT = "https://comms.twilio.com/v1/Emails"
# Generous — the real cap is far larger than any agent-generated message;
# this just guards against something pathological.
MAX_EMAIL_LENGTH = 200_000
DEFAULT_SUBJECT = "Message from Hermes Agent"
# Raw bytes, before base64 (~4/3 inflation). The API caps the whole request
# (JSON + base64 attachments) at 10 MB; this leaves headroom for that
# overhead rather than let a near-the-limit send 400 server-side.
MAX_ATTACHMENT_BYTES_RAW = 7 * 1024 * 1024

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


def _twilio_email_api_base() -> str:
    override = (_get_scoped_secret("TWILIO_EMAIL_API_BASE") or "").strip()
    return (override or TWILIO_EMAIL_API_BASE_DEFAULT).rstrip("/")


def _basic_auth_header(account_sid: str, auth_token: str) -> str:
    """Build the HTTP Basic auth header value for Twilio.

    Mirrors plugins/platforms/sms/adapter.py::SmsAdapter._basic_auth_header.
    """
    creds = f"{account_sid}:{auth_token}"
    encoded = base64.b64encode(creds.encode("ascii")).decode("ascii")
    return f"Basic {encoded}"


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
    handed back to a caller. The API's own validation errors can echo the
    offending to/from address back in the response body -- unlike
    ``chat_id``, that text was never run through ``_mask_email`` before, so
    a bad address could reach application logs, and from there
    ``tools/send_message_tool.py``'s ``{"error": f"Adapter send failed:
    {result.error}"}`` puts it straight into the agent's own context too.
    """
    return _EMAIL_IN_TEXT_RE.sub(lambda m: _mask_email(m.group(0)), text)


def _format_exception_error(e: Exception) -> str:
    """Render an exception so it's never an empty/near-empty error string.

    ``asyncio.TimeoutError`` and several aiohttp connector/SSL errors stringify
    to ``""``, which would otherwise surface as a blank error to both the log
    and the caller (e.g. ``tools/send_message_tool.py``'s ``f"Adapter send
    failed: {result.error}"``) with no clue what actually happened. Also runs
    the result through ``_redact_emails_in_text`` -- defense in depth, since an
    exception's message could in principle echo back request/response content.
    """
    text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
    return _redact_emails_in_text(text)


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


def _build_attachments(
    file_paths: List[str],
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Read local files into the API's attachment shape (base64 content).

    Returns ``(attachments, error)``. On any missing/unreadable file or a
    combined size over ``MAX_ATTACHMENT_BYTES_RAW``, returns ``([], error)``
    -- the whole send is refused rather than going out with only some of
    its attachments, which would silently misrepresent what was sent.
    """
    attachments: List[Dict[str, str]] = []
    total_bytes = 0
    for file_path in file_paths:
        if not os.path.isfile(file_path):
            return [], f"Attachment not found: {file_path}"
        try:
            size = os.path.getsize(file_path)
        except OSError as e:
            return [], f"Could not read attachment {file_path}: {e}"
        total_bytes += size
        if total_bytes > MAX_ATTACHMENT_BYTES_RAW:
            # Checked before reading -- refuse an oversized file outright rather
            # than loading it fully into memory first.
            return [], (
                f"Attachments too large ({total_bytes} bytes) -- Twilio Email caps "
                "the whole request (including base64-encoded attachments) at 10 MB"
            )
        try:
            with open(file_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            return [], f"Could not read attachment {file_path}: {e}"
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        attachments.append({
            "filename": os.path.basename(file_path),
            "contentType": content_type,
            "content": base64.b64encode(raw).decode("ascii"),
        })
    return attachments, None


async def _build_attachments_async(
    file_paths: List[str],
) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Run _build_attachments() off the event loop.

    It does blocking file I/O (and base64-encodes up to ~7 MB), which would
    otherwise stall every other chat/platform the gateway is servicing for
    the duration -- matches the built-in `email` plugin's own
    `loop.run_in_executor(...)` convention for blocking send-path work.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None, _build_attachments, file_paths
    )


def check_email_requirements() -> bool:
    """Passive probe: dependencies + minimal config present right now."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return bool(
        _get_scoped_secret("TWILIO_ACCOUNT_SID")
        and _get_scoped_secret("TWILIO_AUTH_TOKEN")
        and _get_scoped_secret("TWILIO_EMAIL_FROM")
    )


class TwilioEmailAdapter(BasePlatformAdapter):
    """Outbound-only Twilio Email adapter (comms.twilio.com Email API)."""

    MAX_MESSAGE_LENGTH = MAX_EMAIL_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("twilio_email"))
        self._account_sid: str = _get_scoped_secret("TWILIO_ACCOUNT_SID", "") or ""
        self._auth_token: str = _get_scoped_secret("TWILIO_AUTH_TOKEN", "") or ""
        self._from_email: str = _get_scoped_secret("TWILIO_EMAIL_FROM", "") or ""
        self._from_name: str = _get_scoped_secret("TWILIO_EMAIL_FROM_NAME", "") or ""
        self._http_session: Optional[Any] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not (self._account_sid and self._auth_token):
            msg = (
                "[twilio_email] TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN not set -- "
                "cannot send. These are the same core Twilio credentials used for "
                "SMS/Voice -- find them on the Twilio Console dashboard."
            )
            logger.error(msg)
            self._set_fatal_error(
                "twilio_email_missing_credentials", msg, retryable=False
            )
            return False
        if not self._from_email:
            msg = (
                "[twilio_email] TWILIO_EMAIL_FROM not set -- cannot send. Verify a "
                "sender identity for the Email product in the Twilio Console and "
                "set its address here."
            )
            logger.error(msg)
            self._set_fatal_error(
                "twilio_email_missing_from_email", msg, retryable=False
            )
            return False
        import aiohttp

        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        self._mark_connected()
        logger.info("[twilio_email] Ready (outbound-only, no inbound channel)")
        return True

    async def disconnect(self) -> None:
        if self._http_session:
            await self._http_session.close()
            self._http_session = None
        self._mark_disconnected()
        logger.info("[twilio_email] Disconnected")

    async def _send_email_request(
        self,
        chat_id: str,
        subject: str,
        body: str,
        *,
        html: bool = False,
        attachments: Optional[List[Dict[str, str]]] = None,
    ) -> SendResult:
        """Shared POST + response handling for send()/send_image()/
        send_document()/send_multiple_images()."""
        import aiohttp

        subject = _sanitize_subject(subject)
        body = self.format_message(body)
        if not body.strip() and not attachments:
            return SendResult(
                success=False, error="Refusing to send an email with an empty body"
            )

        url = _twilio_email_api_base()
        headers = {
            "Authorization": _basic_auth_header(self._account_sid, self._auth_token),
            "Content-Type": "application/json",
        }
        content: Dict[str, Any] = {
            "subject": subject,
            ("html" if html else "text"): body,
        }
        if attachments:
            content["attachments"] = attachments
        payload: Dict[str, Any] = {
            "from": {
                "address": self._from_email,
                **({"name": self._from_name} if self._from_name else {}),
            },
            "to": [{"address": chat_id}],
            "content": content,
        }

        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            trust_env=True,
        )
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    error_body = _redact_emails_in_text(await resp.text())
                    retry_after = resp.headers.get("Retry-After")
                    suffix = f" (retry after {retry_after}s)" if retry_after else ""
                    logger.error(
                        "[twilio_email] send failed to %s: %s %s",
                        _mask_email(chat_id),
                        resp.status,
                        error_body,
                    )
                    return SendResult(
                        success=False,
                        error=f"Twilio Email {resp.status}: {error_body}{suffix}",
                    )
                # Twilio already accepted the request at this point -- a body-parse
                # failure here is not a failed send (a caller retrying on a false
                # failure could cause a duplicate email), so it's handled separately
                # from the network/request exceptions below.
                try:
                    data = await resp.json()
                except Exception as parse_err:
                    data = None
                    logger.warning(
                        "[twilio_email] Queued to %s but response body didn't parse: %s",
                        _mask_email(chat_id),
                        parse_err,
                    )
                # aiohttp's resp.json() returns None (no exception) for an empty
                # body -- treat that the same as a parse failure, not an AttributeError.
                if data is None:
                    return SendResult(success=True, message_id="")
                operation_id = data.get("operationId", "")
                if not operation_id:
                    logger.warning(
                        "[twilio_email] 202 response missing operationId, raw body: %s",
                        data,
                    )
                logger.info(
                    "[twilio_email] Queued to %s (operationId=%s)",
                    _mask_email(chat_id),
                    operation_id,
                )
                return SendResult(success=True, message_id=operation_id)
        except Exception as e:
            error_text = _format_exception_error(e)
            logger.error(
                "[twilio_email] send error to %s: %s",
                _mask_email(chat_id),
                error_text,
                exc_info=True,
            )
            return SendResult(success=False, error=error_text)
        finally:
            if not self._http_session and session:
                await session.close()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        meta = metadata or {}
        explicit_subject = meta.get("subject")
        html = bool(meta.get("html"))
        if isinstance(explicit_subject, str) and explicit_subject:
            subject, body = explicit_subject, content
        else:
            subject, body = _split_subject_and_body(content)

        attachment_paths = meta.get("attachments") or []
        attachments: List[Dict[str, str]] = []
        if attachment_paths:
            attachments, attach_error = await _build_attachments_async(
                list(attachment_paths)
            )
            if attach_error:
                return SendResult(success=False, error=attach_error)

        return await self._send_email_request(
            chat_id, subject, body, html=html, attachments=attachments
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Attach a local image directly; link remote images in the body.

        Remote images are not downloaded here -- same convention the
        built-in `email` plugin uses for remote image URLs.
        """
        if image_url.startswith("file://"):
            from urllib.parse import unquote

            local_path = unquote(image_url[7:])
            attachments, attach_error = await _build_attachments_async([local_path])
            if attach_error:
                return SendResult(success=False, error=attach_error)
            subject, body = _split_subject_and_body(caption or "")
            return await self._send_email_request(
                chat_id, subject, body, attachments=attachments
            )

        text = f"{caption}\n\nImage: {image_url}" if caption else f"Image: {image_url}"
        subject, body = _split_subject_and_body(text)
        return await self._send_email_request(chat_id, subject, body)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images as one email with multiple attachments.

        Local files are attached directly (one API call, multiple
        attachments); remote URLs are linked in the body instead of
        downloaded, matching send_image()'s convention.
        """
        if not images:
            return

        from urllib.parse import unquote

        local_paths: List[str] = []
        body_lines: List[str] = []
        for image_url, alt_text in images:
            if image_url.startswith("file://"):
                local_paths.append(unquote(image_url[7:]))
                if alt_text:
                    body_lines.append(alt_text)
            else:
                body_lines.append(
                    f"{alt_text}\nImage: {image_url}"
                    if alt_text
                    else f"Image: {image_url}"
                )

        try:
            attachments: List[Dict[str, str]] = []
            if local_paths:
                attachments, attach_error = await _build_attachments_async(local_paths)
                if attach_error:
                    logger.error(
                        "[twilio_email] multi-image send failed: %s", attach_error
                    )
                    await super().send_multiple_images(
                        chat_id, images, metadata, human_delay
                    )
                    return
            result = await self._send_email_request(
                chat_id,
                DEFAULT_SUBJECT,
                "\n\n".join(body_lines),
                attachments=attachments,
            )
            if not result.success:
                logger.error("[twilio_email] multi-image send failed: %s", result.error)
        except Exception as e:
            logger.error(
                "[twilio_email] multi-image send failed, falling back: %s",
                e,
                exc_info=True,
            )
            await super().send_multiple_images(chat_id, images, metadata, human_delay)

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
        attachments, attach_error = await _build_attachments_async([file_path])
        if attach_error:
            return SendResult(success=False, error=attach_error)
        if file_name:
            attachments[0]["filename"] = file_name
        subject, body = _split_subject_and_body(caption or "")
        return await self._send_email_request(
            chat_id, subject, body, attachments=attachments
        )

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
    process.

    `force_document` has no effect -- the Email API's attachments have no
    inline-vs-document distinction (unlike Telegram/WhatsApp document mode).
    """
    import aiohttp

    account_sid = _get_scoped_secret("TWILIO_ACCOUNT_SID", "") or ""
    auth_token = _get_scoped_secret("TWILIO_AUTH_TOKEN", "") or ""
    from_email = _get_scoped_secret("TWILIO_EMAIL_FROM", "") or ""
    from_name = _get_scoped_secret("TWILIO_EMAIL_FROM_NAME", "") or ""
    if not (account_sid and auth_token and from_email):
        return {
            "error": (
                "Twilio Email not configured (TWILIO_ACCOUNT_SID, "
                "TWILIO_AUTH_TOKEN, TWILIO_EMAIL_FROM required)"
            )
        }

    subject, body = _split_subject_and_body(message)
    subject = _sanitize_subject(subject)

    attachments: List[Dict[str, str]] = []
    media_paths = [path for path, _is_voice in (media_files or [])]
    if media_paths:
        attachments, attach_error = await _build_attachments_async(media_paths)
        if attach_error:
            return {"error": attach_error}

    if not body.strip() and not attachments:
        return {"error": "Refusing to send an email with an empty body"}

    try:
        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        proxy = resolve_proxy_url()
        sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
        url = _twilio_email_api_base()
        headers = {
            "Authorization": _basic_auth_header(account_sid, auth_token),
            "Content-Type": "application/json",
        }
        content: Dict[str, Any] = {"subject": subject, "text": body}
        if attachments:
            content["attachments"] = attachments
        payload: Dict[str, Any] = {
            "from": {
                "address": from_email,
                **({"name": from_name} if from_name else {}),
            },
            "to": [{"address": chat_id}],
            "content": content,
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
                    return {"error": f"Twilio Email {resp.status}: {error_body}"}
                # Twilio already accepted the request at this point -- a body-parse
                # failure here is not a failed send (a caller retrying on a false
                # failure could cause a duplicate email).
                try:
                    data = await resp.json()
                except Exception as parse_err:
                    data = None
                    logger.warning(
                        "[twilio_email] Queued to %s but response body didn't parse: %s",
                        _mask_email(chat_id),
                        parse_err,
                    )
                # aiohttp's resp.json() returns None (no exception) for an empty
                # body -- treat that the same as a parse failure, not an AttributeError.
                if data is None:
                    return {
                        "success": True,
                        "platform": "twilio_email",
                        "chat_id": chat_id,
                        "message_id": "",
                    }
                operation_id = data.get("operationId", "")
                if not operation_id:
                    logger.warning(
                        "[twilio_email] 202 response missing operationId, raw body: %s",
                        data,
                    )
                return {
                    "success": True,
                    "platform": "twilio_email",
                    "chat_id": chat_id,
                    "message_id": operation_id,
                }
    except Exception as e:
        error_text = _format_exception_error(e)
        logger.error(
            "[twilio_email] standalone send error to %s: %s",
            _mask_email(chat_id),
            error_text,
            exc_info=True,
        )
        return {"error": f"Twilio Email send failed: {error_text}"}


def _is_connected(config) -> bool:
    return bool(
        (_get_scoped_secret("TWILIO_ACCOUNT_SID") or "").strip()
        and (_get_scoped_secret("TWILIO_AUTH_TOKEN") or "").strip()
        and (_get_scoped_secret("TWILIO_EMAIL_FROM") or "").strip()
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
        required_env=["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_EMAIL_FROM"],
        install_hint="pip install aiohttp",
        cron_deliver_env_var="TWILIO_EMAIL_HOME_CHANNEL",
        parse_target_ref_fn=parse_target_ref,
        validate_target_ref_fn=validate_target_ref,
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_EMAIL_LENGTH,
        pii_safe=True,
        emoji="📧",
        allow_update_command=False,
        platform_hint=(
            "You are sending via Twilio Email. The first line of your "
            "message becomes the subject; the rest becomes the body. Plain "
            "text unless the caller explicitly requests HTML."
        ),
    )
