from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from app.models import Statement, Transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS banks (
    bank_id VARCHAR PRIMARY KEY,
    display_name VARCHAR NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS statements (
    id VARCHAR PRIMARY KEY,
    bank VARCHAR NOT NULL,
    message_id VARCHAR NOT NULL,
    attachment_sha256 VARCHAR NOT NULL UNIQUE,
    file_path VARCHAR NOT NULL,
    statement_period_start DATE,
    statement_period_end DATE,
    due_date DATE,
    cutoff_date DATE,
    statement_date DATE,
    card_identifier VARCHAR NOT NULL DEFAULT '****',
    currency VARCHAR NOT NULL DEFAULT 'TRY',
    statement_total DECIMAL(18, 2),
    minimum_payment DECIMAL(18, 2),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id VARCHAR PRIMARY KEY,
    fingerprint VARCHAR NOT NULL UNIQUE,
    bank VARCHAR NOT NULL,
    card_identifier VARCHAR NOT NULL,
    statement_id VARCHAR NOT NULL,
    statement_period_start DATE,
    statement_period_end DATE,
    transaction_date DATE NOT NULL,
    posting_date DATE,
    merchant_raw VARCHAR NOT NULL,
    merchant_normalized VARCHAR NOT NULL,
    description_raw VARCHAR,
    amount DECIMAL(18, 2) NOT NULL,
    currency VARCHAR NOT NULL,
    installment_current INTEGER,
    installment_total INTEGER,
    transaction_type VARCHAR NOT NULL,
    category VARCHAR NOT NULL,
    subcategory VARCHAR,
    category_source VARCHAR NOT NULL,
    confidence DECIMAL(5, 4),
    statement_file VARCHAR,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS merchant_rules (
    merchant_normalized VARCHAR PRIMARY KEY,
    category VARCHAR NOT NULL,
    subcategory VARCHAR,
    source VARCHAR NOT NULL DEFAULT 'manual',
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    category VARCHAR NOT NULL,
    subcategory VARCHAR,
    PRIMARY KEY(category, subcategory)
);
CREATE TABLE IF NOT EXISTS monthly_reports (
    report_month DATE PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    report_path VARCHAR,
    is_complete BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS processing_log (
    id BIGINT PRIMARY KEY,
    stage VARCHAR NOT NULL,
    file_path VARCHAR,
    status VARCHAR NOT NULL,
    transaction_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    message VARCHAR,
    created_at TIMESTAMPTZ NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path, *, read_only: bool = False):
        database_path = Path(path)
        if read_only:
            if not database_path.is_file():
                raise FileNotFoundError(database_path)
            self.connection = duckdb.connect(str(database_path), read_only=True)
        else:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = duckdb.connect(str(database_path))

    def initialize(self) -> None:
        self.connection.execute(SCHEMA)
        for column, definition in (
            ("statement_date", "DATE"),
            ("card_identifier", "VARCHAR"),
            ("currency", "VARCHAR"),
            ("statement_total", "DECIMAL(18, 2)"),
            ("minimum_payment", "DECIMAL(18, 2)"),
        ):
            self.connection.execute(
                f"ALTER TABLE statements ADD COLUMN IF NOT EXISTS {column} {definition}"
            )
        for column, definition in (
            ("source", "VARCHAR"),
            ("sha256", "VARCHAR"),
            ("bank", "VARCHAR"),
            ("inserted_count", "INTEGER"),
            ("duplicate_count", "INTEGER"),
            ("warning_count", "INTEGER"),
            ("error_code", "VARCHAR"),
            ("source_external_id", "VARCHAR"),
        ):
            self.connection.execute(
                f"ALTER TABLE processing_log ADD COLUMN IF NOT EXISTS {column} {definition}"
            )

    def insert_statement(self, statement: Statement) -> bool:
        exists = self.connection.execute(
            "SELECT 1 FROM statements WHERE attachment_sha256 = ? LIMIT 1",
            [statement.attachment_sha256],
        ).fetchone()
        if exists:
            return False
        self.connection.execute(
            """INSERT INTO statements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [statement.id, statement.bank, statement.message_id, statement.attachment_sha256,
             statement.file_path, statement.statement_period_start, statement.statement_period_end,
             statement.due_date, statement.cutoff_date, statement.statement_date,
             statement.card_identifier, statement.currency, statement.statement_total,
             statement.minimum_payment, statement.created_at],
        )
        return True

    def statement_id_by_hash(self, attachment_sha256: str) -> str | None:
        row = self.connection.execute(
            "SELECT id FROM statements WHERE attachment_sha256 = ? LIMIT 1",
            [attachment_sha256],
        ).fetchone()
        return str(row[0]) if row else None

    def statement_bank_by_hash(self, attachment_sha256: str) -> str | None:
        row = self.connection.execute(
            "SELECT bank FROM statements WHERE attachment_sha256 = ? LIMIT 1",
            [attachment_sha256],
        ).fetchone()
        return str(row[0]) if row else None

    def source_external_id_exists(self, source_external_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM processing_log WHERE source_external_id = ? LIMIT 1",
            [source_external_id],
        ).fetchone()
        return row is not None

    def statement_id_by_semantics(self, statement: Statement) -> str | None:
        row = self.connection.execute(
            """SELECT id FROM statements
               WHERE bank = ?
                 AND statement_period_start IS NOT DISTINCT FROM ?
                 AND statement_period_end IS NOT DISTINCT FROM ?
                 AND statement_date IS NOT DISTINCT FROM ?
                 AND card_identifier IS NOT DISTINCT FROM ?
               LIMIT 1""",
            [statement.bank, statement.statement_period_start, statement.statement_period_end,
             statement.statement_date, statement.card_identifier],
        ).fetchone()
        return str(row[0]) if row else None

    def insert_transaction(self, transaction: Transaction) -> bool:
        exists = self.connection.execute(
            "SELECT 1 FROM transactions WHERE fingerprint = ? LIMIT 1",
            [transaction.fingerprint],
        ).fetchone()
        if exists:
            return False
        self.connection.execute(
            """INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [transaction.id, transaction.fingerprint, transaction.bank, transaction.card_identifier,
             transaction.statement_id, transaction.statement_period_start, transaction.statement_period_end,
             transaction.transaction_date, transaction.posting_date, transaction.merchant_raw,
             transaction.merchant_normalized, transaction.description_raw, transaction.amount,
             transaction.currency, transaction.installment_current, transaction.installment_total,
             transaction.transaction_type.value, transaction.category, transaction.subcategory,
             transaction.category_source, transaction.confidence, transaction.statement_file,
             transaction.created_at],
        )
        return True

    def insert_statement_and_transactions(
        self, statement: Statement, transactions: list[Transaction], *,
        source: str, warnings: list[str], status: str, source_external_id: str | None = None,
    ) -> tuple[str | None, int, int, bool]:
        """Insert one statement and its transactions atomically."""
        self.connection.execute("BEGIN TRANSACTION")
        try:
            existing_id = self.statement_id_by_hash(statement.attachment_sha256)
            if existing_id is None:
                existing_id = self.statement_id_by_semantics(statement)
            if existing_id is not None:
                self.connection.execute("COMMIT")
                return existing_id, 0, len(transactions), False

            self.connection.execute(
                """INSERT INTO statements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [statement.id, statement.bank, statement.message_id, statement.attachment_sha256,
                 statement.file_path, statement.statement_period_start, statement.statement_period_end,
                 statement.due_date, statement.cutoff_date, statement.statement_date,
                 statement.card_identifier, statement.currency, statement.statement_total,
                 statement.minimum_payment, statement.created_at],
            )
            inserted = skipped = 0
            for transaction in transactions:
                transaction.statement_id = statement.id
                if self.insert_transaction(transaction):
                    inserted += 1
                else:
                    skipped += 1
            self.log_processing(
                source=source, sha256=statement.attachment_sha256, bank=statement.bank,
                status=status, transaction_count=len(transactions), inserted_count=inserted,
                duplicate_count=skipped, warning_count=len(warnings), error_code=None,
                file_path=statement.file_path, message="; ".join(warnings) or None,
                source_external_id=source_external_id,
            )
            self.connection.execute("COMMIT")
            return statement.id, inserted, skipped, True
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def log_processing(
        self, *, source: str, sha256: str, bank: str | None, status: str,
        transaction_count: int = 0, inserted_count: int = 0, duplicate_count: int = 0,
        warning_count: int = 0, error_code: str | None = None,
        file_path: str | None = None, message: str | None = None,
        source_external_id: str | None = None,
    ) -> None:
        import time
        self.connection.execute(
            """INSERT INTO processing_log
            (id, stage, file_path, status, transaction_count, error_count, message,
             created_at, source, sha256, bank, inserted_count, duplicate_count,
             warning_count, error_code, source_external_id)
            VALUES (?, 'ingestion', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [time.time_ns(), file_path, status, transaction_count,
             1 if error_code else 0, message, source, sha256, bank,
             inserted_count, duplicate_count, warning_count, error_code, source_external_id],
        )

    def count(self, table: str) -> int:
        if table not in {"statements", "transactions", "processing_log"}:
            raise ValueError("unsupported table")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def count_transactions(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])

    def close(self) -> None:
        self.connection.close()


def initialize_database(path: str | Path) -> Database:
    database = Database(path)
    database.initialize()
    return database
