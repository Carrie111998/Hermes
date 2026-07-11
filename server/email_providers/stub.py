"""In-memory stub email provider — for verifying the send path without real
OAuth credentials. Gmail/Microsoft adapters replace this with the same
interface (base.EmailProvider).
"""
from __future__ import annotations

import uuid

from .base import EmailProvider, OutgoingEmail, SendResult


class StubEmailProvider:
    def __init__(self):
        self.drafts: dict[str, OutgoingEmail] = {}
        self.sent: dict[str, OutgoingEmail] = {}
        self.connected = False

    def connect_account(self, credentials: dict) -> None:
        self.connected = True

    def refresh_token(self) -> None:
        pass

    def create_draft(self, email: OutgoingEmail) -> SendResult:
        mid = "draft_" + uuid.uuid4().hex[:10]
        self.drafts[mid] = email
        return SendResult(provider_message_id=mid, status="draft")

    def send_email(self, email: OutgoingEmail) -> SendResult:
        mid = "sent_" + uuid.uuid4().hex[:10]
        self.sent[mid] = email
        return SendResult(provider_message_id=mid, status="sent")

    def send_draft(self, draft_id: str) -> SendResult:
        email = self.drafts.pop(draft_id)
        mid = "sent_" + uuid.uuid4().hex[:10]
        self.sent[mid] = email
        return SendResult(provider_message_id=mid, status="sent")

    def get_message_status(self, provider_message_id: str) -> str:
        if provider_message_id in self.sent:
            return "sent"
        if provider_message_id in self.drafts:
            return "draft"
        return "unknown"

    def list_recent_replies(self) -> list[dict]:
        return []

    def disconnect_account(self) -> None:
        self.connected = False
