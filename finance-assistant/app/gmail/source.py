from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from app.config import AppConfig, BankConfig
from app.ingestion.models import IngestionSource, IngestionStatus, ProcessingResult
from app.ingestion.service import IngestionService

from .client import GmailClient
from .models import GmailBankSummary, GmailMessage, GmailSyncSummary


class GmailConfigError(ValueError):
    pass


def validate_gmail_bank_config(bank_id: str, config: BankConfig) -> None:
    if not config.gmail_senders or any(not item.strip() or item.strip() == "[REDACTED]" for item in config.gmail_senders):
        raise GmailConfigError(f"Gmail sender config missing for bank: {bank_id}")
    if not config.gmail_subject_keywords or any(not item.strip() for item in config.gmail_subject_keywords):
        raise GmailConfigError(f"Gmail subject config invalid for bank: {bank_id}")
    extensions = {item.casefold() for item in config.gmail_attachment_extensions}
    if ".pdf" not in extensions:
        raise GmailConfigError(f"Gmail PDF attachment config missing for bank: {bank_id}")


def has_gmail_sender_config(config: BankConfig) -> bool:
    return bool(config.gmail_senders) and all(
        item.strip() and item.strip() != "[REDACTED]" for item in config.gmail_senders
    )


def build_gmail_query(config: BankConfig, *, since: date, until: date) -> str:
    senders = config.gmail_senders
    sender_query = f"from:{senders[0]}" if len(senders) == 1 else "{" + " ".join(f"from:{sender}" for sender in senders) + "}"
    subjects = "{" + " ".join(f'subject:"{keyword}"' for keyword in config.gmail_subject_keywords) + "}"
    # Gmail's `after:` boundary is exclusive.  Move it back one day so the
    # requested interval is inclusive of `since`; `before:` remains exclusive.
    after = since - timedelta(days=1)
    before = until + timedelta(days=1)
    return f"{sender_query} has:attachment filename:pdf {subjects} after:{after:%Y/%m/%d} before:{before:%Y/%m/%d}"


class GmailSource:
    SUPPORTED_BANKS = ("isbank_maximum", "axess", "enpara")

    def __init__(self, *, client: GmailClient, ingestion_service: IngestionService, config: AppConfig, temp_dir: str | Path | None = None) -> None:
        self.client = client
        self.ingestion_service = ingestion_service
        self.config = config
        self.temp_dir = Path(temp_dir) if temp_dir else ingestion_service.data_dir / "tmp" / "gmail"

    def sync(self, *, since: date, until: date, bank_id: str | None = None, dry_run: bool = False) -> GmailSyncSummary:
        bank_ids = [bank_id] if bank_id else [item for item in self.SUPPORTED_BANKS if item in self.config.banks]
        summaries: list[GmailBankSummary] = []
        for current_bank in bank_ids:
            if current_bank not in self.config.banks:
                raise GmailConfigError(f"Unknown bank: {current_bank}")
            bank_config = self.config.banks[current_bank]
            if not has_gmail_sender_config(bank_config):
                summaries.append(GmailBankSummary(current_bank, status="SKIPPED_CONFIG"))
                continue
            validate_gmail_bank_config(current_bank, bank_config)
            summary = GmailBankSummary(current_bank)
            message_ids = self.client.list_message_ids(build_gmail_query(bank_config, since=since, until=until))
            summary.messages_found = len(message_ids)
            for message_id in message_ids:
                message = self.client.get_message(message_id)
                for attachment in message.attachments:
                    if not attachment.is_pdf:
                        continue
                    summary.pdf_attachments += 1
                    external_id = f"gmail:{message.message_id}:{attachment.attachment_id}"
                    if dry_run:
                        continue
                    if self.ingestion_service.external_id_processed(external_id):
                        summary.results.append(ProcessingResult(
                            source=IngestionSource.GMAIL, file_name="attachment.pdf", sha256="",
                            source_external_id=external_id, bank=current_bank,
                            status=IngestionStatus.SKIPPED_DUPLICATE, duplicates_skipped=1,
                        ))
                        continue
                    summary.results.append(self._process_attachment(message, attachment, external_id))
            summaries.append(summary)
        return GmailSyncSummary(summaries, dry_run=dry_run)

    def _process_attachment(self, message: GmailMessage, attachment, external_id: str) -> ProcessingResult:
        payload = self.client.download_attachment(message.message_id, attachment.attachment_id)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=self.temp_dir) as run_dir:
            path = Path(run_dir) / f"gmail-{message.message_id[:12]}-{attachment.index}.pdf"
            path.write_bytes(payload)
            return self.ingestion_service.process_file(
                path, source=IngestionSource.GMAIL, source_external_id=external_id
            )
