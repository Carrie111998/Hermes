"""Send the lawyer-approved document to the client by e-mail.

stdlib smtplib, run off the event loop. The one thing worth being strict
about: never send unless the draft is explicitly approved — that check
lives in the caller (``workflows.send_draft``), and this module refuses a
send with no recipient rather than guessing one.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from .config import Settings

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


def _valid_address(value: str) -> bool:
    _, address = parseaddr(value or "")
    return "@" in address and "." in address.split("@")[-1]


def build_message(
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    attachments: list[Attachment] | None = None,
    reply_to: str = "",
) -> EmailMessage:
    if not _valid_address(to):
        raise MailError(f"받는 사람 이메일이 올바르지 않습니다: {to!r}")
    sender = settings.smtp_from or settings.smtp_user
    if not _valid_address(sender):
        raise MailError("SMTP_FROM (또는 SMTP_USER) 이 설정되지 않았습니다")

    message = EmailMessage()
    message["From"] = formataddr((f"{settings.lawyer_name}", parseaddr(sender)[1]))
    message["To"] = to
    message["Subject"] = subject
    if reply_to or settings.lawyer_email:
        message["Reply-To"] = reply_to or settings.lawyer_email
    message.set_content(body)

    for attachment in attachments or []:
        maintype, _, subtype = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message


def _send_blocking(settings: Settings, message: EmailMessage) -> None:
    host, port = settings.smtp_host, settings.smtp_port
    if not host:
        raise MailError("SMTP_HOST 가 설정되지 않았습니다")
    try:
        if settings.smtp_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
        with server:
            server.ehlo()
            if settings.smtp_starttls and not settings.smtp_ssl:
                server.starttls()
                server.ehlo()
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"메일 발송 실패: {exc}") from exc


async def send_email(
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    attachments: list[Attachment] | None = None,
) -> None:
    message = build_message(
        settings, to=to, subject=subject, body=body, attachments=attachments
    )
    await asyncio.to_thread(_send_blocking, settings, message)
    log.info("sent document e-mail to %s", to)
