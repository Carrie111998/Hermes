"""Email channel for the Twilio plugin.

Sends through Twilio's Email API (One Console): a REST API at
``comms.twilio.com``, distinct from the older SendGrid ``api.sendgrid.com``
v3 Mail Send API. Auth is the same core Twilio credential pair every other
channel here uses -- ``TWILIO_ACCOUNT_SID``/``TWILIO_AUTH_TOKEN`` via HTTP
Basic Auth, not a separate SendGrid key -- so this channel reuses
``core/credentials.py`` rather than declaring its own credential reader.

Docs: https://www.twilio.com/docs/email/api/overview

That's still why this channel implements ``Channel`` directly rather than
``MessagingChannel``: it doesn't use ``MessagingServiceSid`` or the
Messages.json resource at all -- its request/response shape (JSON body,
async 202 + ``operationId``, not form-encoded) is nothing like the other
channels' transport.

Every other channel in this plugin passes a single `content` string with
no subject concept. By convention here: the first line of `content` is
the subject and the remainder (after the first newline) is the body;
single-line content gets a generic default subject rather than guessing
one. Callers with more control (e.g. direct-Python use) can override via
`metadata={"subject": ..., "html": True, "attachments": [...]}`.

**Async by design, not a delivery guarantee.** A successful call returns
``202`` with an ``operationId`` -- the send was accepted for processing,
not delivered. This channel doesn't poll the Email Operation resource
(``GET .../Operations/{operationId}``); the ``operationId`` comes back as
``message_id`` for anyone who wants to check later.

**Two payload quirks confirmed live, contradicting the docs:**
- ``from.name`` must always be present, or the API returns a generic
  "Invalid value provided for field 'from'" that masks the actual (more
  specific) validation error underneath, e.g. domain authorization.
- ``content.html`` is required even for a plain-text send. The docs
  describe auto-generating a ``text`` fallback *from* ``html``, not the
  reverse -- a request with only ``content.text`` gets rejected.

Attachments: `send_image()`/`send_document()`/`send_multiple_images()`
(live gateway, dispatched from `adapter.py`) and `media_files` on
`standalone_send()` (the `hermes send`/cron path) all attach local files
as base64 content in the request's `content.attachments` array. Remote
(http/https) image URLs are not downloaded -- they're linked in the body
text instead, matching the built-in `email` plugin's own convention.

Known gaps (unconfirmed API shape or out of scope so far): no cc/bcc, no
scheduled send (`schedule.sendAt`), no inline `cid` image references.

Unlike RCS, this channel never splits a message into multiple
chunks/sends: an email is one document, not a multi-part SMS train.

Self-contained: nothing outside this file needs to change to modify Email
behavior beyond `core/credentials.py`'s shared helpers, and this file
never reaches into another channel's module. If a future channel also
needs this same JSON/REST transport shape, extract the shared bits into
a `core/` module then -- not preemptively, for one consumer.
"""

import asyncio
import base64
import html as _html_lib
import logging
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..core.credentials import (
    basic_auth_header,
    get_account_credentials,
    get_scoped_secret,
)
from .base import Channel

logger = logging.getLogger(__name__)

TWILIO_EMAIL_API_BASE_DEFAULT = "https://comms.twilio.com/v1/Emails"
# Generous -- the real cap is far larger than any agent-generated message;
# this just guards against something pathological.
MAX_EMAIL_LENGTH = 200_000
DEFAULT_SUBJECT = "Message from Hermes Agent"
# See module docstring's "payload quirks" -- from.name is not truly
# optional despite the docs, so always send one.
DEFAULT_FROM_NAME = "Hermes Agent"
# Raw bytes, before base64 (~4/3 inflation). The API caps the whole request
# (JSON + base64 attachments) at 10 MB; this leaves headroom for that
# overhead rather than let a near-the-limit send 400 server-side.
MAX_ATTACHMENT_BYTES_RAW = 7 * 1024 * 1024

