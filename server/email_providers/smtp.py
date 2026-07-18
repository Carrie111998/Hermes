"""Generic SMTP/IMAP adapter — any email service with a username + password.

The universal path: Gmail (app password), Outlook/Microsoft 365, Yahoo, Zoho,
corporate and custom-domain mailboxes all speak SMTP for send and IMAP for
drafts/replies. Sending stays deterministic (smtplib), never an agent action —
same contract as the Gmail/Microsoft API adapters.

Credentials dict:
  username, password           (required)
  smtp_host, smtp_port=587     (send; 465 => implicit SSL, else STARTTLS)
  imap_host, imap_port=993     (optional; enables create_draft + replies)
  from_addr                    (optional; defaults to username)
"""
from __future__ import annotations

import email
import imaplib
import smtplib
import time
from email.message import EmailMessage
from email.utils import parseaddr

from .base import OutgoingEmail, SendResult


class SmtpProvider:
    def __init__(self):
        self.credentials: dict = {}

    def connect_account(self, credentials: dict) -> None:
        self.credentials = dict(credentials)
        for key in ("username", "password", "smtp_host"):
            if not self.credentials.get(key):
                raise ValueError(f"SMTP credential '{key}' is required")

    def refresh_token(self) -> None:
        return None  # password auth has nothing to refresh

    def _sender(self) -> str:
        return self.credentials.get("from_addr") or self.credentials["username"]

    def _build(self, email_msg: OutgoingEmail) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._sender()
        message["To"] = email_msg.to
        if email_msg.cc:
            message["Cc"] = ", ".join(email_msg.cc)
        if email_msg.reply_to:
            message["Reply-To"] = email_msg.reply_to
        message["Subject"] = email_msg.subject
        subtype = "html" if "<" in email_msg.body and ">" in email_msg.body else "plain"
        message.set_content(email_msg.body, subtype=subtype)
        return message

    def send_email(self, email_msg: OutgoingEmail) -> SendResult:
        message = self._build(email_msg)
        recipients = [email_msg.to, *email_msg.cc]
        port = int(self.credentials.get("smtp_port", 587))
        host = self.credentials["smtp_host"]
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        try:
            server.login(self.credentials["username"], self.credentials["password"])
            server.send_message(message, from_addr=self._sender(), to_addrs=recipients)
        finally:
            server.quit()
        return SendResult(message.get("Message-ID") or f"smtp-{int(time.time()*1000)}", "sent")

    def create_draft(self, email_msg: OutgoingEmail) -> SendResult:
        if not self.credentials.get("imap_host"):
            # No IMAP mailbox to store the draft in; the message stays server-side
            # as an approved-but-unsent record. Caller may still send() later.
            return SendResult(f"draft-{int(time.time()*1000)}", "draft")
        message = self._build(email_msg)
        conn = self._imap()
        try:
            conn.append("Drafts", "\\Draft", imaplib.Time2Internaldate(time.time()),
                        message.as_bytes())
        finally:
            conn.logout()
        return SendResult(message.get("Message-ID") or f"draft-{int(time.time()*1000)}", "draft")

    def send_draft(self, draft_id: str) -> SendResult:
        raise NotImplementedError("SMTP drafts are sent by re-issuing send_email")

    def get_message_status(self, provider_message_id: str) -> str:
        return "accepted"  # SMTP gives no post-send tracking; bounces arrive via IMAP

    def _imap(self) -> imaplib.IMAP4:
        port = int(self.credentials.get("imap_port", 993))
        conn = imaplib.IMAP4_SSL(self.credentials["imap_host"], port)
        conn.login(self.credentials["username"], self.credentials["password"])
        return conn

    def list_recent_replies(self) -> list[dict]:
        if not self.credentials.get("imap_host"):
            return []
        conn = self._imap()
        replies: list[dict] = []
        try:
            conn.select("INBOX")
            since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 30 * 86400))
            _, data = conn.search(None, "SINCE", since)
            ids = (data[0] or b"").split()[-50:]
            for msg_id in ids:
                _, fetched = conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
                if not fetched or not fetched[0]:
                    continue
                headers = email.message_from_bytes(fetched[0][1])
                replies.append({
                    "id": (headers.get("Message-ID") or msg_id.decode()).strip(),
                    "from": parseaddr(headers.get("From", ""))[1],
                    "subject": headers.get("Subject", ""),
                    "internet_message_id": headers.get("Message-ID"),
                })
        finally:
            conn.logout()
        return replies

    def disconnect_account(self) -> None:
        self.credentials.clear()
