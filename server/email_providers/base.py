"""Email provider adapter interface (PRODUCT.md §3).

Every provider (Gmail, Microsoft Graph, Zoho, SMTP, ...) implements this.
Sending is deterministic code, never an agent action — the agent composes,
a human approves, this adapter sends. Approval is enforced by
``server.outreach_service``, not by the provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class OutgoingEmail:
    to: str
    subject: str
    body: str
    cc: list[str] = field(default_factory=list)
    language: Optional[str] = None
    reply_to: Optional[str] = None


@dataclass
class SendResult:
    provider_message_id: str
    status: str  # "draft" | "sent"


class EmailProvider(Protocol):
    """The seven-plus operations from PRODUCT.md §3."""

    def connect_account(self, credentials: dict) -> None: ...
    def refresh_token(self) -> None: ...
    def create_draft(self, email: OutgoingEmail) -> SendResult: ...
    def send_email(self, email: OutgoingEmail) -> SendResult: ...
    def send_draft(self, draft_id: str) -> SendResult: ...
    def get_message_status(self, provider_message_id: str) -> str: ...
    def list_recent_replies(self) -> list[dict]: ...
    def disconnect_account(self) -> None: ...