# Mirrors the RCS channel's _E164_TARGET_RE role -- this isn't a phone
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

    ``asyncio.TimeoutError`` and several aiohttp connector/SSL errors
    stringify to ``""``. Also runs the result through
    ``_redact_emails_in_text`` -- defense in depth, since an exception's
    message could in principle echo back request/response content.
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


def _split_subject_and_body(content: str) -> Tuple[str, str]:
    if "\n" in content:
        first_line, _, rest = content.partition("\n")
        first_line = first_line.strip()
        rest = rest.strip()
        if first_line and rest:
            return first_line, rest
    return DEFAULT_SUBJECT, content.strip()


def _plain_text_to_html(text: str) -> str:
    """Minimal plain-text -> HTML so a plain-text send always has a
    non-empty ``content.html`` -- see module docstring's "payload quirks".
    """
    return _html_lib.escape(text).replace("\n", "<br>\n")


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
            # Checked before reading -- refuse an oversized file outright
            # rather than loading it fully into memory first.
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


class EmailChannel(Channel):
    name = "email"
    max_message_length = MAX_EMAIL_LENGTH
    required_env = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_EMAIL_FROM"]
    cron_deliver_env_var = "TWILIO_EMAIL_HOME_CHANNEL"
    platform_hint = (
        "You are sending via Twilio Email. The first line of your message "
        "becomes the subject; the rest becomes the body. Plain text unless "
        "the caller explicitly requests HTML."
    )

    def _api_base(self) -> str:
        override = (get_scoped_secret("TWILIO_EMAIL_API_BASE") or "").strip()
        return (override or TWILIO_EMAIL_API_BASE_DEFAULT).rstrip("/")

    def check_requirements(self) -> bool:
        return bool(
            get_scoped_secret("TWILIO_ACCOUNT_SID")
            and get_scoped_secret("TWILIO_AUTH_TOKEN")
            and get_scoped_secret("TWILIO_EMAIL_FROM")
        )

    def connect_requirements_ok(self) -> Tuple[bool, Optional[str]]:
        if not get_scoped_secret("TWILIO_EMAIL_FROM"):
            return False, (
                "TWILIO_EMAIL_FROM not set — cannot send. Verify a sender "
                "identity for the Email product in the Twilio Console and "
                "set its address here."
            )
        return True, None

    def is_connected(self) -> bool:
        return bool((get_scoped_secret("TWILIO_EMAIL_FROM") or "").strip()) and bool(
            (get_scoped_secret("TWILIO_ACCOUNT_SID") or "").strip()
        )

    def parse_target_ref(self, target_ref: str):
        if _EMAIL_TARGET_RE.fullmatch(target_ref):
            return target_ref.strip(), None
        return None

    def validate_target_ref(self, chat_id: str):
        return (
            True if _EMAIL_TARGET_RE.fullmatch(chat_id) else "not a valid email address"
        )

    def format_message(self, content: str) -> str:
        # Email renders rich content properly, unlike SMS/RCS -- no markdown stripping.
        return content

    async def _post_email(
        self,
        chat_id: str,
        subject: str,
        body: str,
        *,
        html: bool = False,
        attachments: Optional[List[Dict[str, str]]] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Shared POST + response handling for send()/send_image()/
        send_document()/send_multiple_images()."""
        import aiohttp

        account_sid, auth_token = get_account_credentials()
        from_email = get_scoped_secret("TWILIO_EMAIL_FROM", "") or ""
        from_name = get_scoped_secret("TWILIO_EMAIL_FROM_NAME", "") or ""

        subject = _sanitize_subject(subject)
        body = self.format_message(body)
        if not body.strip() and not attachments:
            return {
                "success": False,
                "error": "Refusing to send an email with an empty body",
            }

        url = self._api_base()
        headers = {
            "Authorization": basic_auth_header(account_sid, auth_token),
            "Content-Type": "application/json",
        }
        # content.html is required by the API (see module docstring's
        # "payload quirks") -- a plain-text send needs an auto-derived html
        # body too.
        if html:
            content: Dict[str, Any] = {"subject": subject, "html": body}
        else:
            content = {
                "subject": subject,
                "text": body,
                "html": _plain_text_to_html(body),
            }
        if attachments:
            content["attachments"] = attachments
        payload: Dict[str, Any] = {
            "from": {"address": from_email, "name": from_name or DEFAULT_FROM_NAME},
            "to": [{"address": chat_id}],
            "content": content,
        }

        owns_session = session is None
        session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30), trust_env=True
        )
        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    error_body = _redact_emails_in_text(await resp.text())
                    retry_after = resp.headers.get("Retry-After")
                    suffix = f" (retry after {retry_after}s)" if retry_after else ""
                    logger.error(
                        "[twilio:email] send failed to %s: %s %s",
                        _mask_email(chat_id),
                        resp.status,
                        error_body,
                    )
                    return {
                        "success": False,
                        "error": f"Twilio Email {resp.status}: {error_body}{suffix}",
                    }
                # Twilio already accepted the request at this point -- a
                # body-parse failure here is not a failed send (a caller
                # retrying on a false failure could cause a duplicate
                # email), so it's handled separately from network/request
                # exceptions below.
                try:
                    data = await resp.json()
                except Exception as parse_err:
                    data = None
                    logger.warning(
                        "[twilio:email] Queued to %s but response body didn't parse: %s",
                        _mask_email(chat_id),
                        parse_err,
                    )
                # aiohttp's resp.json() returns None (no exception) for an
                # empty body -- treat that the same as a parse failure, not
                # an AttributeError.
                if data is None:
                    return {"success": True, "message_id": ""}
                operation_id = data.get("operationId", "")
                if not operation_id:
                    logger.warning(
                        "[twilio:email] 202 response missing operationId, raw body: %s",
                        data,
                    )
                logger.info(
                    "[twilio:email] Queued to %s (operationId=%s)",
                    _mask_email(chat_id),
                    operation_id,
                )
                return {"success": True, "message_id": operation_id}
        except Exception as e:
            error_text = _format_exception_error(e)
            logger.error(
                "[twilio:email] send error to %s: %s",
                _mask_email(chat_id),
                error_text,
                exc_info=True,
            )
            return {"success": False, "error": error_text}
        finally:
            if owns_session:
                await session.close()

    async def send(
        self,
        chat_id: str,
        content: str,
        *,
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
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
                return {"success": False, "error": attach_error}

        return await self._post_email(
            chat_id, subject, body, html=html, attachments=attachments, session=session
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Attach a local image directly; link remote images in the body.

        Remote images are not downloaded here -- same convention the
        built-in `email` plugin uses for remote image URLs.
        """
        if image_url.startswith("file://"):
            from urllib.parse import unquote

            local_path = unquote(image_url[7:])
            attachments, attach_error = await _build_attachments_async([local_path])
            if attach_error:
                return {"success": False, "error": attach_error}
            subject, body = _split_subject_and_body(caption or "")
            return await self._post_email(
                chat_id, subject, body, attachments=attachments, session=session
            )

        text = f"{caption}\n\nImage: {image_url}" if caption else f"Image: {image_url}"
        subject, body = _split_subject_and_body(text)
        return await self._post_email(chat_id, subject, body, session=session)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        attachments, attach_error = await _build_attachments_async([file_path])
        if attach_error:
            return {"success": False, "error": attach_error}
        if file_name:
            attachments[0]["filename"] = file_name
        subject, body = _split_subject_and_body(caption or "")
        return await self._post_email(
            chat_id, subject, body, attachments=attachments, session=session
        )

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[Tuple[str, str]],
        metadata: Optional[dict] = None,
        session=None,
    ) -> Dict[str, Any]:
        """Send a batch of images as one email with multiple attachments.

        Local files are attached directly (one API call, multiple
        attachments); remote URLs are linked in the body instead of
        downloaded, matching send_image()'s convention.
        """
        if not images:
            return {"success": True}

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

        attachments: List[Dict[str, str]] = []
        if local_paths:
            attachments, attach_error = await _build_attachments_async(local_paths)
            if attach_error:
                return {"success": False, "error": attach_error}

        return await self._post_email(
            chat_id,
            DEFAULT_SUBJECT,
            "\n\n".join(body_lines),
            attachments=attachments,
            session=session,
        )

    async def standalone_send(
        self, pconfig, chat_id: str, message: str, **kwargs
    ) -> Dict[str, Any]:
        """Out-of-process delivery for `hermes send` and cron
        `deliver=twilio` when no live gateway adapter is present. Accepts
        `media_files` (via **kwargs, per the Channel contract) for
        MEDIA:<path> attachments -- `force_document` has no effect, since
        the Email API's attachments have no inline-vs-document distinction
        (unlike Telegram/WhatsApp document mode).
        """
        import aiohttp

        from gateway.platforms.base import proxy_kwargs_for_aiohttp, resolve_proxy_url

        account_sid, auth_token = get_account_credentials(pconfig)
        from_email = get_scoped_secret("TWILIO_EMAIL_FROM", "") or ""
        from_name = get_scoped_secret("TWILIO_EMAIL_FROM_NAME", "") or ""
        if not (account_sid and auth_token and from_email):
            return {
                "error": (
                    "Twilio Email not configured (TWILIO_ACCOUNT_SID, "
                    "TWILIO_AUTH_TOKEN, TWILIO_EMAIL_FROM required)"
                )
            }

        subject, body = _split_subject_and_body(message)
        subject = _sanitize_subject(subject)

        media_files = kwargs.get("media_files")
        attachments: List[Dict[str, str]] = []
        media_paths = [path for path, _is_voice in (media_files or [])]
        if media_paths:
            attachments, attach_error = await _build_attachments_async(media_paths)
            if attach_error:
                return {"error": attach_error}

        if not body.strip() and not attachments:
            return {"error": "Refusing to send an email with an empty body"}

        try:
            proxy = resolve_proxy_url()
            sess_kw, req_kw = proxy_kwargs_for_aiohttp(proxy)
            url = self._api_base()
            headers = {
                "Authorization": basic_auth_header(account_sid, auth_token),
                "Content-Type": "application/json",
            }
            content: Dict[str, Any] = {
                "subject": subject,
                "text": body,
                "html": _plain_text_to_html(body),
            }
            if attachments:
                content["attachments"] = attachments
            payload: Dict[str, Any] = {
                "from": {"address": from_email, "name": from_name or DEFAULT_FROM_NAME},
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
                            "[twilio:email] standalone send failed to %s: %s %s",
                            _mask_email(chat_id),
                            resp.status,
                            error_body,
                        )
                        return {"error": f"Twilio Email {resp.status}: {error_body}"}
                    try:
                        data = await resp.json()
                    except Exception as parse_err:
                        data = None
                        logger.warning(
                            "[twilio:email] Queued to %s but response body didn't parse: %s",
                            _mask_email(chat_id),
                            parse_err,
                        )
                    if data is None:
                        return {
                            "success": True,
                            "platform": "twilio",
                            "chat_id": chat_id,
                            "message_id": "",
                        }
                    operation_id = data.get("operationId", "")
                    if not operation_id:
                        logger.warning(
                            "[twilio:email] 202 response missing operationId, raw body: %s",
                            data,
                        )
                    return {
                        "success": True,
                        "platform": "twilio",
                        "chat_id": chat_id,
                        "message_id": operation_id,
                    }
        except Exception as e:
            error_text = _format_exception_error(e)
            logger.error(
                "[twilio:email] standalone send error to %s: %s",
                _mask_email(chat_id),
                error_text,
                exc_info=True,
            )
            return {"error": f"Twilio Email send failed: {error_text}"}
