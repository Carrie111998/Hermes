from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from app.database import initialize_database
from app.ingestion.models import IngestionSource, IngestionStatus
from app.ingestion.service import IngestionService
from app.models import Transaction, TransactionType
from app.parsers.base import ParserFormatError, StatementDocument, StatementMetadata, StatementParser, ParserRegistry


class DummyParser(StatementParser):
    bank_id = "dummy"

    def can_parse(self, document: StatementDocument) -> bool:
        return "DUMMY" in document.text

    def parse_metadata(self, document: StatementDocument) -> StatementMetadata:
        if "FORMAT_ERROR" in document.text:
            raise ParserFormatError("dummy format changed")
        return StatementMetadata(
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            statement_date=date(2026, 8, 31),
            card_identifier="****1234",
            statement_total=Decimal("20.00" if "TWO" in document.text else "10.00"),
        )

    def parse_transactions(self, document: StatementDocument, metadata: StatementMetadata) -> list[Transaction]:
        if "PARSE_ERROR" in document.text:
            raise ParserFormatError("dummy transaction table changed")
        count = 2 if "TWO" in document.text else 1
        transactions = [
            Transaction(
                bank=self.bank_id,
                card_identifier=metadata.card_identifier,
                statement_id="pending",
                statement_period_start=metadata.period_start,
                statement_period_end=metadata.period_end,
                transaction_date=date(2026, 8, index + 1),
                merchant_raw=f"MERCHANT-{index}",
                amount=Decimal("10.00"),
                transaction_type=TransactionType.PURCHASE,
            )
            for index in range(count)
        ]
        if "DUP_TX" in document.text:
            transactions[1] = transactions[0]
        return transactions


def make_service(tmp_path: Path, *, loader=None) -> IngestionService:
    return IngestionService(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "database" / "finance.duckdb",
        registry=ParserRegistry([DummyParser()]),
        document_loader=loader or (lambda path: StatementDocument(path, path.read_text())),
    )


