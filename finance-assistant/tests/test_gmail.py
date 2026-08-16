from __future__ import annotations

import base64
from datetime import date
from pathlib import Path

import pytest

from app.config import AppConfig, BankConfig
from app.gmail.client import GmailClient
from app.gmail.models import GmailAttachment, GmailMessage
from app.gmail.source import GmailConfigError, GmailSource, build_gmail_query, validate_gmail_bank_config
from app.ingestion.models import IngestionSource, IngestionStatus, ProcessingResult


def bank_config(sender="statements@example.test", keywords=None):
    senders = sender if isinstance(sender, list) else [sender]
    return BankConfig(
        senders=[], subject_keywords=[], statement_keywords=[],
        gmail_senders=senders, gmail_subject_keywords=keywords or ["Ekstre"],
        gmail_attachment_extensions=[".pdf"],
    )


def app_config():
    return AppConfig(banks={"enpara": bank_config()}, categories={}, fee_keywords=[])


def multi_bank_config():
    return AppConfig(
        banks={
            "isbank_maximum": bank_config(sender=""),
            "axess": bank_config(sender=""),
            "enpara": bank_config(sender=["enpara@enpara.com", "enpara@mailer.enpara.com"]),
        },
        categories={}, fee_keywords=[],
    )


def test_gmail_query_generation_is_server_side_and_date_bounded():
    query = build_gmail_query(bank_config(), since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert "from:statements@example.test" in query
    assert "has:attachment filename:pdf" in query
    assert 'subject:"Ekstre"' in query
    assert "after:2026/07/31" in query
    assert "before:2026/09/01" in query


def test_gmail_month_filter_august_2026():
    query = build_gmail_query(bank_config(), since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert "after:2026/07/31" in query
    assert "before:2026/09/01" in query


def test_gmail_month_query_boundaries():
    query = build_gmail_query(bank_config(), since=date(2026, 2, 1), until=date(2026, 2, 28))
    assert "after:2026/01/31" in query
    assert "before:2026/03/01" in query


def test_gmail_month_filter_does_not_use_statement_period():
    query = build_gmail_query(bank_config(), since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert "after:2026/07/31" in query
    assert "before:2026/09/01" in query
    assert "statement_period" not in query.casefold()


def test_enpara_multiple_senders_query():
    config = bank_config(sender=["enpara@enpara.com", "enpara@mailer.enpara.com"])
    query = build_gmail_query(config, since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert "{from:enpara@enpara.com from:enpara@mailer.enpara.com}" in query


def test_gmail_sync_skips_unconfigured_bank(tmp_path):
    client, ingestion = FakeClient(), FakeIngestion()
    source = GmailSource(client=client, ingestion_service=ingestion, config=multi_bank_config(), temp_dir=tmp_path)
    summary = source.sync(since=date(2026, 8, 1), until=date(2026, 8, 31))
    statuses = {item.bank: item.status for item in summary.banks}
    assert statuses["isbank_maximum"] == "SKIPPED_CONFIG"
    assert statuses["axess"] == "SKIPPED_CONFIG"
    assert client.queries == [summary_query for summary_query in client.queries if "from:enpara@" in summary_query]


def test_gmail_sync_continues_other_configured_banks(tmp_path):
    client, ingestion = FakeClient(), FakeIngestion()
    source = GmailSource(client=client, ingestion_service=ingestion, config=multi_bank_config(), temp_dir=tmp_path)
    summary = source.sync(since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert [item.bank for item in summary.banks] == ["isbank_maximum", "axess", "enpara"]
    assert summary.banks[-1].pdf_attachments == 1


def test_no_broad_query_for_unconfigured_bank(tmp_path):
    client, ingestion = FakeClient(), FakeIngestion()
    source = GmailSource(client=client, ingestion_service=ingestion, config=multi_bank_config(), temp_dir=tmp_path)
    source.sync(since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert all("from:" in query for query in client.queries)
    assert not any("has:attachment filename:pdf" == query.strip() for query in client.queries)


def test_gmail_config_fails_closed_for_redacted_sender():
    with pytest.raises(GmailConfigError):
        validate_gmail_bank_config("enpara", bank_config("[REDACTED]"))


def test_gmail_attachment_detection_skips_non_pdf():
    class Request:
        def execute(self):
            return {"id": "msg-1", "internalDate": "1", "payload": {"parts": [
                {"filename": "logo.png", "mimeType": "image/png", "body": {"attachmentId": "a1"}},
                {"filename": "statement.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "a2"}},
            ]}}
    class Messages:
        def get(self, **kwargs): return Request()
    class Users:
        def messages(self): return Messages()
    class Service:
        def users(self): return Users()

    message = GmailClient(Service()).get_message("msg-1")
    assert [item.filename for item in message.attachments] == ["statement.pdf"]


def test_gmail_client_retries_429_with_bounded_backoff():
    class Error(Exception):
        def __init__(self): self.resp = type("Resp", (), {"status": 429})()
    class Request:
        calls = 0
        def execute(self):
            self.calls += 1
            if self.calls < 3: raise Error()
            return {"ok": True}
    request, sleeps = Request(), []
    assert GmailClient(object(), sleep=sleeps.append)._execute(request) == {"ok": True}
    assert request.calls == 3
    assert sleeps == [1, 2]


class FakeClient:
    def __init__(self):
        self.queries = []
        self.messages = {"m1": GmailMessage("m1", "1", (GmailAttachment("a1", "statement.pdf", "application/pdf", 0),))}
    def list_message_ids(self, query):
        self.queries.append(query)
        return ["m1"]
    def get_message(self, message_id): return self.messages[message_id]
    def download_attachment(self, message_id, attachment_id): return b"%PDF-fake"


class FakeIngestion:
    data_dir = Path("/tmp/finance-gmail-test")
    def __init__(self, processed=False): self.processed = processed; self.calls = []
    def external_id_processed(self, external_id): return self.processed
    def process_file(self, path, *, source, source_external_id):
        self.calls.append((Path(path), source, source_external_id))
        return ProcessingResult(source=source, file_name=Path(path).name, sha256="a" * 64,
                                source_external_id=source_external_id, bank="enpara",
                                status=IngestionStatus.SUCCESS, transactions_inserted=3)


def test_gmail_source_dry_run_does_not_download_or_ingest(tmp_path):
    client, ingestion = FakeClient(), FakeIngestion()
    source = GmailSource(client=client, ingestion_service=ingestion, config=app_config(), temp_dir=tmp_path)
    summary = source.sync(since=date(2026, 8, 1), until=date(2026, 8, 31), dry_run=True)
    assert summary.messages_checked == 1
    assert summary.pdf_attachments == 1
    assert ingestion.calls == []


def test_gmail_source_ingests_pdf_with_external_id_and_cleans_temp(tmp_path):
    client, ingestion = FakeClient(), FakeIngestion()
    source = GmailSource(client=client, ingestion_service=ingestion, config=app_config(), temp_dir=tmp_path)
    summary = source.sync(since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert summary.imported == 1
    assert ingestion.calls[0][1:] == (IngestionSource.GMAIL, "gmail:m1:a1")
    assert list(tmp_path.rglob("*.pdf")) == []


def test_gmail_source_skips_external_duplicate_without_download(tmp_path):
    client, ingestion = FakeClient(), FakeIngestion(processed=True)
    source = GmailSource(client=client, ingestion_service=ingestion, config=app_config(), temp_dir=tmp_path)
    summary = source.sync(since=date(2026, 8, 1), until=date(2026, 8, 31))
    assert summary.duplicates == 1
    assert ingestion.calls == []
