from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.ingestion.models import ProcessingResult


@dataclass(frozen=True, slots=True)
class GmailAttachment:
    attachment_id: str
    filename: str
    mime_type: str
    index: int

    @property
    def is_pdf(self) -> bool:
        return self.mime_type.casefold() == "application/pdf" or self.filename.casefold().endswith(".pdf")


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    internal_date: str | None
    attachments: tuple[GmailAttachment, ...]


@dataclass(slots=True)
class GmailBankSummary:
    bank: str
    status: str = "READY"
    messages_found: int = 0
    pdf_attachments: int = 0
    results: list[ProcessingResult] | None = None

    def __post_init__(self) -> None:
        if self.results is None:
            self.results = []


@dataclass(slots=True)
class GmailSyncSummary:
    banks: list[GmailBankSummary]
    dry_run: bool = False

    @property
    def messages_checked(self) -> int:
        return sum(item.messages_found for item in self.banks)

    @property
    def pdf_attachments(self) -> int:
        return sum(item.pdf_attachments for item in self.banks)

    @property
    def imported(self) -> int:
        return sum(1 for item in self.banks for result in item.results or [] if result.status.value.startswith("SUCCESS"))

    @property
    def duplicates(self) -> int:
        return sum(1 for item in self.banks for result in item.results or [] if result.status.value == "SKIPPED_DUPLICATE")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.banks for result in item.results or [] if result.status.value in {"FAILED", "FORMAT_ERROR", "UNSUPPORTED"})


def parse_sync_date(value: str) -> date:
    return date.fromisoformat(value)