def create_pdf(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_ingest_single_file_and_archive(tmp_path):
    service = make_service(tmp_path)
    pdf = create_pdf(tmp_path / "statement.pdf", "DUMMY TWO")

    result = service.process_file(pdf)

    assert result.status is IngestionStatus.SUCCESS
    assert result.source is IngestionSource.MANUAL
    assert result.transactions_found == 2
    assert result.transactions_inserted == 2
    assert result.archive_path is not None
    assert Path(result.archive_path).is_file()
    assert not pdf.exists()


def test_ingest_duplicate_hash_moves_duplicate_to_archive(tmp_path):
    service = make_service(tmp_path)
    source = create_pdf(tmp_path / "statement.pdf", "DUMMY")
    first = service.process_file(source)
    duplicate = create_pdf(tmp_path / "same-copy.pdf", "DUMMY")

    result = service.process_file(duplicate)

    assert first.status is IngestionStatus.SUCCESS
    assert result.status is IngestionStatus.SKIPPED_DUPLICATE
    assert result.duplicates_skipped == 1
    assert result.archive_path is not None
    assert Path(result.archive_path).is_file()
    assert service.database_count("statements") == 1
    assert service.database_count("transactions") == 1


def test_ingest_statement_duplicate_different_binary(tmp_path):
    service = make_service(tmp_path)
    first = create_pdf(tmp_path / "first.pdf", "DUMMY")
    second = create_pdf(tmp_path / "second.pdf", "DUMMY DIFFERENT")

    service.process_file(first)
    result = service.process_file(second)

    assert result.status is IngestionStatus.SKIPPED_DUPLICATE
    assert result.duplicates_skipped == 1
    assert service.database_count("statements") == 1
    assert service.database_count("transactions") == 1


def test_ingest_unsupported_pdf_goes_to_failed(tmp_path):
    service = make_service(tmp_path)
    pdf = create_pdf(tmp_path / "unknown.pdf", "NOT A BANK STATEMENT")

    result = service.process_file(pdf)

    assert result.status is IngestionStatus.UNSUPPORTED
    assert result.errors == ["No supported bank parser detected"]
    assert result.failed_path is not None
    assert Path(result.failed_path).is_file()
    assert service.database_count("statements") == 0


def test_ingest_parser_format_error_is_distinct(tmp_path):
    service = make_service(tmp_path)
    pdf = create_pdf(tmp_path / "changed.pdf", "DUMMY FORMAT_ERROR")

    result = service.process_file(pdf)

    assert result.status is IngestionStatus.FORMAT_ERROR
    assert result.failed_path is not None
    assert "dummy format changed" in result.errors[0]


def test_ingest_transaction_duplicates_are_skipped_inside_atomic_insert(tmp_path):
    service = make_service(tmp_path)
    result = service.process_file(create_pdf(tmp_path / "duplicate-transactions.pdf", "DUMMY TWO DUP_TX"))

    assert result.status is IngestionStatus.SUCCESS_WITH_WARNINGS
    assert result.transactions_found == 2
    assert result.transactions_inserted == 1
    assert result.duplicates_skipped == 1
    assert service.database_count("transactions") == 1


def test_ingest_writes_safe_processing_audit_log(tmp_path):
    service = make_service(tmp_path)
    service.process_file(create_pdf(tmp_path / "private-name.pdf", "DUMMY"))

    db = initialize_database(tmp_path / "data" / "database" / "finance.duckdb")
    row = db.connection.execute(
        "SELECT source, status, bank, transaction_count, inserted_count, file_path FROM processing_log"
    ).fetchone()
    db.close()
    assert row[:5] == ("MANUAL", "SUCCESS", "dummy", 1, 1)
    assert row[5].startswith("document-")


def test_gmail_external_id_is_persisted_in_processing_log(tmp_path):
    service = make_service(tmp_path)
    pdf = create_pdf(tmp_path / "gmail-statement.pdf", "DUMMY")

    result = service.process_file(
        pdf,
        source=IngestionSource.GMAIL,
        source_external_id="gmail:message-1:attachment-1",
    )

    db = initialize_database(tmp_path / "data" / "database" / "finance.duckdb")
    row = db.connection.execute(
        "SELECT source, source_external_id FROM processing_log"
    ).fetchone()
    db.close()

    assert result.source_external_id == "gmail:message-1:attachment-1"
    assert row == ("GMAIL", "gmail:message-1:attachment-1")
    assert service.external_id_processed("gmail:message-1:attachment-1") is True


def test_database_migration_is_idempotent_and_preserves_existing_rows(tmp_path):
    db_path = tmp_path / "data" / "database" / "finance.duckdb"
    first = initialize_database(db_path)
    first.log_processing(
        source="MANUAL", sha256="a" * 64, bank="dummy", status="SUCCESS"
    )
    first.close()

    second = initialize_database(db_path)
    second.initialize()
    columns = {
        row[0] for row in second.connection.execute("DESCRIBE processing_log").fetchall()
    }
    count = second.count("processing_log")
    second.close()

    assert count == 1
    assert "source_external_id" in columns


def test_batch_ingest_assigns_directory_source(tmp_path):
    service = make_service(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    create_pdf(inbox / "one.pdf", "DUMMY")
    create_pdf(inbox / "two.pdf", "UNKNOWN")

    results = service.process_directory(inbox)

    assert len(results) == 2
    assert all(result.source is IngestionSource.DIRECTORY for result in results)
    assert {result.status for result in results} == {IngestionStatus.SUCCESS, IngestionStatus.UNSUPPORTED}


def test_database_failure_rolls_back_statement_and_transactions(tmp_path):
    class FailingService(IngestionService):
        def _insert_database(self, *args, **kwargs):
            raise RuntimeError("simulated database failure")

    service = FailingService(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "database" / "finance.duckdb",
        registry=ParserRegistry([DummyParser()]),
        document_loader=lambda path: StatementDocument(path, path.read_text()),
    )
    pdf = create_pdf(tmp_path / "failure.pdf", "DUMMY TWO")

    result = service.process_file(pdf)

    assert result.status is IngestionStatus.FAILED
    assert result.failed_path is not None
    assert service.database_count("statements") == 0
    assert service.database_count("transactions") == 0
