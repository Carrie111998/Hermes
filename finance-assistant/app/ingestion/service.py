from __future__ import annotations

import hashlib
import shutil
import calendar
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.database import initialize_database
from app.models import IngestionSource, IngestionStatus, Statement
from app.parsers.base import ParserFormatError, ParserRegistry, StatementDocument
from app.parsers.utils import load_pdf_document

from .models import ProcessingResult


class IngestionService:
    """Source-independent PDF ingestion and persistence orchestration."""

    def __init__(
        self,
        *,
        data_dir: str | Path,
        db_path: str | Path | None = None,
        registry: ParserRegistry,
        document_loader: Callable[[str | Path], StatementDocument] = load_pdf_document,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = Path(db_path) if db_path else self.data_dir / "database" / "finance.duckdb"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.document_loader = document_loader

    def process_file(
        self, path: str | Path, *, source: IngestionSource = IngestionSource.MANUAL,
        source_external_id: str | None = None,
    ) -> ProcessingResult:
        pdf_path = Path(path).expanduser().resolve()
        digest = ""
        try:
            if not pdf_path.is_file():
                raise FileNotFoundError(str(pdf_path))
            if pdf_path.suffix.casefold() != ".pdf":
                raise ValueError("Only PDF files are supported")
            digest = self._sha256(pdf_path)
            result = ProcessingResult(source=source, file_name=pdf_path.name, sha256=digest, source_external_id=source_external_id)

            existing_bank = self._statement_bank_by_hash(digest)
            if existing_bank is not None:
                result.bank = existing_bank
                result.status = IngestionStatus.SKIPPED_DUPLICATE
                result.duplicates_skipped = 1
                result.archive_path = self._move_to_duplicates(pdf_path, digest)
                self._record_log(result, error_code="DUPLICATE_HASH")
                return result

            document = self.document_loader(pdf_path)
            parser = self.registry.find(document)
            if parser is None:
                return self._finish_failed(
                    result, IngestionStatus.UNSUPPORTED,
                    "No supported bank parser detected", "UNSUPPORTED", pdf_path,
                )
            result.bank = parser.bank_id
            try:
                metadata = parser.parse_metadata(document)
                transactions = parser.parse_transactions(document, metadata)
            except ParserFormatError as exc:
                return self._finish_failed(
                    result, IngestionStatus.FORMAT_ERROR, str(exc), "FORMAT_ERROR", pdf_path,
                )

            result.statement_period_start = metadata.period_start
            result.statement_period_end = metadata.period_end
            result.transactions_found = len(transactions)
            statement = Statement(
                bank=parser.bank_id,
                message_id=source.value.lower(),
                attachment_sha256=digest,
                file_path=result.safe_name,
                statement_period_start=metadata.period_start,
                statement_period_end=metadata.period_end,
                due_date=metadata.due_date,
                cutoff_date=metadata.cutoff_date,
                statement_date=metadata.statement_date,
                card_identifier=metadata.card_identifier,
                currency=metadata.currency,
                statement_total=metadata.statement_total,
                minimum_payment=metadata.minimum_payment,
            )
            result.warnings = self._reconciliation_warnings(transactions, metadata.statement_total)
            statement_id, inserted, skipped, statement_inserted = self._insert_database(
                statement, transactions, source=source, warnings=result.warnings,
                source_external_id=source_external_id,
            )
            result.statement_id = statement_id
            result.transactions_inserted = inserted
            result.duplicates_skipped = skipped
            if not statement_inserted:
                result.status = IngestionStatus.SKIPPED_DUPLICATE
                result.duplicates_skipped = result.transactions_found
                result.archive_path = self._move_to_duplicates(pdf_path, digest)
                self._record_log(result, error_code="DUPLICATE_STATEMENT")
                return result

            result.status = (
                IngestionStatus.SUCCESS_WITH_WARNINGS
                if result.warnings or skipped
                else IngestionStatus.SUCCESS
            )
            result.archive_path = self._move_to_archive(pdf_path, parser.bank_id, metadata.statement_date or metadata.period_end)
            self._record_log(result, already_recorded=True)
            return result
        except ParserFormatError as exc:
            result = ProcessingResult(source=source, file_name=pdf_path.name, sha256=digest, source_external_id=source_external_id)
            return self._finish_failed(result, IngestionStatus.FORMAT_ERROR, str(exc), "FORMAT_ERROR", pdf_path)
        except Exception as exc:
            result = ProcessingResult(source=source, file_name=pdf_path.name, sha256=digest, source_external_id=source_external_id)
            return self._finish_failed(result, IngestionStatus.FAILED, str(exc), "PROCESSING_ERROR", pdf_path)

    def process_directory(self, directory: str | Path | None = None) -> list[ProcessingResult]:
        inbox = Path(directory) if directory else self.data_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        return [
            self.process_file(path, source=IngestionSource.DIRECTORY)
            for path in sorted(inbox.iterdir())
            if path.is_file() and path.suffix.casefold() == ".pdf"
        ]

    def get_statement_status(self, year: int, month: int) -> dict[str, str]:
        db = initialize_database(self.db_path)
        try:
            rows = db.connection.execute(
                """SELECT bank, statement_period_start, statement_period_end
                   FROM statements
                   WHERE COALESCE(statement_period_start, statement_date) <= ?
                     AND COALESCE(statement_period_end, statement_date, statement_period_start) >= ?""",
                [date(year, month, calendar.monthrange(year, month)[1]), date(year, month, 1)],
            ).fetchall()
        finally:
            db.close()
        present = {str(bank) for bank, _start, _end in rows}
        return {parser.bank_id: ("PRESENT" if parser.bank_id in present else "MISSING") for parser in self.registry.parsers}

    def database_count(self, table: str) -> int:
        db = initialize_database(self.db_path)
        try:
            return db.count(table)
        finally:
            db.close()

    def external_id_processed(self, source_external_id: str) -> bool:
        db = initialize_database(self.db_path)
        try:
            return db.source_external_id_exists(source_external_id)
        finally:
            db.close()

    def _insert_database(self, statement, transactions, *, source, warnings, source_external_id=None):
        db = initialize_database(self.db_path)
        try:
            return db.insert_statement_and_transactions(
                statement, transactions, source=source.value,
                warnings=warnings, source_external_id=source_external_id,
                status=(IngestionStatus.SUCCESS_WITH_WARNINGS.value if warnings else IngestionStatus.SUCCESS.value),
            )
        finally:
            db.close()

    def _statement_bank_by_hash(self, digest: str) -> str | None:
        if not self.db_path.exists():
            return None
        db = initialize_database(self.db_path)
        try:
            return db.statement_bank_by_hash(digest)
        finally:
            db.close()

    def _finish_failed(self, result, status, error, error_code, pdf_path):
        result.status = status
        result.errors.append(error)
        result.failed_path = self._move_to_failed(pdf_path, status)
        self._record_log(result, error_code=error_code)
        return result

    def _record_log(self, result: ProcessingResult, *, error_code: str | None = None, already_recorded: bool = False) -> None:
        if already_recorded or not result.sha256:
            return
        db = initialize_database(self.db_path)
        try:
            db.log_processing(
                source=result.source.value, sha256=result.sha256, bank=result.bank,
                status=result.status.value, transaction_count=result.transactions_found,
                inserted_count=result.transactions_inserted, duplicate_count=result.duplicates_skipped,
                warning_count=result.warning_count, error_code=error_code,
                file_path=result.safe_name, message="; ".join(result.errors or result.warnings) or None,
                source_external_id=result.source_external_id,
            )
        finally:
            db.close()

    def _move_to_archive(self, source: Path, bank: str, statement_date: date | None) -> str:
        year = str(statement_date.year if statement_date else date.today().year)
        destination_dir = self.data_dir / "archive" / year / bank
        return str(self._move_safely(source, destination_dir, f"{statement_date or date.today()}-{self._sha256(source)[:12]}.pdf"))

    def _move_to_duplicates(self, source: Path, digest: str) -> str:
        return str(self._move_safely(source, self.data_dir / "archive" / "duplicates", f"duplicate-{digest[:12]}.pdf"))

    def _move_to_failed(self, source: Path, status: IngestionStatus) -> str | None:
        if not source.exists():
            return None
        folder = "unsupported" if status is IngestionStatus.UNSUPPORTED else "format_error" if status is IngestionStatus.FORMAT_ERROR else "processing_error"
        return str(self._move_safely(source, self.data_dir / "failed" / folder, f"{source.stem}.pdf"))

    @staticmethod
    def _move_safely(source: Path, destination_dir: Path, filename: str) -> Path:
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / filename
        counter = 1
        while destination.exists():
            destination = destination_dir / f"{Path(filename).stem}-{counter}{Path(filename).suffix}"
            counter += 1
        return Path(shutil.move(str(source), str(destination)))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _reconciliation_warnings(transactions, statement_total):
        if statement_total is None:
            return []
        parsed_total = sum((tx.amount for tx in transactions), start=0)
        return [] if abs(parsed_total - statement_total) <= Decimal("0.01") else [
            "Statement total does not directly reconcile with transaction rows"
        ]
